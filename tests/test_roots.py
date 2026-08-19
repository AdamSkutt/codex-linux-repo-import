from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_repo_import.models import SessionRecord
from codex_repo_import.roots import (
    apply_path_mappings,
    group_sessions,
    parse_path_mapping,
    resolve_project_root,
)


def _git_failure(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="")


def _session(thread_id: str, cwd: Path) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        started_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        cwd=str(cwd),
        archived=False,
        rollout_path=Path(f"/rollouts/{thread_id}.jsonl"),
        originator="codex_vscode",
        provenance="vscode-extension",
    )


class PathMappingTests(unittest.TestCase):
    def test_longest_prefix_mapping_wins(self) -> None:
        mapped, changed = apply_path_mappings(
            "/old/repo/packages/client",
            (("/old", "/general"), ("/old/repo", "/specific")),
        )

        self.assertTrue(changed)
        self.assertEqual(mapped, "/specific/packages/client")

    def test_mapping_only_matches_a_path_component_boundary(self) -> None:
        mapped, changed = apply_path_mappings(
            "/old/repository",
            (("/old/repo", "/new/repo"),),
        )

        self.assertFalse(changed)
        self.assertEqual(mapped, "/old/repository")

    def test_exact_mapping_is_supported(self) -> None:
        mapped, changed = apply_path_mappings(
            "/old/repo",
            (("/old/repo", "/new/repo"),),
        )

        self.assertTrue(changed)
        self.assertEqual(mapped, "/new/repo")

    def test_parse_mapping_rejects_invalid_values(self) -> None:
        invalid_values = (
            "/old/without-a-destination",
            "=/new",
            "/old=",
            "relative=/absolute",
            "/absolute=relative",
        )

        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_path_mapping(value)


class RootResolutionTests(unittest.TestCase):
    def test_nested_directory_resolves_to_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            nested = repository / "packages" / "client"
            nested.mkdir(parents=True)
            (repository / ".git").mkdir()
            (repository / ".git" / "HEAD").write_text(
                "ref: refs/heads/main\n", encoding="utf-8"
            )

            with mock.patch(
                "codex_repo_import.roots.subprocess.run", side_effect=_git_failure
            ):
                resolved = resolve_project_root(str(nested))

        self.assertEqual(resolved.root, str(repository.resolve()))
        self.assertEqual(resolved.kind, "git")
        self.assertTrue(resolved.eligible_for_apply)

    def test_git_file_worktree_fallback_when_git_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            worktree = Path(temporary_directory) / "worktree"
            nested = worktree / "src" / "package"
            nested.mkdir(parents=True)
            (worktree / ".git").write_text(
                "gitdir: /tmp/main-repository/.git/worktrees/worktree\n",
                encoding="utf-8",
            )

            with mock.patch(
                "codex_repo_import.roots.subprocess.run",
                side_effect=FileNotFoundError("git is unavailable"),
            ):
                resolved = resolve_project_root(str(nested))

        self.assertEqual(resolved.root, str(worktree.resolve()))
        self.assertEqual(resolved.kind, "git")
        self.assertTrue(resolved.eligible_for_apply)

    def test_symlink_is_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            target = base / "real-workspace"
            target.mkdir()
            alias = base / "workspace-alias"
            try:
                alias.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")

            with mock.patch("codex_repo_import.roots._git_root", return_value=None):
                resolved = resolve_project_root(str(alias))

        self.assertEqual(resolved.root, str(target.resolve()))
        self.assertEqual(resolved.kind, "workspace")
        self.assertTrue(resolved.eligible_for_apply)

    def test_non_git_directory_remains_the_exact_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory) / "plain-workspace"
            workspace.mkdir()

            with mock.patch("codex_repo_import.roots._git_root", return_value=None):
                resolved = resolve_project_root(str(workspace))

        self.assertEqual(resolved.root, str(workspace.resolve()))
        self.assertEqual(resolved.kind, "workspace")
        self.assertTrue(resolved.eligible_for_apply)

    def test_empty_git_marker_is_not_treated_as_a_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            (base / ".git").mkdir()
            workspace = base / "plain-workspace"
            workspace.mkdir()

            with mock.patch(
                "codex_repo_import.roots.subprocess.run",
                side_effect=FileNotFoundError("git is unavailable"),
            ):
                resolved = resolve_project_root(str(workspace))

        self.assertEqual(resolved.root, str(workspace.resolve()))
        self.assertEqual(resolved.kind, "workspace")

    def test_missing_directory_is_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "moved-workspace"
            resolved = resolve_project_root(str(missing))

        self.assertEqual(resolved.root, str(missing))
        self.assertEqual(resolved.kind, "missing")
        self.assertFalse(resolved.eligible_for_apply)
        self.assertIn("does not exist", resolved.reason or "")

    def test_broad_roots_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            desktop = home / "Desktop"
            desktop.mkdir(parents=True)

            with mock.patch("codex_repo_import.roots._git_root", return_value=None):
                for candidate in (Path("/"), home, desktop):
                    with self.subTest(candidate=candidate):
                        resolved = resolve_project_root(str(candidate), home=home)
                        self.assertEqual(resolved.kind, "ambiguous")
                        self.assertFalse(resolved.eligible_for_apply)
                        self.assertIn("too broad", resolved.reason or "")


class SessionGroupingTests(unittest.TestCase):
    def test_cwd_aliases_for_the_same_git_root_are_grouped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            nested = repository / "packages" / "client"
            nested.mkdir(parents=True)
            (repository / ".git").mkdir()
            (repository / ".git" / "HEAD").write_text(
                "ref: refs/heads/main\n", encoding="utf-8"
            )
            sessions = (
                _session("thread-at-root", repository),
                _session("thread-in-package", nested),
            )

            with mock.patch(
                "codex_repo_import.roots.subprocess.run", side_effect=_git_failure
            ):
                groups = group_sessions(sessions)

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group.root.root, str(repository.resolve()))
        self.assertEqual(
            {session.thread_id for session in group.sessions},
            {"thread-at-root", "thread-in-package"},
        )
        self.assertEqual(group.cwd_aliases, {str(repository), str(nested)})


if __name__ == "__main__":
    unittest.main()
