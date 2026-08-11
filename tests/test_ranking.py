from __future__ import annotations

from datetime import datetime, timezone
import unittest

from codex_repo_import.ranking import rank_projects, score_project

from tests.helpers import group, session


AS_OF = datetime(2026, 10, 30, tzinfo=timezone.utc)


class ScoreProjectTests(unittest.TestCase):
    def test_golden_score_combines_frequency_recency_activity_span_and_continuity(self) -> None:
        records = [
            session("a", "2026-08-01T00:00:00Z", "/repo"),
            session("b", "2026-08-31T00:00:00Z", "/repo"),
            session("c", "2026-09-30T00:00:00Z", "/repo"),
            session("d", "2026-10-30T00:00:00Z", "/repo"),
        ]

        score, metrics = score_project(records, as_of=AS_OF)

        self.assertAlmostEqual(score, 65.87759895286412, places=10)
        self.assertEqual(
            metrics,
            {
                "chat_count": 4,
                "archived_chat_count": 0,
                "active_days": 4,
                "span_days": 90.0,
                "age_days": 0.0,
                "active_weeks": 4,
                "frequency": 0.528634,
                "recency": 1.0,
                "activity": 0.468679,
                "span": 0.5,
                "continuity": 0.534522,
                "score": 65.877599,
            },
        )

    def test_archived_sessions_are_counted_but_do_not_affect_score(self) -> None:
        active = session("active", "2026-10-30T00:00:00Z", "/repo")
        archived = session(
            "archived",
            "2020-01-01T00:00:00Z",
            "/repo",
            archived=True,
        )
        score_with_archive, metrics = score_project([active, archived], as_of=AS_OF)
        active_score, _ = score_project([active], as_of=AS_OF)
        self.assertEqual(score_with_archive, active_score)
        self.assertEqual(metrics["chat_count"], 1)
        self.assertEqual(metrics["archived_chat_count"], 1)

    def test_archived_only_project_has_zero_score_and_safe_metrics(self) -> None:
        record = session("old", "2026-01-01T00:00:00Z", "/repo", archived=True)
        score, metrics = score_project([record], as_of=AS_OF)
        self.assertEqual(score, 0.0)
        self.assertEqual(metrics["chat_count"], 0)
        self.assertEqual(metrics["archived_chat_count"], 1)
        self.assertEqual(metrics["continuity"], 0.0)

    def test_future_session_does_not_create_negative_age_or_superunit_recency(self) -> None:
        future = session("future", "2026-11-01T00:00:00Z", "/repo")
        _, metrics = score_project([future], as_of=AS_OF)
        self.assertEqual(metrics["age_days"], 0.0)
        self.assertEqual(metrics["recency"], 1.0)

    def test_local_day_count_uses_requested_timezone(self) -> None:
        records = [
            session("a", "2026-08-01T21:30:00Z", "/repo"),
            session("b", "2026-08-02T20:30:00Z", "/repo"),
        ]
        _, istanbul = score_project(records, as_of=AS_OF, timezone_name="Europe/Istanbul")
        _, utc = score_project(records, as_of=AS_OF, timezone_name="UTC")
        self.assertEqual(istanbul["active_days"], 1)
        self.assertEqual(utc["active_days"], 2)


class RankProjectsTests(unittest.TestCase):
    def test_orders_by_score_then_latest_count_and_lexical_root(self) -> None:
        same_time = "2026-10-01T00:00:00Z"
        lexical_b = group("/workspace/b", [session("b", same_time, "/workspace/b")])
        lexical_a = group("/workspace/a", [session("a", same_time, "/workspace/a")])
        recent = group("/workspace/recent", [session("r", "2026-10-30T00:00:00Z", "/workspace/recent")])

        ranked = rank_projects([lexical_b, lexical_a, recent], as_of=AS_OF)

        self.assertEqual(
            [item.root.root for item in ranked],
            ["/workspace/recent", "/workspace/a", "/workspace/b"],
        )
        self.assertGreater(ranked[0].score, ranked[1].score)
        self.assertEqual(ranked[1].score, ranked[2].score)

    def test_ranking_populates_manifest_metrics_in_place(self) -> None:
        candidate = group("/workspace/a", [session("a", "2026-10-30T00:00:00Z", "/workspace/a")])
        returned = rank_projects([candidate], as_of=AS_OF)
        self.assertIs(returned[0], candidate)
        self.assertIn("score", candidate.metrics)
        self.assertAlmostEqual(candidate.metrics["score"], candidate.score, places=6)


if __name__ == "__main__":
    unittest.main()
