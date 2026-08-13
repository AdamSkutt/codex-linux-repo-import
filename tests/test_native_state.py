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
    MANAGED_DIRECTORY_KIND,
    NativeStateError,
    NativeStateStore,
    classify_compatibility,
    is_desktop_running,
    load_snapshot,
    validate_native_state,
)
from codex_repo_import.planner import UNLINKED_PROJECT_NAME, build_import_plan, encode_state

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
        self.home = self.root / "home"
        self.desktop = self.home / "Desktop"
        self.desktop.mkdir(parents=True)
        os.chmod(self.home, 0o700)
        os.chmod(self.desktop, 0o700)
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
            home=self.home,
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

    def build_managed_plan(self, *, name: str = UNLINKED_PROJECT_NAME):
        target = self.desktop / name
        missing_root = "/old/missing-unlinked-workspace"
        candidate = group(
            missing_root,
            [session("thread-unlinked", "2026-08-10T10:00:00Z", missing_root)],
            eligible=False,
            kind="missing",
            reason="directory does not exist",
        )
        snapshot = self.store.snapshot()
        plan = build_import_plan(
            [candidate],
            snapshot.state,
            before_sha256=snapshot.sha256,
            as_of=AS_OF,
            timezone_name="Europe/Istanbul",
            unlinked_project_root=str(target),
        )
        return plan, target


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

    def test_managed_directory_plan_is_read_only_and_apply_creates_empty_private_folder(self) -> None:
        plan, target = self.build_managed_plan()
        self.assertFalse(target.exists(), "building a plan must not create the fallback directory")

        backup = self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertTrue(target.is_dir())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((backup / "external-actions.json").exists())
        self.assertFalse((backup / "external-cleanup.json").exists())
        projects = plan.after_state["local-projects"]
        linked = [
            project
            for project in projects.values()
            if project.get("rootPaths") == [str(target)]
        ]
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["name"], UNLINKED_PROJECT_NAME)

    def test_managed_directory_second_cas_race_preserves_folder_and_external_state(self) -> None:
        plan, target = self.build_managed_plan()
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

        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.store.snapshot().state["external-race"], "must-survive")

    def test_managed_directory_native_write_failure_restores_state_and_preserves_folder(self) -> None:
        plan, target = self.build_managed_plan()
        real_atomic_write = native_state._atomic_write
        failed = False

        def fail_once_on_main(path, payload, mode=None, uid=None, gid=None):
            nonlocal failed
            if Path(path) == self.state_file and not failed:
                failed = True
                raise OSError("synthetic managed commit failure")
            return real_atomic_write(path, payload, mode, uid, gid)

        with mock.patch.object(native_state, "_atomic_write", side_effect=fail_once_on_main):
            with self.assertRaisesRegex(OSError, "synthetic managed commit failure"):
                self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertTrue(target.is_dir())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.backup_file.read_bytes(), self.initial_payload)

    def test_crash_after_mkdir_preserves_folder_and_fresh_plan_adopts_it(self) -> None:
        plan, target = self.build_managed_plan()

        def crash_after_mkdir(expected_main, expected_backup):
            self.assertTrue(target.is_dir())
            raise KeyboardInterrupt

        with mock.patch.object(self.store, "_assert_unchanged", side_effect=crash_after_mkdir):
            with self.assertRaises(KeyboardInterrupt):
                self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertTrue(target.is_dir())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.backup_file.read_bytes(), self.initial_payload)

        fresh_plan, fresh_target = self.build_managed_plan()
        self.assertEqual(fresh_target, target)
        self.assertEqual(
            fresh_plan.manifest["external_changes"]["managed_directories"],
            [],
        )
        self.store.apply(fresh_plan, app_version=TESTED_VERSION)
        self.assertEqual(self.store.snapshot().state, fresh_plan.after_state)
        self.assertTrue(target.is_dir())

    def test_managed_directory_keyboard_interrupt_restores_and_preserves_folder(self) -> None:
        plan, target = self.build_managed_plan()
        real_atomic_write = native_state._atomic_write
        interrupted = False

        def interrupt_once_on_main(path, payload, mode=None, uid=None, gid=None):
            nonlocal interrupted
            if Path(path) == self.state_file and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return real_atomic_write(path, payload, mode, uid, gid)

        with mock.patch.object(native_state, "_atomic_write", side_effect=interrupt_once_on_main):
            with self.assertRaises(KeyboardInterrupt):
                self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.backup_file.read_bytes(), self.initial_payload)

    def test_stale_creation_plan_rejects_directory_file_and_symlink_collisions(self) -> None:
        for collision_kind in ("directory", "file", "symlink"):
            with self.subTest(collision_kind=collision_kind):
                plan, target = self.build_managed_plan(name=f"collision-{collision_kind}")
                if collision_kind == "directory":
                    target.mkdir()
                elif collision_kind == "file":
                    target.write_text("user-owned", encoding="utf-8")
                else:
                    destination = self.desktop / "foreign-directory"
                    destination.mkdir(exist_ok=True)
                    target.symlink_to(destination, target_is_directory=True)

                with self.assertRaisesRegex(NativeStateError, "collides"):
                    self.store.apply(plan, app_version=TESTED_VERSION)

                self.assertTrue(target.exists())
                self.assertEqual(self.store.backups(), [])

    def test_preexisting_directory_symlink_swap_before_commit_is_rejected(self) -> None:
        plan, target = self.build_managed_plan()
        target.mkdir(mode=0o700)
        plan, _ = self.build_managed_plan()
        saved = self.desktop / "saved-user-directory"
        original_assert = self.store._assert_unchanged

        def assert_then_swap(expected_main, expected_backup):
            original_assert(expected_main, expected_backup)
            target.rename(saved)
            target.symlink_to(saved, target_is_directory=True)

        with mock.patch.object(
            self.store,
            "_assert_unchanged",
            side_effect=assert_then_swap,
        ):
            with self.assertRaisesRegex(NativeStateError, "symlink|changed"):
                self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertTrue(target.is_symlink())
        self.assertTrue(saved.is_dir())
        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)

    def test_first_fallback_rejects_a_preexisting_nonempty_directory(self) -> None:
        plan, target = self.build_managed_plan()
        target.mkdir(mode=0o700)
        user_file = target / "user-file.txt"
        user_file.write_text("keep", encoding="utf-8")
        plan, _ = self.build_managed_plan()

        with self.assertRaisesRegex(NativeStateError, "must remain empty"):
            self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep")
        self.assertTrue(target.is_dir())
        self.assertEqual(self.store.backups(), [])
        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.backup_file.read_bytes(), self.initial_payload)

    def test_first_fallback_rechecks_preexisting_directory_emptiness_before_commit(self) -> None:
        plan, target = self.build_managed_plan()
        target.mkdir(mode=0o700)
        plan, _ = self.build_managed_plan()
        user_file = target / "raced-user-file.txt"
        original_assert = self.store._assert_unchanged

        def assert_then_add_user_file(expected_main, expected_backup):
            original_assert(expected_main, expected_backup)
            user_file.write_text("keep", encoding="utf-8")

        with mock.patch.object(
            self.store,
            "_assert_unchanged",
            side_effect=assert_then_add_user_file,
        ):
            with self.assertRaisesRegex(NativeStateError, "must remain empty"):
                self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep")
        self.assertTrue(target.is_dir())
        self.assertEqual(len(self.store.backups()), 1)
        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.backup_file.read_bytes(), self.initial_payload)

    def test_existing_native_unlinked_project_allows_nonempty_root(self) -> None:
        target = self.desktop / UNLINKED_PROJECT_NAME
        target.mkdir(mode=0o700)
        user_file = target / "existing-content.txt"
        user_file.write_text("keep", encoding="utf-8")
        existing_project_id = "existing-unlinked-project"
        current = deepcopy(self.initial_state)
        current["local-projects"][existing_project_id] = {
            "id": existing_project_id,
            "name": UNLINKED_PROJECT_NAME,
            "rootPaths": [str(target)],
            "createdAt": 100,
            "updatedAt": 200,
        }
        current["project-order"].append(existing_project_id)
        current["sidebar-project-thread-orders"][existing_project_id] = {
            "threadIds": [],
        }
        self.write_current(current, sync_backup=True)
        plan, planned_target = self.build_managed_plan()
        self.assertEqual(planned_target, target)

        backup = self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep")
        self.assertTrue(target.is_dir())
        self.assertFalse((backup / "external-actions.json").exists())

    def test_post_commit_content_race_restores_native_and_preserves_content(self) -> None:
        plan, target = self.build_managed_plan()
        original_reverify = self.store._reverify_managed_directory
        calls = 0

        def add_content_on_post_write(verified):
            nonlocal calls
            calls += 1
            if calls == 2:
                (target / "keep.txt").write_text("do not delete", encoding="utf-8")
            return original_reverify(verified)

        with mock.patch.object(
            self.store,
            "_reverify_managed_directory",
            side_effect=add_content_on_post_write,
        ):
            with self.assertRaisesRegex(NativeStateError, "must remain empty"):
                self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "do not delete")
        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.backup_file.read_bytes(), self.initial_payload)

    def test_post_commit_directory_swap_restores_native_and_preserves_both_directories(self) -> None:
        plan, target = self.build_managed_plan()
        original_reverify = self.store._reverify_managed_directory
        saved = self.desktop / "preserved-original-fallback"
        calls = 0

        def swap_on_post_write(verified):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.rename(saved)
                target.mkdir(mode=0o700)
            return original_reverify(verified)

        with mock.patch.object(
            self.store,
            "_reverify_managed_directory",
            side_effect=swap_on_post_write,
        ):
            with self.assertRaisesRegex(NativeStateError, "identity|changed"):
                self.store.apply(plan, app_version=TESTED_VERSION)

        self.assertTrue(saved.is_dir())
        self.assertTrue(target.is_dir())
        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.backup_file.read_bytes(), self.initial_payload)

    def test_managed_directory_rejects_group_or_world_writable_boundaries(self) -> None:
        cases = ("home", "desktop", "target")
        for case in cases:
            with self.subTest(case=case):
                plan, target = self.build_managed_plan(name=f"unsafe-{case}")
                changed_path = self.home if case == "home" else self.desktop
                if case == "target":
                    target.mkdir(mode=0o700)
                    plan, _ = self.build_managed_plan(name=f"unsafe-{case}")
                    changed_path = target
                original_mode = stat.S_IMODE(changed_path.stat().st_mode)
                os.chmod(changed_path, original_mode | stat.S_IWGRP)
                try:
                    with self.assertRaisesRegex(NativeStateError, "group/world-writable"):
                        self.store.apply(plan, app_version=TESTED_VERSION)
                finally:
                    os.chmod(changed_path, original_mode)
                self.assertEqual(self.store.backups(), [])

    def test_managed_directory_manifest_parser_is_strict(self) -> None:
        mutations = (
            lambda entries: entries.append(deepcopy(entries[0])),
            lambda entries: entries[0].update({"extra": True}),
            lambda entries: entries[0].update({"mode": "0755"}),
            lambda entries: entries[0].update({"kind": "arbitrary-operation"}),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                plan, target = self.build_managed_plan()
                entries = plan.manifest["external_changes"]["managed_directories"]
                mutate(entries)
                with self.assertRaises(NativeStateError):
                    self.store.apply(plan, app_version=TESTED_VERSION)
                self.assertFalse(target.exists())
                self.assertEqual(self.store.backups(), [])

    def test_managed_directory_must_match_option_and_exact_fallback_link(self) -> None:
        plan, target = self.build_managed_plan()
        other = self.desktop / "Other Fallback"
        plan.manifest["external_changes"]["managed_directories"][0]["path"] = str(other)
        with self.assertRaisesRegex(NativeStateError, "does not match"):
            self.store.apply(plan, app_version=TESTED_VERSION)
        self.assertFalse(target.exists())
        self.assertFalse(other.exists())

    def test_multi_root_duplicate_and_casefold_fallback_linkages_are_rejected(self) -> None:
        for invalid_kind in (
            "multi-root",
            "duplicate",
            "casefold-collision",
            "normalized-alias",
        ):
            with self.subTest(invalid_kind=invalid_kind):
                plan, target = self.build_managed_plan()
                projects = plan.after_state["local-projects"]
                project_id = next(
                    project_id
                    for project_id, project in projects.items()
                    if project.get("rootPaths") == [str(target)]
                )
                if invalid_kind == "multi-root":
                    projects[project_id]["rootPaths"].append(str(self.desktop / "Other"))
                elif invalid_kind == "duplicate":
                    duplicate = deepcopy(projects[project_id])
                    duplicate["id"] = "duplicate-fallback-project"
                    projects[duplicate["id"]] = duplicate
                else:
                    alias_id = f"{invalid_kind}-fallback-project"
                    projects[alias_id] = {
                        "id": alias_id,
                        "name": (
                            UNLINKED_PROJECT_NAME.lower()
                            if invalid_kind == "casefold-collision"
                            else "Ordinary Project"
                        ),
                        "rootPaths": [
                            str(self.desktop / "Other")
                            if invalid_kind == "casefold-collision"
                            else f"{self.desktop}/detour/../{target.name}"
                        ],
                        "createdAt": 100,
                        "updatedAt": 200,
                    }
                plan.after_sha256 = native_state.sha256_bytes(encode_state(plan.after_state))
                with self.assertRaisesRegex(NativeStateError, "exactly one|duplicate|fixed name"):
                    self.store.apply(plan, app_version=TESTED_VERSION)
                self.assertFalse(target.exists())

    def test_managed_directory_creation_is_refused_for_root(self) -> None:
        plan, target = self.build_managed_plan()
        with mock.patch.object(native_state.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(NativeStateError, "refused for root"):
                self.store.apply(plan, app_version=TESTED_VERSION)
        self.assertFalse(target.exists())
        self.assertEqual(self.store.backups(), [])


class RollbackTests(StoreHarness):
    def test_managed_directory_rollback_restores_native_state_and_preserves_folder(self) -> None:
        plan, target = self.build_managed_plan()
        backup = self.store.apply(plan, app_version=TESTED_VERSION)
        self.assertTrue(target.is_dir())

        _, safety = self.store.rollback(backup.name, app_version=TESTED_VERSION)

        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual(self.backup_file.read_bytes(), self.initial_payload)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((safety / "external-cleanup.json").exists())

    def test_managed_directory_rollback_conflict_preserves_directory(self) -> None:
        plan, target = self.build_managed_plan()
        backup = self.store.apply(plan, app_version=TESTED_VERSION)
        changed = self.store.snapshot().state
        changed["project-order"] = list(reversed(changed["project-order"]))
        self.write_current(changed, sync_backup=True)

        with self.assertRaisesRegex(NativeStateError, "importer-owned keys"):
            self.store.rollback(backup.name, app_version=TESTED_VERSION)

        self.assertTrue(target.is_dir())

    def test_managed_directory_rollback_preserves_user_content_after_native_restore(self) -> None:
        plan, target = self.build_managed_plan()
        backup = self.store.apply(plan, app_version=TESTED_VERSION)
        (target / "keep.txt").write_text("user data", encoding="utf-8")

        _, safety = self.store.rollback(backup.name, app_version=TESTED_VERSION)

        self.assertEqual(self.state_file.read_bytes(), self.initial_payload)
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "user data")
        self.assertTrue(target.is_dir())
        self.assertFalse((safety / "external-cleanup.json").exists())

    def test_managed_directory_is_not_removed_when_native_rollback_fails(self) -> None:
        plan, target = self.build_managed_plan()
        backup = self.store.apply(plan, app_version=TESTED_VERSION)
        applied_payload = self.state_file.read_bytes()
        real_atomic_write = native_state._atomic_write
        failed = False

        def fail_once_on_main(path, payload, mode=None, uid=None, gid=None):
            nonlocal failed
            if Path(path) == self.state_file and not failed:
                failed = True
                raise OSError("synthetic managed rollback failure")
            return real_atomic_write(path, payload, mode, uid, gid)

        with mock.patch.object(native_state, "_atomic_write", side_effect=fail_once_on_main):
            with self.assertRaisesRegex(OSError, "synthetic managed rollback failure"):
                self.store.rollback(backup.name, app_version=TESTED_VERSION)

        self.assertEqual(self.state_file.read_bytes(), applied_payload)
        self.assertEqual(self.backup_file.read_bytes(), applied_payload)
        self.assertTrue(target.is_dir())

    def test_preexisting_verified_directory_is_never_removed(self) -> None:
        plan, target = self.build_managed_plan()
        target.mkdir(mode=0o755)
        plan, _ = self.build_managed_plan()

        backup = self.store.apply(plan, app_version=TESTED_VERSION)
        self.assertFalse((backup / "external-actions.json").exists())
        (target / "user-file.txt").write_text("keep", encoding="utf-8")

        self.store.rollback(backup.name, app_version=TESTED_VERSION)

        self.assertEqual((target / "user-file.txt").read_text(encoding="utf-8"), "keep")
        self.assertTrue(target.is_dir())

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
