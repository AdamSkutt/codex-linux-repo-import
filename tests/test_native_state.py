from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from codex_repo_import import native_state
from codex_repo_import.native_state import (
    NativeStateError,
    NativeStateStore,
    classify_compatibility,
    is_desktop_running,
    load_snapshot,
    validate_native_state,
)
from codex_repo_import.planner import build_import_plan, encode_state

from tests.helpers import cloned_state, group, session


TESTED_VERSION = "26.803.81509"
AS_OF = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def valid_native_state():
    """Turn the planner-conflict fixture into a referentially valid disk state."""

    state = cloned_state()
    state["local-projects"]["some-other-project"] = {
        "id": "some-other-project",
        "name": "Some Other Project",
        "rootPaths": ["/workspace/some-other"],
        "createdAt": 100,
        "updatedAt": 200,
    }
    state["projectless-thread-ids"].remove("thread-conflict")
    state["sidebar-project-thread-orders"]["existing-project"]["sortKey"] = "updated_at"
    return state


class StoreHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_file = self.root / ".codex-global-state.json"
        self.backup_file = self.root / ".codex-global-state.json.bak"
        self.backup_root = self.root / "tool-backups"
        self.proc_root = self.root / "proc"
        self.proc_root.mkdir()
        # Planner tests deliberately include an inconsistent assignment to
        # exercise conflict repair. NativeStateStore starts from a state that
        # already satisfies the stricter on-disk referential invariants.
        self.initial_state = valid_native_state()
        self.initial_payload = encode_state(self.initial_state)
        self.state_file.write_bytes(self.initial_payload)
        self.backup_file.write_bytes(self.initial_payload)
        os.chmod(self.state_file, 0o600)
        os.chmod(self.backup_file, 0o600)
        self.store = NativeStateStore(
            self.state_file,
            backup_root=self.backup_root,
            proc_root=self.proc_root,
        )

    def build_plan(self, *, root: str = "/workspace/new", thread_id: str = "thread-new"):
        candidate = group(
            root,
            [session(thread_id, "2026-08-10T10:00:00Z", root)],
        )
        return build_import_plan(
            [candidate],
            self.store.snapshot().state,
            before_sha256=self.store.snapshot().sha256,
            as_of=AS_OF,
            timezone_name="Europe/Istanbul",
        )

    def write_current(self, state, *, sync_backup: bool = False) -> None:
        payload = encode_state(state)
        self.state_file.write_bytes(payload)
        if sync_backup:
            self.backup_file.write_bytes(payload)


class ValidationAndCompatibilityTests(unittest.TestCase):
    def test_validation_rejects_top_level_and_project_shape_errors(self) -> None:
        self.assertEqual(validate_native_state([]), ["top-level state must be a JSON object"])
        errors = validate_native_state(
            {
                "local-projects": {
                    "wrong-key": {"id": "different", "rootPaths": "not-a-list"},
                    "not-object": [],
                },
                "project-order": {},
            }
        )
        self.assertIn("project-order must be list", errors)
        self.assertIn("local-projects['wrong-key'].id does not match its key", errors)
        self.assertIn("local-projects['wrong-key'].rootPaths must be a list", errors)
        self.assertIn("local-projects['not-object'] must be an object", errors)

    def test_compatibility_has_tested_untested_and_incompatible_levels(self) -> None:
        state = valid_native_state()
        tested = classify_compatibility(state, TESTED_VERSION)
        self.assertTrue(tested.tested)
        self.assertEqual(tested.level, "tested")

        untested = classify_compatibility(state, "27.0.0")
        self.assertEqual(untested.level, "schema-compatible-untested")
        self.assertFalse(untested.tested)

        state["electron-completed-local-data-migration-ids"] = []
        incompatible = classify_compatibility(state, "27.0.0")
        self.assertEqual(incompatible.level, "incompatible")
        self.assertIn("missing native project schema marker", incompatible.reason)
        tested_build_without_marker = classify_compatibility(state, TESTED_VERSION)
        self.assertEqual(tested_build_without_marker.level, "incompatible")

    def test_load_snapshot_rejects_invalid_json_and_invalid_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(NativeStateError, "not valid UTF-8 JSON"):
                load_snapshot(path)
            path.write_text('{"project-order":{}}', encoding="utf-8")
            with self.assertRaisesRegex(NativeStateError, "project-order must be list"):
                load_snapshot(path)

    def test_validation_checks_references_duplicates_and_assignment_overlap(self) -> None:
        state = valid_native_state()
        state["project-order"].append("missing-project")
        state["projectless-thread-ids"].append("thread-already")
        state["sidebar-project-thread-orders"]["existing-project"]["threadIds"] = [
            "duplicate",
            "duplicate",
        ]
        errors = validate_native_state(state)
        self.assertIn("project-order references unknown local projects", errors)
        self.assertIn("assigned and projectless thread ids overlap", errors)
        self.assertIn("sidebar order for 'existing-project' contains duplicates", errors)

    def test_fake_proc_root_detects_executable_and_command_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            self.assertFalse(is_desktop_running(proc))
            process = proc / "999991"
            process.mkdir()
            (process / "cmdline").write_bytes(b"/usr/lib/chatgpt/ChatGPT\x00--flag\x00")
            self.assertTrue(is_desktop_running(proc))

            (process / "cmdline").write_bytes(b"unrelated\x00")
            (process / "exe").symlink_to("/usr/lib/chatgpt/ChatGPT")
            self.assertTrue(is_desktop_running(proc))


class ApplyTests(StoreHarness):
    def test_apply_creates_verifiable_backup_journal_and_identical_native_copies(self) -> None:
        plan = self.build_plan()

        backup = self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertEqual(self.store.snapshot().state, plan.after_state)
        self.assertEqual(self.state_file.read_bytes(), self.backup_file.read_bytes())
        self.assertEqual(stat.S_IMODE(self.state_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.backup_file.stat().st_mode), 0o600)
        self.assertEqual(self.store.backups(), [backup])
        self.assertEqual((backup / "state.json").read_bytes(), self.initial_payload)
        self.assertEqual((backup / "state.json.bak").read_bytes(), self.initial_payload)

        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["kind"], "pre-apply")
        self.assertEqual(manifest["state_sha256"], plan.before_sha256)
        self.assertEqual(manifest["details"]["after_sha256"], plan.after_sha256)
        journal = json.loads((backup / "mutation.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["before_sha256"], plan.before_sha256)
        self.assertEqual(journal["after_sha256"], plan.after_sha256)
        changed_paths = {tuple(item["path"]) for item in journal["changes"]}
        self.assertIn(("local-projects",), changed_paths)
        self.assertIn(("thread-project-assignments",), changed_paths)
        self.assertIn(
            ("electron-persisted-atom-state", "flat-project-sidebar-preferences-v1"),
            changed_paths,
        )

    def test_backup_tree_and_files_are_private(self) -> None:
        backup = self.store.apply(self.build_plan(), app_version=TESTED_VERSION)
        self.assertEqual(stat.S_IMODE(self.backup_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        for name in ("state.json", "state.json.bak", "manifest.json", "mutation.json"):
            with self.subTest(name=name):
                self.assertEqual(stat.S_IMODE((backup / name).stat().st_mode), 0o600)

    def test_compare_and_swap_stops_apply_if_state_changed_after_plan(self) -> None:
        plan = self.build_plan()
        changed = deepcopy(self.initial_state)
        changed["external-change"] = True
        self.write_current(changed, sync_backup=True)

        with self.assertRaisesRegex(NativeStateError, "state changed after the plan"):
            self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertEqual(self.store.snapshot().state, changed)
        self.assertEqual(self.store.backups(), [])

    def test_after_hash_mismatch_stops_before_backup_or_write(self) -> None:
        plan = self.build_plan()
        plan.after_sha256 = "0" * 64
        with self.assertRaisesRegex(NativeStateError, "after-state hash does not match"):
            self.store.apply(plan, app_version=TESTED_VERSION)
        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.store.backups(), [])

    def test_invalid_after_state_is_rejected_before_backup_or_write(self) -> None:
        plan = self.build_plan()
        created_id = plan.manifest["native_changes"]["create_projects"][0]["project_id"]
        plan.after_state["local-projects"][created_id]["id"] = "mismatched-id"
        plan.after_sha256 = native_state.sha256_bytes(encode_state(plan.after_state))
        with self.assertRaisesRegex(NativeStateError, "validation|after-state|planned native state"):
            self.store.apply(plan, app_version=TESTED_VERSION)
        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.store.backups(), [])

    def test_plan_cannot_mutate_state_outside_the_owned_allowlist(self) -> None:
        plan = self.build_plan()
        plan.after_state["unknown-top-level"] = {"unexpected": "mutation"}
        plan.after_sha256 = native_state.sha256_bytes(encode_state(plan.after_state))

        with self.assertRaisesRegex(NativeStateError, "outside the importer allowlist"):
            self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.store.backups(), [])

    def test_second_cas_check_preserves_a_change_racing_after_backup(self) -> None:
        plan = self.build_plan()
        original_create_backup = self.store._create_backup

        def create_backup_then_race(snapshot, state_backup_snapshot, *, kind, details=None):
            backup = original_create_backup(
                snapshot,
                state_backup_snapshot,
                kind=kind,
                details=details,
            )
            raced = deepcopy(snapshot.state)
            raced["external-race"] = "must-survive"
            self.write_current(raced)
            return backup

        with mock.patch.object(
            self.store,
            "_create_backup",
            side_effect=create_backup_then_race,
        ):
            with self.assertRaisesRegex(NativeStateError, "changed|race"):
                self.store.apply(plan, app_version=TESTED_VERSION)
        self.assertEqual(self.store.snapshot().state["external-race"], "must-survive")

    def test_running_desktop_guard_stops_before_backup_or_write(self) -> None:
        plan = self.build_plan()
        process = self.proc_root / "999992"
        process.mkdir()
        (process / "cmdline").write_bytes(b"/usr/lib/chatgpt/ChatGPT\x00")
        with self.assertRaisesRegex(NativeStateError, "Codex Desktop is running"):
            self.store.apply(plan, app_version=TESTED_VERSION)
        self.assertEqual(self.store.backups(), [])
        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)

    def test_untested_build_requires_explicit_override(self) -> None:
        plan = self.build_plan()
        with self.assertRaisesRegex(NativeStateError, "--allow-untested"):
            self.store.apply(plan, app_version="27.0.0")
        self.assertEqual(self.store.backups(), [])

        backup = self.store.apply(plan, app_version="27.0.0", allow_untested=True)
        self.assertTrue(backup.is_dir())
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["details"]["compatibility"], "schema-compatible-untested")

    def test_partial_write_failure_restores_both_native_files(self) -> None:
        plan = self.build_plan()
        real_atomic_write = native_state._atomic_write
        failed = False

        def fail_once_on_backup_write(path, payload, mode=None, uid=None, gid=None):
            nonlocal failed
            if Path(path) == self.state_file and not failed:
                failed = True
                raise OSError("synthetic second-write failure")
            return real_atomic_write(path, payload, mode, uid, gid)

        with mock.patch.object(native_state, "_atomic_write", side_effect=fail_once_on_backup_write):
            with self.assertRaisesRegex(OSError, "synthetic second-write failure"):
                self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.backup_file.read_bytes(), self.initial_payload)
        self.assertEqual(len(self.store.backups()), 1, "the recovery evidence must remain available")

    def test_keyboard_interrupt_between_writes_restores_both_native_files(self) -> None:
        plan = self.build_plan()
        real_atomic_write = native_state._atomic_write
        interrupted = False

        def interrupt_once_on_main_write(path, payload, mode=None, uid=None, gid=None):
            nonlocal interrupted
            if Path(path) == self.state_file and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return real_atomic_write(path, payload, mode, uid, gid)

        with mock.patch.object(native_state, "_atomic_write", side_effect=interrupt_once_on_main_write):
            with self.assertRaises(KeyboardInterrupt):
                self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.backup_file.read_bytes(), self.initial_payload)


class RollbackTests(StoreHarness):
    def test_surgical_rollback_restores_owned_paths_and_preserves_later_external_state(self) -> None:
        plan = self.build_plan()
        backup = self.store.apply(plan, app_version=TESTED_VERSION)
        current = self.store.snapshot().state
        current["external-post-apply"] = {"preserve": True}
        current["electron-persisted-atom-state"]["dynamic-unrelated-atom"] = 42
        self.write_current(current, sync_backup=True)

        selected, safety = self.store.rollback(backup.name, app_version=TESTED_VERSION)

        self.assertEqual(selected, backup)
        restored = self.store.snapshot().state
        expected = deepcopy(self.initial_state)
        expected["external-post-apply"] = {"preserve": True}
        expected["electron-persisted-atom-state"]["dynamic-unrelated-atom"] = 42
        self.assertEqual(restored, expected)
        self.assertEqual(self.state_file.read_bytes(), self.backup_file.read_bytes())
        safety_manifest = json.loads((safety / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(safety_manifest["kind"], "pre-rollback")
        self.assertEqual(safety_manifest["details"]["restore_backup_id"], backup.name)

    def test_rollback_stops_on_importer_owned_path_conflict_without_overwrite(self) -> None:
        plan = self.build_plan()
        backup = self.store.apply(plan, app_version=TESTED_VERSION)
        changed = self.store.snapshot().state
        changed["project-order"][0], changed["project-order"][1] = (
            changed["project-order"][1],
            changed["project-order"][0],
        )
        self.write_current(changed, sync_backup=True)
        raw_before_rollback = self.state_file.read_bytes()

        with self.assertRaisesRegex(NativeStateError, "importer-owned keys") as raised:
            self.store.rollback(backup.name, app_version=TESTED_VERSION)

        self.assertIn("project-order", str(raised.exception))
        self.assertEqual(self.state_file.read_bytes(), raw_before_rollback)
        self.assertEqual(self.store.backups(), [backup])

    def test_rollback_rejects_journal_path_outside_the_owned_allowlist(self) -> None:
        backup = self.store.apply(self.build_plan(), app_version=TESTED_VERSION)
        journal_path = backup / "mutation.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["changes"][0] = {
            "path": ["unknown-top-level"],
            "before": {"present": False},
            "after": {"present": True, "value": self.initial_state["unknown-top-level"]},
        }
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        raw_before = self.state_file.read_bytes()
        with self.assertRaisesRegex(NativeStateError, "journal|allow"):
            self.store.rollback(backup.name, app_version=TESTED_VERSION)
        self.assertEqual(self.state_file.read_bytes(), raw_before)

    def test_rollback_second_write_failure_restores_pre_rollback_pair(self) -> None:
        plan = self.build_plan()
        backup = self.store.apply(plan, app_version=TESTED_VERSION)
        pre_rollback_main = self.state_file.read_bytes()
        pre_rollback_backup = self.backup_file.read_bytes()
        real_atomic_write = native_state._atomic_write
        failed = False

        def fail_once_on_backup_write(path, payload, mode=None, uid=None, gid=None):
            nonlocal failed
            if Path(path) == self.state_file and not failed:
                failed = True
                raise OSError("synthetic rollback second-write failure")
            return real_atomic_write(path, payload, mode, uid, gid)

        with mock.patch.object(native_state, "_atomic_write", side_effect=fail_once_on_backup_write):
            with self.assertRaisesRegex(OSError, "synthetic rollback second-write failure"):
                self.store.rollback(backup.name, app_version=TESTED_VERSION)
        self.assertEqual(self.state_file.read_bytes(), pre_rollback_main)
        self.assertEqual(self.backup_file.read_bytes(), pre_rollback_backup)

    def test_rollback_keyboard_interrupt_restores_pre_rollback_pair(self) -> None:
        backup = self.store.apply(self.build_plan(), app_version=TESTED_VERSION)
        pre_rollback_main = self.state_file.read_bytes()
        pre_rollback_backup = self.backup_file.read_bytes()
        real_atomic_write = native_state._atomic_write
        interrupted = False

        def interrupt_once_on_main_write(path, payload, mode=None, uid=None, gid=None):
            nonlocal interrupted
            if Path(path) == self.state_file and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return real_atomic_write(path, payload, mode, uid, gid)

        with mock.patch.object(native_state, "_atomic_write", side_effect=interrupt_once_on_main_write):
            with self.assertRaises(KeyboardInterrupt):
                self.store.rollback(backup.name, app_version=TESTED_VERSION)

        self.assertEqual(self.state_file.read_bytes(), pre_rollback_main)
        self.assertEqual(self.backup_file.read_bytes(), pre_rollback_backup)

    def test_rollback_rejects_tampered_backup_and_path_traversal(self) -> None:
        plan = self.build_plan()
        backup = self.store.apply(plan, app_version=TESTED_VERSION)
        (backup / "state.json").write_bytes(b"{}")
        with self.assertRaisesRegex(NativeStateError, "backup|hash|validation|valid"):
            self.store.rollback(backup.name, app_version=TESTED_VERSION)
        with self.assertRaisesRegex(NativeStateError, "backup|single directory"):
            self.store.rollback("../outside", app_version=TESTED_VERSION)

    def test_rollback_rejects_tampered_backup_copy(self) -> None:
        backup = self.store.apply(self.build_plan(), app_version=TESTED_VERSION)
        (backup / "state.json.bak").write_bytes(b"tampered")
        with self.assertRaisesRegex(NativeStateError, "backup|hash|integrity|valid"):
            self.store.rollback(backup.name, app_version=TESTED_VERSION)

    def test_pre_rollback_safety_backup_cannot_be_selected_as_restore_source(self) -> None:
        backup = self.store.apply(self.build_plan(), app_version=TESTED_VERSION)
        _, safety = self.store.rollback(backup.name, app_version=TESTED_VERSION)
        with self.assertRaisesRegex(NativeStateError, "pre-apply"):
            self.store.rollback(safety.name, app_version=TESTED_VERSION)

    def test_running_desktop_guard_also_blocks_rollback(self) -> None:
        plan = self.build_plan()
        backup = self.store.apply(plan, app_version=TESTED_VERSION)
        process = self.proc_root / "999993"
        process.mkdir()
        (process / "cmdline").write_bytes(b"/usr/lib/chatgpt/ChatGPT\x00")
        with self.assertRaisesRegex(NativeStateError, "Codex Desktop is running"):
            self.store.rollback(backup.name, app_version=TESTED_VERSION)


if __name__ == "__main__":
    unittest.main()
