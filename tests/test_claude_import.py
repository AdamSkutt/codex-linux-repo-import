from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock

from codex_repo_import import cli
from codex_repo_import.claude_import import (
    ClaudeSessionCandidate,
    CodexAppServerClient,
    completion_failures,
    create_claude_import_backup,
    migration_item_for,
    scan_claude_history,
)


PRIVATE_MARKER = "PRIVATE-CLAUDE-MESSAGE-MUST-NOT-LEAK"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
THREAD_ID = "22222222-2222-4222-8222-222222222222"


def _record(
    record_type: str,
    content: object,
    cwd: Path | None,
    *,
    timestamp: str,
    sidechain: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": record_type,
        "sessionId": SESSION_ID,
        "timestamp": timestamp,
        "isSidechain": sidechain,
        "message": {"role": record_type, "content": content},
    }
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return payload


class SyntheticClaudeLayout:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.projects = root / ".claude" / "projects"
        self.project_bucket = self.projects / "-workspace-alpha"
        self.workspace = root / "workspace-alpha"
        self.codex_home = root / ".codex"
        self.source = self.project_bucket / f"{SESSION_ID}.jsonl"
        self.project_bucket.mkdir(parents=True)
        self.workspace.mkdir()
        self.codex_home.mkdir()

    def write_session(
        self,
        *,
        cwd: Path | None = None,
        extra_messages: int = 0,
        date: str = "2026-08-01",
    ) -> None:
        actual_cwd = self.workspace if cwd is None else cwd
        records: list[dict[str, object]] = [
            {"type": "custom-title", "customTitle": PRIVATE_MARKER, "sessionId": SESSION_ID},
            _record(
                "user",
                PRIVATE_MARKER,
                actual_cwd,
                timestamp=f"{date}T10:00:00Z",
            ),
            _record(
                "assistant",
                [{"type": "text", "text": PRIVATE_MARKER}],
                actual_cwd,
                timestamp=f"{date}T10:01:00Z",
            ),
        ]
        for index in range(extra_messages):
            records.append(
                _record(
                    "assistant",
                    [{"type": "text", "text": f"extra-{index}"}],
                    actual_cwd,
                    timestamp=f"{date}T10:02:{index:02d}Z",
                )
            )
        self.source.write_text(
            "".join(json.dumps(item) + "\n" for item in records),
            encoding="utf-8",
        )
        self.source.chmod(0o600)

    def create_database(self, *, include_history: bool = False) -> Path:
        database = self.codex_home / "state_5.sqlite"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)"
            )
            rollout = self.codex_home / "sessions" / f"rollout-{THREAD_ID}.jsonl"
            rollout.parent.mkdir()
            rollout.write_text("synthetic rollout\n", encoding="utf-8")
            connection.execute(
                "INSERT INTO threads (id, rollout_path) VALUES (?, ?)",
                (THREAD_ID, str(rollout)),
            )
            if include_history:
                connection.execute(
                    "CREATE TABLE external_agent_config_imports "
                    "(completed_at_ms INTEGER, successes TEXT, provider_id TEXT)"
                )
            connection.commit()
        finally:
            connection.close()
        return database


class ClaudeDiscoveryTests(unittest.TestCase):
    def test_scan_finds_all_structural_metadata_without_exposing_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session()
            before = hashlib.sha256(layout.source.read_bytes()).hexdigest()

            plan = scan_claude_history(layout.projects, layout.codex_home)

            after = hashlib.sha256(layout.source.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            self.assertEqual(len(plan.pending), 1)
            candidate = plan.pending[0]
            self.assertEqual(candidate.message_records, 2)
            self.assertEqual(candidate.target_cwd, str(layout.workspace.resolve()))
            self.assertEqual(
                candidate.first_timestamp,
                datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
            )
            self.assertNotIn(PRIVATE_MARKER, json.dumps(plan.to_public_dict()))
            self.assertNotIn(PRIVATE_MARKER, repr(candidate))

    def test_moved_workspace_is_blocked_by_upstream_embedded_cwd_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            old = Path(temporary) / "old-workspace"
            layout.write_session(cwd=old)

            blocked = scan_claude_history(layout.projects, layout.codex_home)

            self.assertEqual(len(blocked.blocked), 1)
            self.assertIn("embedded workspace path", blocked.blocked[0].blocked_reason or "")

    def test_exact_hash_ledger_marks_a_session_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session()
            layout.create_database()
            digest = hashlib.sha256(layout.source.read_bytes()).hexdigest()
            ledger = {
                "records": [
                    {
                        "source_path": str(layout.source.resolve()),
                        "content_sha256": digest,
                        "imported_thread_id": THREAD_ID,
                        "imported_at": 1_786_000_000,
                    }
                ]
            }
            (layout.codex_home / "external_agent_session_imports.json").write_text(
                json.dumps(ledger), encoding="utf-8"
            )

            plan = scan_claude_history(layout.projects, layout.codex_home)

            self.assertEqual(len(plan.imported), 1)
            self.assertEqual(plan.imported[0].imported_thread_id, THREAD_ID)

    def test_changed_source_is_pending_and_keeps_existing_rollout_for_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session()
            layout.create_database()
            ledger = {
                "records": [
                    {
                        "source_path": str(layout.source.resolve()),
                        "content_sha256": "0" * 64,
                        "imported_thread_id": THREAD_ID,
                        "imported_at": 1_786_000_000,
                    }
                ]
            }
            (layout.codex_home / "external_agent_session_imports.json").write_text(
                json.dumps(ledger), encoding="utf-8"
            )

            plan = scan_claude_history(layout.projects, layout.codex_home)

            self.assertEqual(len(plan.pending), 1)
            self.assertEqual(plan.pending[0].imported_thread_id, THREAD_ID)
            self.assertIsNotNone(plan.pending[0].target_rollout_path)

    def test_legacy_sqlite_history_prevents_duplicate_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session()
            database = layout.create_database(include_history=True)
            successes = [
                {
                    "item_type": "SESSIONS",
                    "cwd": None,
                    "source": str(layout.source.resolve()),
                    "target": THREAD_ID,
                }
            ]
            imported_after_mtime = layout.source.stat().st_mtime_ns // 1_000_000 + 5_000
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO external_agent_config_imports "
                    "(completed_at_ms, successes, provider_id) VALUES (?, ?, ?)",
                    (imported_after_mtime, json.dumps(successes), "claude-code"),
                )
                connection.commit()
            finally:
                connection.close()

            plan = scan_claude_history(layout.projects, layout.codex_home)

            self.assertEqual(len(plan.imported), 1)

    def test_missing_import_target_blocks_instead_of_duplicating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session()
            database = layout.codex_home / "state_5.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)"
                )
                connection.commit()
            finally:
                connection.close()
            digest = hashlib.sha256(layout.source.read_bytes()).hexdigest()
            (layout.codex_home / "external_agent_session_imports.json").write_text(
                json.dumps(
                    {
                        "records": [
                            {
                                "source_path": str(layout.source.resolve()),
                                "content_sha256": digest,
                                "imported_thread_id": THREAD_ID,
                                "imported_at": 1_786_000_000,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            plan = scan_claude_history(layout.projects, layout.codex_home)

            self.assertEqual(len(plan.blocked), 1)
            self.assertEqual(plan.blocked[0].blocked_reason, "import registry points to a missing Codex thread")
            self.assertIn("imported_thread_missing", {item.code for item in plan.diagnostics})

    def test_large_session_is_blocked_unless_explicitly_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session(extra_messages=1)
            with mock.patch(
                "codex_repo_import.claude_import.SAFE_MESSAGE_RECORD_LIMIT",
                2,
            ):
                blocked = scan_claude_history(layout.projects, layout.codex_home)
                allowed = scan_claude_history(
                    layout.projects,
                    layout.codex_home,
                    allow_large_sessions=True,
                )

            self.assertEqual(len(blocked.blocked), 1)
            self.assertEqual(len(allowed.pending), 1)

    def test_old_sessions_are_not_hidden_by_an_age_or_count_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session(date="2020-01-02")
            old_time_ns = 1_578_000_000_000_000_000
            os.utime(layout.source, ns=(old_time_ns, old_time_ns))

            plan = scan_claude_history(layout.projects, layout.codex_home)

            self.assertEqual(len(plan.pending), 1)
            self.assertEqual(plan.pending[0].first_timestamp.year, 2020)

    def test_nested_subagent_transcripts_are_not_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session()
            nested = layout.project_bucket / SESSION_ID / "subagents"
            nested.mkdir(parents=True)
            nested_source = nested / "agent.jsonl"
            nested_source.write_bytes(layout.source.read_bytes())
            nested_source.chmod(0o600)

            plan = scan_claude_history(layout.projects, layout.codex_home)

            self.assertEqual([item.source_path for item in plan.pending], [layout.source.resolve()])

    def test_group_writable_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session()
            layout.source.chmod(0o620)

            plan = scan_claude_history(layout.projects, layout.codex_home)

            self.assertEqual(plan.candidates, ())
            self.assertIn(
                "source_insecure_permissions",
                {item.code for item in plan.diagnostics},
            )

    def test_symlinked_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            outside = Path(temporary) / "outside.jsonl"
            outside.write_text("{}\n", encoding="utf-8")
            layout.source.symlink_to(outside)

            plan = scan_claude_history(layout.projects, layout.codex_home)

            self.assertEqual(plan.candidates, ())
            self.assertIn("source_symlink_rejected", {item.code for item in plan.diagnostics})


class ClaudeProtocolAndBackupTests(unittest.TestCase):
    def _candidate(self, layout: SyntheticClaudeLayout) -> ClaudeSessionCandidate:
        layout.write_session()
        return scan_claude_history(layout.projects, layout.codex_home).pending[0]

    def test_migration_item_selects_only_sessions_and_does_not_copy_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            candidate = self._candidate(layout)

            item = migration_item_for(candidate)

            self.assertEqual(item["itemType"], "SESSIONS")
            self.assertEqual(item["details"]["plugins"], [])
            self.assertEqual(item["details"]["sessions"][0]["title"], None)
            self.assertNotIn(PRIVATE_MARKER, json.dumps(item))

    def test_completion_failures_reads_new_protocol_and_accepts_legacy_empty_payload(self) -> None:
        self.assertEqual(completion_failures({}), [])
        payload = {
            "itemTypeResults": [
                {
                    "itemType": "SESSIONS",
                    "successes": [],
                    "failures": [{"message": "synthetic failure"}],
                }
            ]
        }
        self.assertEqual(completion_failures(payload), ["synthetic failure"])

    def test_recovery_snapshot_uses_private_files_and_sqlite_online_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            candidate = self._candidate(layout)
            database = layout.create_database()
            (layout.codex_home / "session_index.jsonl").write_text("{}\n", encoding="utf-8")
            backup_root = layout.codex_home / "backups" / "codex-linux-repo-import"
            backup_root.mkdir(parents=True)

            backup = create_claude_import_backup(
                layout.codex_home,
                backup_root,
                [candidate],
            )

            manifest = json.loads((backup.path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["kind"], "claude-session-import")
            self.assertTrue((backup.path / database.name).is_file())
            self.assertEqual(stat.S_IMODE(backup.path.stat().st_mode), 0o700)
            for path in backup.path.rglob("*"):
                if path.is_file():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_app_server_client_speaks_initialize_and_import_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            candidate = self._candidate(layout)
            fake = Path(temporary) / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "for line in sys.stdin:\n"
                "    message = json.loads(line)\n"
                "    method = message.get('method')\n"
                "    if method == 'initialize':\n"
                "        print(json.dumps({'id': message['id'], 'result': {'codexHome': '/tmp'}}), flush=True)\n"
                "    elif method == 'externalAgentConfig/import':\n"
                "        if set(message.get('params', {})) != {'migrationItems'}:\n"
                "            print(json.dumps({'id': message['id'], 'error': {'message': 'unexpected import params'}}), flush=True)\n"
                "            continue\n"
                "        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)\n"
                "        print(json.dumps({'method': 'externalAgentConfig/import/completed', 'params': {}}), flush=True)\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)

            with CodexAppServerClient(
                codex_home=layout.codex_home,
                codex_bin=str(fake),
                timeout=5,
            ) as client:
                completion = client.import_session(candidate)

            self.assertEqual(completion, {})


class ClaudeCliTests(unittest.TestCase):
    def test_cli_plan_is_dry_run_and_import_requires_yes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session()
            source_before = layout.source.read_bytes()
            out = io.StringIO()
            err = io.StringIO()

            with redirect_stdout(out), redirect_stderr(err):
                plan_code = cli.main(
                    [
                        "claude",
                        "plan",
                        "--claude-projects",
                        str(layout.projects),
                        "--codex-home",
                        str(layout.codex_home),
                        "--json",
                    ]
                )
                import_code = cli.main(
                    [
                        "claude",
                        "import",
                        "--claude-projects",
                        str(layout.projects),
                        "--codex-home",
                        str(layout.codex_home),
                    ]
                )

            self.assertEqual(plan_code, 0)
            self.assertEqual(import_code, 2)
            self.assertEqual(layout.source.read_bytes(), source_before)
            self.assertNotIn(PRIVATE_MARKER, out.getvalue())
            self.assertIn("requires --yes", err.getvalue())

    def test_cli_import_creates_recovery_snapshot_and_reports_verified_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session()
            source_before = layout.source.read_bytes()
            fake_client = mock.MagicMock()
            fake_client.__enter__.return_value = fake_client
            fake_client.__exit__.return_value = None
            fake_client.import_session.return_value = {}
            out = io.StringIO()

            with (
                mock.patch.object(cli, "CodexAppServerClient", return_value=fake_client),
                mock.patch.object(cli, "imported_target_for", return_value=THREAD_ID),
                mock.patch.object(cli, "is_desktop_running", return_value=False),
                mock.patch.object(cli, "_validate_claude_import_root"),
                redirect_stdout(out),
            ):
                code = cli.main(
                    [
                        "claude",
                        "import",
                        "--claude-projects",
                        str(layout.projects),
                        "--codex-home",
                        str(layout.codex_home),
                        "--json",
                        "--yes",
                    ]
                )

            payload = json.loads(out.getvalue())
            backup = Path(payload["backup_path"])
            manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual(payload["imported"], 1)
            self.assertEqual(payload["results"][0]["imported_thread_id"], THREAD_ID)
            self.assertEqual(manifest["results"][0]["status"], "imported")
            self.assertEqual(layout.source.read_bytes(), source_before)
            self.assertNotIn(PRIVATE_MARKER, out.getvalue())

    def test_cli_rejects_relative_source_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session()
            err = io.StringIO()

            with redirect_stderr(err):
                code = cli.main(
                    [
                        "claude",
                        "plan",
                        "--claude-projects",
                        str(layout.projects),
                        "--codex-home",
                        str(layout.codex_home),
                        "--source",
                        "relative-session.jsonl",
                    ]
                )

            self.assertEqual(code, 2)
            self.assertIn("--source must be absolute", err.getvalue())

    def test_cli_refuses_mutation_while_codex_desktop_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session()
            source_before = layout.source.read_bytes()
            err = io.StringIO()

            with (
                mock.patch.object(cli, "is_desktop_running", return_value=True),
                redirect_stderr(err),
            ):
                code = cli.main(
                    [
                        "claude",
                        "import",
                        "--claude-projects",
                        str(layout.projects),
                        "--codex-home",
                        str(layout.codex_home),
                        "--yes",
                    ]
                )

            self.assertEqual(code, 2)
            self.assertIn("fully quit Codex Desktop", err.getvalue())
            self.assertEqual(layout.source.read_bytes(), source_before)
            self.assertFalse((layout.codex_home / "backups").exists())

    def test_cli_refuses_import_from_a_non_active_claude_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = SyntheticClaudeLayout(Path(temporary))
            layout.write_session()
            err = io.StringIO()

            with (
                mock.patch.object(cli, "is_desktop_running", return_value=False),
                redirect_stderr(err),
            ):
                code = cli.main(
                    [
                        "claude",
                        "import",
                        "--claude-projects",
                        str(layout.projects),
                        "--codex-home",
                        str(layout.codex_home),
                        "--yes",
                    ]
                )

            self.assertEqual(code, 2)
            self.assertIn("only from the active HOME source", err.getvalue())
            self.assertFalse((layout.codex_home / "backups").exists())


if __name__ == "__main__":
    unittest.main()
