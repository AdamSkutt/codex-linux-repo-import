from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_repo_import.sessions import (
    MAX_SESSION_META_BYTES,
    read_session_meta,
    scan_sessions,
)

from tests.helpers import FIXTURES


class _OneLineHandle(io.BytesIO):
    """A file spy that fails if the parser asks for anything after metadata."""

    def __init__(self, first_line: str | bytes) -> None:
        super().__init__(first_line.encode("utf-8") if isinstance(first_line, str) else first_line)
        self.readline_calls: list[int] = []

    def readline(self, size: int = -1) -> bytes:
        self.readline_calls.append(size)
        if len(self.readline_calls) > 1:
            raise AssertionError("session parser read beyond the first JSONL record")
        return super().readline(size)


class ReadSessionMetaTests(unittest.TestCase):
    def test_reads_exactly_one_line_and_only_exposes_allowlisted_metadata(self) -> None:
        envelope = {
            "type": "session_meta",
            "payload": {
                "id": "privacy-thread",
                "timestamp": "2026-08-01T10:00:00Z",
                "cwd": "/workspace/private",
                "originator": "codex_vscode",
                "source": "vscode",
                "cli_version": "0.147.0-alpha.6",
                "forked_from_id": "parent-thread",
                "private_extra": "MUST-NOT-LEAK",
            },
        }
        handle = _OneLineHandle(json.dumps(envelope) + "\n")
        with mock.patch.object(Path, "open", return_value=handle):
            record, diagnostic = read_session_meta(Path("ignored.jsonl"), archived=False)

        self.assertIsNone(diagnostic)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(handle.readline_calls, [MAX_SESSION_META_BYTES + 1])
        public = record.to_public_dict()
        self.assertEqual(
            set(public),
            {"id", "started_at", "cwd", "forked_from_id", "archived"},
        )
        self.assertNotIn("MUST-NOT-LEAK", repr(record))
        self.assertNotIn("MUST-NOT-LEAK", json.dumps(public))

    def test_exact_originator_and_source_filtering(self) -> None:
        base = {
            "type": "session_meta",
            "payload": {
                "id": "candidate",
                "timestamp": "2026-08-01T10:00:00Z",
                "cwd": "/workspace/project",
                "originator": "codex_vscode",
                "source": "vscode",
            },
        }
        variants = (
            ("Codex Desktop", "vscode"),
            ("codex_vscode", {"subagent": "critic"}),
            ("codex_vscode ", "vscode"),
            ("codex_vscode", "VSCode"),
        )
        for originator, source in variants:
            with self.subTest(originator=originator, source=source):
                envelope = json.loads(json.dumps(base))
                envelope["payload"]["originator"] = originator
                envelope["payload"]["source"] = source
                handle = _OneLineHandle(json.dumps(envelope) + "\n")
                with mock.patch.object(Path, "open", return_value=handle):
                    record, diagnostic = read_session_meta(Path("ignored.jsonl"), archived=False)
                self.assertIsNone(record)
                self.assertIsNone(diagnostic)

    def test_naive_timestamp_is_interpreted_as_utc(self) -> None:
        envelope = {
            "type": "session_meta",
            "payload": {
                "id": "naive",
                "timestamp": "2026-08-01T10:00:00",
                "cwd": "/workspace/project",
                "originator": "codex_vscode",
                "source": "vscode",
            },
        }
        handle = _OneLineHandle(json.dumps(envelope) + "\n")
        with mock.patch.object(Path, "open", return_value=handle):
            record, diagnostic = read_session_meta(Path("ignored.jsonl"), archived=True)
        self.assertIsNone(diagnostic)
        assert record is not None
        self.assertEqual(record.started_at.isoformat(), "2026-08-01T10:00:00+00:00")
        self.assertTrue(record.archived)

    def test_structural_and_field_errors_have_stable_diagnostic_codes(self) -> None:
        cases = (
            ("not-json\n", "invalid_session_json"),
            (json.dumps({"type": "event_msg"}) + "\n", "missing_session_meta"),
            (json.dumps({"type": "session_meta", "payload": []}) + "\n", "invalid_session_meta"),
            (
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "timestamp": "2026-08-01T10:00:00Z",
                            "cwd": "/workspace",
                            "originator": "codex_vscode",
                            "source": "vscode",
                        },
                    }
                )
                + "\n",
                "missing_thread_id",
            ),
            (
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "no-cwd",
                            "timestamp": "2026-08-01T10:00:00Z",
                            "originator": "codex_vscode",
                            "source": "vscode",
                        },
                    }
                )
                + "\n",
                "missing_cwd",
            ),
            (
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "bad-time",
                            "timestamp": "tomorrow-ish",
                            "cwd": "/workspace",
                            "originator": "codex_vscode",
                            "source": "vscode",
                        },
                    }
                )
                + "\n",
                "invalid_timestamp",
            ),
        )
        for line, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                handle = _OneLineHandle(line)
                with mock.patch.object(Path, "open", return_value=handle):
                    record, diagnostic = read_session_meta(Path("ignored.jsonl"), archived=False)
                self.assertIsNone(record)
                self.assertIsNotNone(diagnostic)
                assert diagnostic is not None
                self.assertEqual(diagnostic.code, expected_code)

    def test_rejects_an_oversized_first_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rollout-oversized.jsonl"
            path.write_text("x" * (MAX_SESSION_META_BYTES + 1), encoding="utf-8")
            record, diagnostic = read_session_meta(path, archived=False)
        self.assertIsNone(record)
        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual(diagnostic.code, "session_meta_too_large")

    def test_invalid_utf8_is_diagnostic_instead_of_aborting_the_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rollout-invalid-utf8.jsonl"
            path.write_bytes(b"\xff\xfe\xfa\n")
            record, diagnostic = read_session_meta(path, archived=False)
        self.assertIsNone(record)
        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertIn(diagnostic.code, {"session_read_error", "invalid_session_json"})


class ScanSessionsTests(unittest.TestCase):
    def test_fixture_scan_filters_non_extension_sessions_and_active_wins_dedupe(self) -> None:
        fixture_root = FIXTURES / "sessions"
        records, diagnostics = scan_sessions(
            fixture_root / "active",
            fixture_root / "archived",
        )

        self.assertEqual([item.thread_id for item in records], ["thread-archived", "thread-active"])
        active = next(item for item in records if item.thread_id == "thread-active")
        self.assertFalse(active.archived)
        self.assertEqual(active.cwd, "/workspace/alpha")
        archived = next(item for item in records if item.thread_id == "thread-archived")
        self.assertTrue(archived.archived)
        self.assertEqual(
            sorted(item.code for item in diagnostics),
            ["duplicate_thread_id", "missing_session_meta"],
        )

    def test_missing_directories_are_an_empty_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            records, diagnostics = scan_sessions(base / "missing", base / "also-missing")
        self.assertEqual(records, [])
        self.assertEqual(diagnostics, [])

    def test_duplicate_active_files_keep_lexically_first_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            active = base / "active"
            archived = base / "archived"
            active.mkdir()
            payload = {
                "type": "session_meta",
                "payload": {
                    "id": "duplicate",
                    "timestamp": "2026-08-01T10:00:00Z",
                    "cwd": "/workspace/first",
                    "originator": "codex_vscode",
                    "source": "vscode",
                },
            }
            (active / "rollout-a.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")
            payload["payload"]["cwd"] = "/workspace/second"
            (active / "rollout-b.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")
            records, diagnostics = scan_sessions(active, archived)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].cwd, "/workspace/first")
        self.assertEqual([item.code for item in diagnostics], ["duplicate_thread_id"])


if __name__ == "__main__":
    unittest.main()
