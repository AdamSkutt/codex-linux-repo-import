from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_repo_import import __version__
from codex_repo_import import cli


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTED_VERSION = "26.803.81509"
PRIVATE_MARKER = "PRIVATE-CLI-FIXTURE-MESSAGE-MUST-NOT-LEAK"


def _native_state(thread_ids: list[str]) -> dict[str, object]:
    return {
        "electron-completed-local-data-migration-ids": ["2026-07-13-local-projects"],
        "local-projects": {},
        "project-order": [],
        "thread-project-assignments": {},
        "projectless-thread-ids": list(thread_ids),
        "sidebar-project-thread-orders": {},
        "electron-persisted-atom-state": {
            "flat-project-sidebar-preferences-v1": {
                "chatSortMode": "priority",
                "initialized": True,
                "mode": "project",
                "projectSortMode": "priority",
            },
            "unified-sidebar-project-order-v1": [],
        },
        "fixture-unknown-key": {"preserve": True},
    }


def _write_rollout(
    directory: Path,
    *,
    filename: str,
    thread_id: str,
    timestamp: str,
    cwd: Path,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    meta = {
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "timestamp": timestamp,
            "cwd": str(cwd),
            "originator": "codex_vscode",
            "source": "vscode",
        },
    }
    message = {"type": "response_item", "payload": {"message": PRIVATE_MARKER}}
    (directory / filename).write_text(
        json.dumps(meta) + "\n" + json.dumps(message) + "\n",
        encoding="utf-8",
    )


class SyntheticCodexLayout:
    def __init__(self, root: Path, *, with_sessions: bool) -> None:
        self.home = root / "codex-home"
        self.sessions = self.home / "sessions"
        self.archived = self.home / "archived_sessions"
        self.project = root / "project-alpha"
        self.state_file = self.home / ".codex-global-state.json"
        self.state_backup_file = self.home / ".codex-global-state.json.bak"
        self.database = self.home / "state_5.sqlite"
        self.home.mkdir(parents=True)
        self.sessions.mkdir()
        self.archived.mkdir()
        self.project.mkdir()
        # Mark the fixture boundary explicitly so a test runner living under a
        # broader Git worktree cannot absorb this synthetic project.
        (self.project / ".git").mkdir()

        thread_ids = ["thread-active", "thread-archived"] if with_sessions else []
        state_payload = json.dumps(
            _native_state(thread_ids),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.state_file.write_bytes(state_payload)
        self.state_backup_file.write_bytes(state_payload)

        connection = sqlite3.connect(self.database)
        try:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO threads (id) VALUES (?)",
                [(thread_id,) for thread_id in thread_ids],
            )
            connection.commit()
        finally:
            connection.close()

        if with_sessions:
            _write_rollout(
                self.sessions / "2026" / "08",
                filename="rollout-active.jsonl",
                thread_id="thread-active",
                timestamp="2026-08-10T10:00:00Z",
                cwd=self.project,
            )
            _write_rollout(
                self.archived,
                filename="rollout-archived.jsonl",
                thread_id="thread-archived",
                timestamp="2026-07-01T10:00:00Z",
                cwd=self.project,
            )


class CliIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def run_cli(self, *arguments: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        current_pythonpath = environment.get("PYTHONPATH")
        source_path = str(REPOSITORY_ROOT / "src")
        environment["PYTHONPATH"] = (
            source_path
            if not current_pythonpath
            else source_path + os.pathsep + current_pythonpath
        )
        environment["CODEX_DESKTOP_VERSION"] = TESTED_VERSION
        return subprocess.run(
            [sys.executable, "-m", "codex_repo_import", *arguments],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )

    def test_help_and_version_exit_cleanly_through_module_entrypoint(self) -> None:
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("usage: codex-linux-repo-import", help_result.stdout)
        for command in ("doctor", "scan", "plan", "apply", "backups", "rollback"):
            self.assertIn(command, help_result.stdout)
        self.assertEqual(help_result.stderr, "")

        version_result = self.run_cli("--version")
        self.assertEqual(version_result.returncode, 0, version_result.stderr)
        self.assertEqual(
            version_result.stdout.strip(),
            f"codex-linux-repo-import {__version__}",
        )
        self.assertEqual(version_result.stderr, "")

    def test_scan_json_ranks_synthetic_project_without_reading_message_record(self) -> None:
        layout = SyntheticCodexLayout(self.root, with_sessions=True)
        state_before = hashlib.sha256(layout.state_file.read_bytes()).hexdigest()

        result = self.run_cli(
            "scan",
            "--codex-home",
            str(layout.home),
            "--timezone",
            "UTC",
            "--as-of",
            "2026-08-12T12:00:00Z",
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertTrue(payload["privacy"]["only_first_session_meta_record_read"])
        self.assertFalse(payload["privacy"]["message_records_read"])
        self.assertEqual(payload["diagnostics"], [])
        self.assertEqual(len(payload["projects"]), 1)
        project = payload["projects"][0]
        self.assertEqual(project["rank"], 1)
        self.assertEqual(project["root"], str(layout.project.resolve()))
        self.assertEqual(project["active_chats"], 1)
        self.assertEqual(project["archived_chats"], 1)
        self.assertTrue(project["eligible"])
        self.assertNotIn(PRIVATE_MARKER, result.stdout)
        self.assertEqual(hashlib.sha256(layout.state_file.read_bytes()).hexdigest(), state_before)

    def test_plan_json_uses_catalog_and_is_a_true_dry_run(self) -> None:
        layout = SyntheticCodexLayout(self.root, with_sessions=True)
        state_before = layout.state_file.read_bytes()
        backup_before = layout.state_backup_file.read_bytes()
        database_before = hashlib.sha256(layout.database.read_bytes()).hexdigest()

        result = self.run_cli(
            "plan",
            "--codex-home",
            str(layout.home),
            "--timezone",
            "UTC",
            "--as-of",
            "2026-08-12T12:00:00Z",
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        summary = manifest["summary"]
        self.assertEqual(summary["extension_chats_found"], 2)
        self.assertEqual(summary["active_extension_chats"], 1)
        self.assertEqual(summary["archived_extension_chats"], 1)
        self.assertEqual(summary["projects_to_create"], 1)
        self.assertEqual(summary["thread_assignments_to_write"], 1)
        self.assertEqual(summary["threads_missing_from_native_catalog"], 0)
        self.assertEqual(manifest["native_changes"]["catalog_missing_thread_ids"], [])
        self.assertNotEqual(
            manifest["state"]["before_sha256"],
            manifest["state"]["after_sha256"],
        )
        self.assertNotIn(PRIVATE_MARKER, result.stdout)
        self.assertEqual(layout.state_file.read_bytes(), state_before)
        self.assertEqual(layout.state_backup_file.read_bytes(), backup_before)
        self.assertEqual(hashlib.sha256(layout.database.read_bytes()).hexdigest(), database_before)
        self.assertFalse((layout.home / "backups" / "codex-linux-repo-import").exists())

    def test_apply_requires_yes_before_any_native_mutation(self) -> None:
        layout = SyntheticCodexLayout(self.root, with_sessions=True)
        state_before = layout.state_file.read_bytes()
        backup_before = layout.state_backup_file.read_bytes()

        result = self.run_cli("apply", "--codex-home", str(layout.home))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("apply requires --yes", result.stderr)
        self.assertEqual(layout.state_file.read_bytes(), state_before)
        self.assertEqual(layout.state_backup_file.read_bytes(), backup_before)
        self.assertFalse((layout.home / "backups" / "codex-linux-repo-import").exists())

    def test_plan_without_extension_chats_returns_a_clear_error(self) -> None:
        layout = SyntheticCodexLayout(self.root, with_sessions=False)

        result = self.run_cli(
            "plan",
            "--codex-home",
            str(layout.home),
            "--json",
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("no VS Code Codex extension chats were found", result.stderr)

    def test_backups_json_is_empty_for_a_fresh_layout(self) -> None:
        layout = SyntheticCodexLayout(self.root, with_sessions=False)

        result = self.run_cli(
            "backups",
            "--codex-home",
            str(layout.home),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"backups": []})
        self.assertEqual(result.stderr, "")

    def test_doctor_json_reports_healthy_synthetic_fixture(self) -> None:
        layout = SyntheticCodexLayout(self.root, with_sessions=True)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(cli, "is_desktop_running", return_value=False),
            mock.patch.object(cli, "detect_desktop_version", return_value=TESTED_VERSION),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            return_code = cli.main(
                ["doctor", "--codex-home", str(layout.home), "--json"]
            )

        self.assertEqual(return_code, 0, stderr.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["tool_version"], __version__)
        self.assertFalse(payload["desktop_running"])
        self.assertEqual(payload["desktop_version"], TESTED_VERSION)
        self.assertEqual(payload["native_state"]["compatibility"], "tested")
        self.assertTrue(payload["native_state"]["backup_matches"])
        self.assertEqual(payload["extension_sessions"], {"active": 1, "archived": 1, "diagnostics": 0})
        self.assertEqual(payload["native_thread_catalog"]["thread_count"], 2)
        self.assertTrue(payload["ready_for_offline_apply"])


if __name__ == "__main__":
    unittest.main()
