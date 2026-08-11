from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import unittest
from uuid import NAMESPACE_URL, uuid5

from codex_repo_import.models import Diagnostic
from codex_repo_import.planner import (
    SIDEBAR_PREFERENCES_KEY,
    UNIFIED_PROJECT_ORDER_KEY,
    build_import_plan,
    encode_state,
    sha256_bytes,
)

from tests.helpers import cloned_state, group, session


AS_OF = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def build(groups, state, **options):
    return build_import_plan(
        groups,
        state,
        before_sha256=sha256_bytes(encode_state(state)),
        as_of=AS_OF,
        timezone_name="Europe/Istanbul",
        **options,
    )


def deterministic_id(root: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"https://github.com/AdamSkutt/codex-linux-repo-import/project/{root}",
        )
    )


class ProjectCreationTests(unittest.TestCase):
    def test_new_project_id_is_deterministic_and_unknown_state_is_preserved(self) -> None:
        state = cloned_state()
        original = deepcopy(state)
        root = "/workspace/new"
        candidate = group(root, [session("thread-new", "2026-08-10T10:00:00Z", root)])

        plan = build([candidate], state)

        project_id = deterministic_id(root)
        project = plan.after_state["local-projects"][project_id]
        self.assertEqual(
            project,
            {
                "id": project_id,
                "name": "new",
                "rootPaths": [root],
                "createdAt": int(AS_OF.timestamp() * 1000),
                "updatedAt": int(AS_OF.timestamp() * 1000),
            },
        )
        self.assertEqual(plan.after_state["unknown-top-level"], original["unknown-top-level"])
        self.assertEqual(
            plan.after_state["electron-persisted-atom-state"]["unrelated-atom"],
            original["electron-persisted-atom-state"]["unrelated-atom"],
        )
        self.assertEqual(state, original, "planning must not mutate its input")
        self.assertEqual(plan.after_sha256, sha256_bytes(encode_state(plan.after_state)))

    def test_existing_root_is_reused_after_lexical_normalization(self) -> None:
        state = cloned_state()
        candidate = group(
            "/workspace/./existing",
            [session("thread-new", "2026-08-10T10:00:00Z", "/workspace/existing")],
        )
        plan = build([candidate], state)
        native = plan.manifest["native_changes"]
        self.assertEqual(native["create_projects"], [])
        self.assertEqual(native["reuse_projects"][0]["project_id"], "existing-project")
        self.assertEqual(len(plan.after_state["local-projects"]), 1)

    def test_two_roots_of_one_native_project_merge_orders_without_duplicate_project_id(self) -> None:
        state = cloned_state()
        state["local-projects"]["existing-project"]["rootPaths"].append(
            "/workspace/existing-secondary"
        )
        primary = group(
            "/workspace/existing",
            [session("thread-already", "2026-08-10T10:00:00Z", "/workspace/existing")],
        )
        secondary = group(
            "/workspace/existing-secondary",
            [
                session(
                    "thread-secondary",
                    "2026-08-11T10:00:00Z",
                    "/workspace/existing-secondary",
                )
            ],
        )
        plan = build([primary, secondary], state)
        self.assertEqual(plan.after_state["project-order"].count("existing-project"), 1)
        self.assertEqual(
            plan.after_state["sidebar-project-thread-orders"]["existing-project"]["threadIds"],
            ["thread-secondary", "thread-already", "thread-old"],
        )
        self.assertEqual(
            plan.after_state["thread-project-assignments"]["thread-secondary"]["projectId"],
            "existing-project",
        )

    def test_duplicate_basename_labels_include_parent_context(self) -> None:
        state = cloned_state()
        one = group("/workspace/one/repo", [session("one", "2026-08-10T10:00:00Z", "/workspace/one/repo")])
        two = group("/workspace/two/repo", [session("two", "2026-08-09T10:00:00Z", "/workspace/two/repo")])
        plan = build([one, two], state)
        projects = plan.after_state["local-projects"]
        self.assertEqual(projects[deterministic_id("/workspace/one/repo")]["name"], "repo · one")
        self.assertEqual(projects[deterministic_id("/workspace/two/repo")]["name"], "repo · two")

    def test_ineligible_group_is_reported_but_never_created_or_assigned(self) -> None:
        state = cloned_state()
        candidate = group(
            "/workspace/missing",
            [session("thread-new", "2026-08-10T10:00:00Z", "/workspace/missing")],
            eligible=False,
            kind="missing",
            reason="directory does not exist",
        )
        plan = build([candidate], state)
        self.assertEqual(plan.manifest["summary"]["eligible_projects"], 0)
        self.assertEqual(plan.manifest["native_changes"]["create_projects"], [])
        self.assertNotIn(deterministic_id("/workspace/missing"), plan.after_state["local-projects"])
        self.assertNotIn("thread-new", plan.after_state["thread-project-assignments"])
        self.assertFalse(plan.manifest["projects"][0]["eligible_for_apply"])

    def test_all_conflicting_threads_do_not_create_an_empty_project(self) -> None:
        state = cloned_state()
        root = "/workspace/would-be-empty"
        candidate = group(
            root,
            [session("thread-conflict", "2026-08-10T10:00:00Z", root)],
        )

        plan = build([candidate], state)

        self.assertEqual(plan.manifest["summary"]["thread_assignment_conflicts"], 1)
        self.assertEqual(plan.manifest["native_changes"]["create_projects"], [])
        self.assertNotIn(deterministic_id(root), plan.after_state["local-projects"])


class AssignmentAndOrderingTests(unittest.TestCase):
    def test_ranked_project_and_thread_order_are_written_for_manual_sidebar_mode(self) -> None:
        state = cloned_state()
        new_root = "/workspace/new"
        new_group = group(
            new_root,
            [session("thread-new", "2026-08-11T10:00:00Z", new_root)],
        )
        existing_group = group(
            "/workspace/existing",
            [
                session("thread-already", "2026-08-10T10:00:00Z", "/workspace/existing"),
                session("thread-newer-existing", "2026-08-12T10:00:00Z", "/workspace/existing"),
            ],
        )

        plan = build([new_group, existing_group], state)
        new_id = deterministic_id(new_root)

        self.assertEqual(plan.after_state["project-order"], [new_id, "existing-project"])
        self.assertEqual(
            plan.after_state["sidebar-project-thread-orders"]["existing-project"]["threadIds"],
            ["thread-newer-existing", "thread-already", "thread-old"],
        )
        self.assertEqual(
            plan.after_state["sidebar-project-thread-orders"]["existing-project"]["sortKey"],
            "keep-me",
            "unknown per-project order fields must survive an import",
        )
        atoms = plan.after_state["electron-persisted-atom-state"]
        self.assertEqual(atoms[SIDEBAR_PREFERENCES_KEY]["projectSortMode"], "manual")
        self.assertEqual(
            atoms[UNIFIED_PROJECT_ORDER_KEY],
            [
                f"codex:project:{new_id}",
                "codex:project:existing-project",
                "chatgpt:project:remote-one",
            ],
        )
        self.assertNotIn("thread-new", plan.after_state["projectless-thread-ids"])
        self.assertIn("thread-unrelated", plan.after_state["projectless-thread-ids"])
        self.assertEqual(plan.manifest["native_changes"]["assignment_unchanged"], 1)

    def test_assignment_to_other_project_conflicts_without_reassign(self) -> None:
        state = cloned_state()
        candidate = group(
            "/workspace/existing",
            [session("thread-conflict", "2026-08-10T10:00:00Z", "/workspace/existing")],
        )
        plan = build([candidate], state)
        self.assertEqual(plan.manifest["summary"]["thread_assignment_conflicts"], 1)
        self.assertEqual(plan.manifest["summary"]["thread_assignments_to_write"], 0)
        self.assertEqual(
            plan.after_state["thread-project-assignments"]["thread-conflict"]["projectId"],
            "some-other-project",
        )
        self.assertNotIn(
            "thread-conflict",
            plan.after_state["projectless-thread-ids"],
            "a natively assigned thread must not remain in the projectless list",
        )

    def test_malformed_existing_assignment_conflicts_instead_of_being_overwritten(self) -> None:
        state = cloned_state()
        state["thread-project-assignments"]["thread-malformed"] = "corrupt-native-value"
        candidate = group(
            "/workspace/existing",
            [session("thread-malformed", "2026-08-10T10:00:00Z", "/workspace/existing")],
        )
        plan = build([candidate], state)
        self.assertEqual(plan.manifest["summary"]["thread_assignment_conflicts"], 1)
        self.assertEqual(
            plan.after_state["thread-project-assignments"]["thread-malformed"],
            "corrupt-native-value",
        )

    def test_reassign_option_moves_conflicting_thread_and_removes_projectless_id(self) -> None:
        state = cloned_state()
        candidate = group(
            "/workspace/existing",
            [session("thread-conflict", "2026-08-10T10:00:00Z", "/workspace/existing")],
        )
        plan = build([candidate], state, reassign=True)
        self.assertEqual(plan.manifest["summary"]["thread_assignment_conflicts"], 0)
        self.assertEqual(plan.manifest["summary"]["thread_assignments_to_write"], 1)
        self.assertEqual(
            plan.after_state["thread-project-assignments"]["thread-conflict"],
            {
                "projectKind": "local",
                "projectId": "existing-project",
                "pendingCoreUpdate": False,
            },
        )
        self.assertTrue(plan.manifest["native_changes"]["assignment_updates"][0]["reassigned"])
        self.assertNotIn("thread-conflict", plan.after_state["projectless-thread-ids"])

    def test_reassigned_thread_is_removed_from_previous_project_manual_order(self) -> None:
        state = cloned_state()
        state["sidebar-project-thread-orders"]["some-other-project"] = {
            "threadIds": ["keep-before", "thread-conflict", "keep-after"],
            "nativeFutureField": "preserve",
        }
        candidate = group(
            "/workspace/existing",
            [session("thread-conflict", "2026-08-10T10:00:00Z", "/workspace/existing")],
        )
        plan = build([candidate], state, reassign=True)
        previous_order = plan.after_state["sidebar-project-thread-orders"]["some-other-project"]
        self.assertEqual(previous_order["threadIds"], ["keep-before", "keep-after"])
        self.assertEqual(previous_order["nativeFutureField"], "preserve")
        self.assertEqual(
            plan.after_state["sidebar-project-thread-orders"]["existing-project"]["threadIds"][0],
            "thread-conflict",
        )

    def test_same_target_assignment_with_extra_fields_is_accepted_and_preserved(self) -> None:
        state = cloned_state()
        state["thread-project-assignments"]["thread-already"]["nativeFutureField"] = "preserve"
        candidate = group(
            "/workspace/existing",
            [session("thread-already", "2026-08-10T10:00:00Z", "/workspace/existing")],
        )
        plan = build([candidate], state)
        self.assertEqual(plan.manifest["summary"]["thread_assignment_conflicts"], 0)
        self.assertEqual(plan.manifest["native_changes"]["assignment_unchanged"], 1)
        self.assertEqual(
            plan.after_state["thread-project-assignments"]["thread-already"]["nativeFutureField"],
            "preserve",
        )

    def test_archived_threads_are_excluded_by_default_and_optional_on_request(self) -> None:
        state = cloned_state()
        root = "/workspace/archive"
        candidate = group(
            root,
            [
                session("thread-new", "2026-08-10T10:00:00Z", root),
                session("thread-archived", "2026-01-10T10:00:00Z", root, archived=True),
            ],
        )
        default_plan = build([candidate], state)
        inclusive_plan = build([candidate], state, include_archived=True)
        project_id = deterministic_id(root)
        self.assertEqual(
            default_plan.after_state["sidebar-project-thread-orders"][project_id]["threadIds"],
            ["thread-new"],
        )
        self.assertEqual(
            inclusive_plan.after_state["sidebar-project-thread-orders"][project_id]["threadIds"],
            ["thread-new", "thread-archived"],
        )
        self.assertIn("thread-archived", default_plan.after_state["projectless-thread-ids"])
        self.assertNotIn("thread-archived", inclusive_plan.after_state["projectless-thread-ids"])

    def test_preserve_native_sort_leaves_atom_state_byte_equivalent(self) -> None:
        state = cloned_state()
        atoms_before = deepcopy(state["electron-persisted-atom-state"])
        candidate = group(
            "/workspace/new",
            [session("thread-new", "2026-08-10T10:00:00Z", "/workspace/new")],
        )
        plan = build([candidate], state, activate_weighted_sort=False)
        self.assertEqual(plan.after_state["electron-persisted-atom-state"], atoms_before)
        self.assertIsNone(plan.manifest["native_changes"]["unified_project_order"])
        self.assertIsNone(plan.manifest["native_changes"]["sidebar_preference_change"])

    def test_threads_missing_from_native_catalog_are_reported_and_not_mutated(self) -> None:
        state = cloned_state()
        root = "/workspace/new"
        candidate = group(
            root,
            [
                session("catalog-known", "2026-08-10T10:00:00Z", root),
                session("catalog-missing", "2026-08-09T10:00:00Z", root),
            ],
        )
        plan = build([candidate], state, known_thread_ids={"catalog-known"})
        project_id = deterministic_id(root)
        self.assertIn("catalog-known", plan.after_state["thread-project-assignments"])
        self.assertNotIn("catalog-missing", plan.after_state["thread-project-assignments"])
        self.assertEqual(
            plan.after_state["sidebar-project-thread-orders"][project_id]["threadIds"],
            ["catalog-known"],
        )
        self.assertEqual(
            plan.manifest["native_changes"]["catalog_missing_thread_ids"],
            ["catalog-missing"],
        )
        self.assertEqual(plan.manifest["summary"]["threads_missing_from_native_catalog"], 1)

    def test_project_order_is_deduplicated_while_preserving_unranked_order(self) -> None:
        state = cloned_state()
        state["local-projects"]["other"] = {
            "id": "other",
            "name": "Other",
            "rootPaths": ["/workspace/other"],
        }
        state["project-order"] = ["existing-project", "existing-project", "other"]
        candidate = group(
            "/workspace/existing",
            [session("thread-already", "2026-08-10T10:00:00Z", "/workspace/existing")],
        )
        plan = build([candidate], state)
        self.assertEqual(plan.after_state["project-order"], ["existing-project", "other"])


class ManifestTests(unittest.TestCase):
    def test_manifest_is_privacy_explicit_and_includes_diagnostics(self) -> None:
        state = cloned_state()
        candidate = group(
            "/workspace/new",
            [session("thread-new", "2026-08-10T10:00:00Z", "/workspace/new")],
        )
        diagnostic = Diagnostic("synthetic", "fixture warning", "/tmp/fixture")
        plan = build([candidate], state, diagnostics=[diagnostic])
        self.assertFalse(plan.manifest["privacy"]["message_bodies_read"])
        self.assertFalse(plan.manifest["privacy"]["session_files_modified"])
        self.assertNotIn("threads", plan.manifest["projects"][0])
        self.assertNotIn("cwd_aliases", plan.manifest["projects"][0])
        self.assertNotIn("rollout", repr(plan.manifest["projects"][0]))
        self.assertEqual(plan.manifest["diagnostics"], [diagnostic.to_dict()])
        self.assertEqual(plan.manifest["state"]["before_sha256"], plan.before_sha256)
        self.assertEqual(plan.manifest["state"]["after_sha256"], plan.after_sha256)


if __name__ == "__main__":
    unittest.main()
