from __future__ import annotations

from datetime import datetime, timezone
import unittest

from codex_repo_import.planner import (
    PlannerError,
    UNIFIED_PROJECT_ORDER_KEY,
    UNLINKED_PROJECT_NAME,
    build_import_plan,
    encode_state,
    sha256_bytes,
)

from tests.helpers import cloned_state, group, session


AS_OF = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
UNLINKED_ROOT = "/workspace/unlinked-codex-chats"


def build(groups, state, **options):
    return build_import_plan(
        groups,
        state,
        before_sha256=sha256_bytes(encode_state(state)),
        as_of=AS_OF,
        timezone_name="Europe/Istanbul",
        **options,
    )


def project_id_for_root(plan, root: str) -> str:
    matches = [
        project_id
        for project_id, project in plan.after_state["local-projects"].items()
        if project.get("rootPaths") == [root]
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one Project for {root!r}, got {matches!r}")
    return matches[0]


def add_projectless(state, *thread_ids: str) -> None:
    state["projectless-thread-ids"].extend(thread_ids)


class UnlinkedSelectionTests(unittest.TestCase):
    def test_opt_in_merges_active_ambiguous_and_missing_sessions_only(self) -> None:
        state = cloned_state()
        add_projectless(
            state,
            "thread-normal",
            "thread-ambiguous",
            "thread-missing",
            "thread-unresolved",
        )
        normal_root = "/workspace/normal"
        groups = [
            group(
                "/home/example/Desktop",
                [session("thread-ambiguous", "2026-08-11T10:00:00Z", "/home/example/Desktop")],
                eligible=False,
                kind="ambiguous",
                reason="root is too broad for automatic native project creation",
            ),
            group(
                normal_root,
                [session("thread-normal", "2026-08-12T10:00:00Z", normal_root)],
            ),
            group(
                "/old/moved-repository",
                [session("thread-missing", "2026-08-11T09:00:00Z", "/old/moved-repository")],
                eligible=False,
                kind="missing",
                reason="directory does not exist",
            ),
            group(
                "relative-or-invalid",
                [session("thread-unresolved", "2026-08-11T08:00:00Z", "relative-or-invalid")],
                eligible=False,
                kind="unresolved",
                reason="path is not absolute",
            ),
        ]

        plan = build(groups, state, unlinked_project_root=UNLINKED_ROOT)

        unlinked_id = project_id_for_root(plan, UNLINKED_ROOT)
        normal_id = project_id_for_root(plan, normal_root)
        self.assertEqual(
            plan.after_state["local-projects"][unlinked_id]["name"],
            "Unlinked Codex Chats",
        )
        self.assertEqual(
            plan.after_state["thread-project-assignments"]["thread-ambiguous"]["projectId"],
            unlinked_id,
        )
        self.assertEqual(
            plan.after_state["thread-project-assignments"]["thread-missing"]["projectId"],
            unlinked_id,
        )
        self.assertEqual(
            plan.after_state["thread-project-assignments"]["thread-normal"]["projectId"],
            normal_id,
        )
        self.assertNotIn("thread-unresolved", plan.after_state["thread-project-assignments"])
        self.assertIn("thread-unresolved", plan.after_state["projectless-thread-ids"])
        self.assertEqual(
            plan.after_state["sidebar-project-thread-orders"][unlinked_id]["threadIds"],
            ["thread-ambiguous", "thread-missing"],
        )
        self.assertEqual(
            [item["root"] for item in plan.manifest["projects"]],
            [item.root.root for item in groups],
            "the synthetic fallback must not replace original source groups in the manifest",
        )
        self.assertEqual(
            plan.after_state["project-order"][-1],
            unlinked_id,
            "a newly created fallback must remain outside the weighted ranking",
        )

    def test_without_opt_in_all_ineligible_sessions_remain_untouched(self) -> None:
        state = cloned_state()
        add_projectless(state, "thread-ambiguous", "thread-missing")
        groups = [
            group(
                "/home/example/Desktop",
                [session("thread-ambiguous", "2026-08-11T10:00:00Z", "/home/example/Desktop")],
                eligible=False,
                kind="ambiguous",
            ),
            group(
                "/old/missing",
                [session("thread-missing", "2026-08-10T10:00:00Z", "/old/missing")],
                eligible=False,
                kind="missing",
            ),
        ]

        plan = build(groups, state)

        self.assertEqual(plan.after_state, state)
        self.assertEqual(plan.manifest["native_changes"]["create_projects"], [])
        self.assertEqual(plan.manifest["summary"]["thread_assignments_to_write"], 0)

    def test_archived_unlinked_sessions_are_excluded_by_default(self) -> None:
        state = cloned_state()
        add_projectless(state, "thread-active-unlinked", "thread-archived-unlinked")
        candidate = group(
            "/old/missing",
            [
                session("thread-active-unlinked", "2026-08-10T10:00:00Z", "/old/missing"),
                session(
                    "thread-archived-unlinked",
                    "2026-08-11T10:00:00Z",
                    "/old/missing",
                    archived=True,
                ),
            ],
            eligible=False,
            kind="missing",
        )

        default_plan = build([candidate], state, unlinked_project_root=UNLINKED_ROOT)
        inclusive_plan = build(
            [candidate],
            state,
            unlinked_project_root=UNLINKED_ROOT,
            include_archived=True,
        )
        default_id = project_id_for_root(default_plan, UNLINKED_ROOT)
        inclusive_id = project_id_for_root(inclusive_plan, UNLINKED_ROOT)

        self.assertEqual(
            default_plan.after_state["sidebar-project-thread-orders"][default_id]["threadIds"],
            ["thread-active-unlinked"],
        )
        self.assertIn(
            "thread-archived-unlinked",
            default_plan.after_state["projectless-thread-ids"],
        )
        self.assertEqual(
            inclusive_plan.after_state["sidebar-project-thread-orders"][inclusive_id]["threadIds"],
            ["thread-archived-unlinked", "thread-active-unlinked"],
        )


class UnlinkedSafetyAndOrderingTests(unittest.TestCase):
    def test_reused_fallback_keeps_prior_slot_while_normal_projects_are_reranked(self) -> None:
        state = cloned_state()
        state["local-projects"]["other-project"] = {
            "id": "other-project",
            "name": "Other Project",
            "rootPaths": ["/workspace/other"],
        }
        state["local-projects"]["fallback-project"] = {
            "id": "fallback-project",
            "name": UNLINKED_PROJECT_NAME,
            "rootPaths": [UNLINKED_ROOT],
        }
        state["project-order"] = [
            "other-project",
            "fallback-project",
            "existing-project",
        ]
        state["thread-project-assignments"].update(
            {
                "thread-other": {
                    "projectKind": "local",
                    "projectId": "other-project",
                    "pendingCoreUpdate": False,
                },
                "thread-unlinked": {
                    "projectKind": "local",
                    "projectId": "fallback-project",
                    "pendingCoreUpdate": False,
                },
            }
        )
        ranked_existing = group(
            "/workspace/existing",
            [session("thread-already", "2026-08-12T10:00:00Z", "/workspace/existing")],
        )
        ranked_other = group(
            "/workspace/other",
            [session("thread-other", "2026-08-11T10:00:00Z", "/workspace/other")],
        )
        fallback_candidate = group(
            "/old/missing",
            [session("thread-unlinked", "2026-08-10T10:00:00Z", "/old/missing")],
            eligible=False,
            kind="missing",
        )

        plan = build(
            [ranked_existing, ranked_other, fallback_candidate],
            state,
            unlinked_project_root=UNLINKED_ROOT,
        )

        self.assertEqual(
            plan.after_state["project-order"],
            ["existing-project", "fallback-project", "other-project"],
        )
        self.assertEqual(
            plan.after_state["electron-persisted-atom-state"][UNIFIED_PROJECT_ORDER_KEY],
            [
                "codex:project:existing-project",
                "codex:project:fallback-project",
                "codex:project:other-project",
                "chatgpt:project:remote-one",
            ],
        )

    def test_existing_fallback_keeps_its_slot_when_no_candidate_is_assignable(self) -> None:
        state = cloned_state()
        state["local-projects"]["fallback-project"] = {
            "id": "fallback-project",
            "name": UNLINKED_PROJECT_NAME,
            "rootPaths": [UNLINKED_ROOT],
        }
        state["project-order"] = ["fallback-project", "existing-project"]
        state["thread-project-assignments"]["thread-conflict"] = {
            "projectKind": "local",
            "projectId": "existing-project",
            "pendingCoreUpdate": False,
        }
        ranked = group(
            "/workspace/existing",
            [session("thread-already", "2026-08-12T10:00:00Z", "/workspace/existing")],
        )
        candidate = group(
            "/old/missing",
            [session("thread-conflict", "2026-08-11T10:00:00Z", "/old/missing")],
            eligible=False,
            kind="missing",
        )

        plan = build(
            [ranked, candidate],
            state,
            unlinked_project_root=UNLINKED_ROOT,
        )

        self.assertEqual(
            plan.after_state["project-order"],
            ["fallback-project", "existing-project"],
        )
        self.assertEqual(plan.manifest["summary"]["unlinked_chats_assignable"], 0)

    def test_fallback_reuse_requires_fixed_name_and_exactly_one_exact_root(self) -> None:
        candidate = group(
            "/old/missing",
            [session("thread-unlinked", "2026-08-11T10:00:00Z", "/old/missing")],
            eligible=False,
            kind="missing",
        )
        invalid_projects = [
            {
                "id": "fallback-project",
                "name": "Anything Else",
                "rootPaths": [UNLINKED_ROOT],
            },
            {
                "id": "fallback-project",
                "name": UNLINKED_PROJECT_NAME,
                "rootPaths": [UNLINKED_ROOT, "/workspace/also-owned"],
            },
        ]

        for invalid_project in invalid_projects:
            with self.subTest(project=invalid_project):
                state = cloned_state()
                add_projectless(state, "thread-unlinked")
                state["local-projects"]["fallback-project"] = invalid_project
                state["project-order"].append("fallback-project")

                with self.assertRaisesRegex(PlannerError, "exact fixed name"):
                    build(
                        [candidate],
                        state,
                        unlinked_project_root=UNLINKED_ROOT,
                    )

    def test_fallback_rejects_duplicate_or_same_named_projects_on_another_root(self) -> None:
        candidate = group(
            "/old/missing",
            [session("thread-unlinked", "2026-08-11T10:00:00Z", "/old/missing")],
            eligible=False,
            kind="missing",
        )
        for shape in ("duplicate-root", "same-name-other-root"):
            with self.subTest(shape=shape):
                state = cloned_state()
                add_projectless(state, "thread-unlinked")
                state["local-projects"]["fallback-one"] = {
                    "id": "fallback-one",
                    "name": UNLINKED_PROJECT_NAME,
                    "rootPaths": [UNLINKED_ROOT],
                }
                state["local-projects"]["fallback-two"] = {
                    "id": "fallback-two",
                    "name": UNLINKED_PROJECT_NAME,
                    "rootPaths": [
                        UNLINKED_ROOT
                        if shape == "duplicate-root"
                        else "/workspace/other-fallback"
                    ],
                }

                with self.assertRaisesRegex(PlannerError, "duplicate|another root"):
                    build(
                        [candidate],
                        state,
                        unlinked_project_root=UNLINKED_ROOT,
                    )

    def test_manual_fallback_only_atom_change_is_exposed_in_manifest(self) -> None:
        state = cloned_state()
        state["electron-persisted-atom-state"][
            "flat-project-sidebar-preferences-v1"
        ]["projectSortMode"] = "manual"
        add_projectless(state, "thread-unlinked")
        candidate = group(
            "/old/missing",
            [session("thread-unlinked", "2026-08-11T10:00:00Z", "/old/missing")],
            eligible=False,
            kind="missing",
        )

        plan = build([candidate], state, unlinked_project_root=UNLINKED_ROOT)

        after_unified = plan.after_state["electron-persisted-atom-state"][
            UNIFIED_PROJECT_ORDER_KEY
        ]
        self.assertEqual(
            plan.manifest["native_changes"]["unified_project_order"],
            after_unified,
        )
        self.assertTrue(after_unified[-1].startswith("codex:project:"))

    def test_summary_distinguishes_candidates_from_assignable_chats(self) -> None:
        state = cloned_state()
        add_projectless(state, "thread-assignable", "thread-catalog-missing")
        candidate = group(
            "/old/missing",
            [
                session("thread-assignable", "2026-08-12T10:00:00Z", "/old/missing"),
                session("thread-conflict", "2026-08-11T10:00:00Z", "/old/missing"),
                session(
                    "thread-catalog-missing",
                    "2026-08-10T10:00:00Z",
                    "/old/missing",
                ),
            ],
            eligible=False,
            kind="missing",
        )

        plan = build(
            [candidate],
            state,
            unlinked_project_root=UNLINKED_ROOT,
            known_thread_ids={"thread-assignable", "thread-conflict"},
        )
        summary = plan.manifest["summary"]

        self.assertEqual(summary["unlinked_chats_candidates"], 3)
        self.assertEqual(summary["unlinked_chats_assignable"], 1)
        self.assertEqual(
            summary["unlinked_chats_selected"],
            3,
            "the legacy CLI summary key remains candidate-compatible",
        )

    def test_reassign_never_steals_an_existing_assignment_into_unlinked(self) -> None:
        state = cloned_state()
        state["thread-project-assignments"]["thread-conflict"] = {
            "projectKind": "local",
            "projectId": "existing-project",
            "pendingCoreUpdate": False,
        }
        candidate = group(
            "/old/missing",
            [session("thread-conflict", "2026-08-11T10:00:00Z", "/old/missing")],
            eligible=False,
            kind="missing",
        )

        plan = build(
            [candidate],
            state,
            unlinked_project_root=UNLINKED_ROOT,
            reassign=True,
        )

        self.assertEqual(
            plan.after_state["thread-project-assignments"]["thread-conflict"]["projectId"],
            "existing-project",
        )
        self.assertEqual(plan.manifest["summary"]["thread_assignment_conflicts"], 1)
        self.assertEqual(plan.manifest["summary"]["thread_assignments_to_write"], 0)
        self.assertFalse(
            any(
                project.get("rootPaths") == [UNLINKED_ROOT]
                for project in plan.after_state["local-projects"].values()
            ),
            "a fallback Project must not be created when every candidate is conflicted",
        )

    def test_catalog_filtering_never_mutates_a_missing_thread(self) -> None:
        state = cloned_state()
        add_projectless(state, "catalog-known", "catalog-missing")
        candidate = group(
            "/old/missing",
            [
                session("catalog-known", "2026-08-11T10:00:00Z", "/old/missing"),
                session("catalog-missing", "2026-08-10T10:00:00Z", "/old/missing"),
            ],
            eligible=False,
            kind="missing",
        )

        plan = build(
            [candidate],
            state,
            unlinked_project_root=UNLINKED_ROOT,
            known_thread_ids={"catalog-known"},
        )
        unlinked_id = project_id_for_root(plan, UNLINKED_ROOT)

        self.assertIn("catalog-known", plan.after_state["thread-project-assignments"])
        self.assertNotIn("catalog-missing", plan.after_state["thread-project-assignments"])
        self.assertIn("catalog-missing", plan.after_state["projectless-thread-ids"])
        self.assertEqual(
            plan.after_state["sidebar-project-thread-orders"][unlinked_id]["threadIds"],
            ["catalog-known"],
        )
        self.assertEqual(
            plan.manifest["native_changes"]["catalog_missing_thread_ids"],
            ["catalog-missing"],
        )

    def test_merged_thread_order_is_deterministic_across_source_group_order(self) -> None:
        state = cloned_state()
        add_projectless(state, "thread-b", "thread-a")
        first = group(
            "/old/one",
            [session("thread-b", "2026-08-11T10:00:00Z", "/old/one")],
            eligible=False,
            kind="missing",
        )
        second = group(
            "/home/example/Desktop",
            [session("thread-a", "2026-08-11T10:00:00Z", "/home/example/Desktop")],
            eligible=False,
            kind="ambiguous",
        )

        forward = build([first, second], state, unlinked_project_root=UNLINKED_ROOT)
        reverse = build([second, first], state, unlinked_project_root=UNLINKED_ROOT)
        forward_id = project_id_for_root(forward, UNLINKED_ROOT)
        reverse_id = project_id_for_root(reverse, UNLINKED_ROOT)

        self.assertEqual(forward_id, reverse_id)
        self.assertEqual(
            forward.after_state["sidebar-project-thread-orders"][forward_id]["threadIds"],
            ["thread-a", "thread-b"],
        )
        self.assertEqual(forward.after_state, reverse.after_state)

    def test_unlinked_project_is_last_and_does_not_activate_weighted_sort_by_itself(self) -> None:
        state = cloned_state()
        add_projectless(state, "thread-unlinked")
        candidate = group(
            "/old/missing",
            [session("thread-unlinked", "2026-08-11T10:00:00Z", "/old/missing")],
            eligible=False,
            kind="missing",
        )

        plan = build([candidate], state, unlinked_project_root=UNLINKED_ROOT)
        unlinked_id = project_id_for_root(plan, UNLINKED_ROOT)

        self.assertEqual(plan.after_state["project-order"][-1], unlinked_id)
        self.assertEqual(
            plan.after_state["electron-persisted-atom-state"],
            state["electron-persisted-atom-state"],
            "an unranked fallback alone must not force weighted manual sorting",
        )
        self.assertIsNone(plan.manifest["native_changes"]["sidebar_preference_change"])

    def test_second_identical_plan_reuses_fallback_and_is_byte_idempotent(self) -> None:
        state = cloned_state()
        add_projectless(state, "thread-unlinked")
        candidate = group(
            "/old/missing",
            [session("thread-unlinked", "2026-08-11T10:00:00Z", "/old/missing")],
            eligible=False,
            kind="missing",
        )
        first = build([candidate], state, unlinked_project_root=UNLINKED_ROOT)

        second = build(
            [candidate],
            first.after_state,
            unlinked_project_root=UNLINKED_ROOT,
        )

        self.assertEqual(second.after_state, first.after_state)
        self.assertEqual(second.before_sha256, second.after_sha256)
        self.assertEqual(second.manifest["summary"]["thread_assignments_to_write"], 0)
        self.assertEqual(second.manifest["native_changes"]["assignment_unchanged"], 1)
        self.assertEqual(second.manifest["native_changes"]["create_projects"], [])
        self.assertEqual(len(second.manifest["native_changes"]["reuse_projects"]), 1)


if __name__ == "__main__":
    unittest.main()
