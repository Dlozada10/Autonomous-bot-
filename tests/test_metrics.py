"""Metrics storage and aggregation.

All offline. The API call itself is a thin wrapper; the part that can be
silently wrong is which snapshot counts and how buckets are averaged.
"""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.config import Config
from core.metrics import report
from core.state import Store


class MetricsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.cfg = Config()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def publish(self, slug, fmt, voice, remote_id):
        vid = self.store.add_video(slug=slug, topic_id=None, format_id=fmt,
                                   voice_id=voice, title=f"title {slug}",
                                   script={}, path=f"/tmp/{slug}.mp4")
        self.store.add_upload(vid, "youtube", "ok", remote_id)
        return vid

    def snap(self, vid, day, views, retention, likes=0, subs=0):
        self.store.record_metrics(vid, "youtube", day, {
            "views": views, "averageViewPercentage": retention,
            "likes": likes, "subscribersGained": subs,
            "estimatedMinutesWatched": 1.0, "averageViewDuration": 20,
            "comments": 0, "shares": 0,
        })


class TestRemoteIds(MetricsCase):
    def test_maps_successful_uploads_only(self):
        vid = self.publish("a", "teardown", "v1", "YT_A")
        other = self.store.add_video(slug="b", topic_id=None, format_id="news",
                                     voice_id="v1", title="t", script={})
        self.store.add_upload(other, "youtube", "error", detail="boom")
        self.assertEqual(self.store.remote_ids("youtube"), {"YT_A": vid})

    def test_ignores_other_platforms(self):
        vid = self.publish("a", "teardown", "v1", "YT_A")
        self.store.add_upload(vid, "tiktok", "ok", "TT_A")
        self.assertEqual(list(self.store.remote_ids("youtube")), ["YT_A"])

    def test_skips_blank_remote_ids(self):
        vid = self.store.add_video(slug="a", topic_id=None, format_id="news",
                                   voice_id="v1", title="t", script={})
        self.store.add_upload(vid, "youtube", "ok", "")
        self.assertEqual(self.store.remote_ids("youtube"), {})


class TestSnapshots(MetricsCase):
    def test_same_day_overwrites_rather_than_duplicating(self):
        vid = self.publish("a", "teardown", "v1", "YT_A")
        self.snap(vid, "2026-09-01", 100, 40.0)
        self.snap(vid, "2026-09-01", 250, 45.0)
        rows = self.store.conn.execute("SELECT * FROM metrics").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["views"], 250)

    def test_different_days_accumulate_as_history(self):
        vid = self.publish("a", "teardown", "v1", "YT_A")
        self.snap(vid, "2026-09-01", 100, 40.0)
        self.snap(vid, "2026-09-05", 900, 42.0)
        self.assertEqual(
            self.store.conn.execute("SELECT COUNT(*) c FROM metrics").fetchone()["c"], 2
        )

    def test_aggregation_uses_only_the_latest_snapshot(self):
        vid = self.publish("a", "teardown", "v1", "YT_A")
        self.snap(vid, "2026-09-01", 100, 40.0)
        self.snap(vid, "2026-09-05", 900, 60.0)
        row = self.store.performance_by("format_id")[0]
        self.assertEqual(row["videos"], 1)
        self.assertEqual(row["avg_views"], 900)
        self.assertEqual(row["avg_retention"], 60.0)

    def test_missing_metric_is_stored_as_null(self):
        vid = self.publish("a", "teardown", "v1", "YT_A")
        # Shorts sometimes report no retention at all.
        self.store.record_metrics(vid, "youtube", "2026-09-01", {"views": 10})
        row = self.store.conn.execute("SELECT * FROM metrics").fetchone()
        self.assertEqual(row["views"], 10)
        self.assertIsNone(row["avg_view_pct"])


class TestAggregation(MetricsCase):
    def setUp(self):
        super().setUp()
        a = self.publish("a", "teardown", "v1", "A")
        b = self.publish("b", "teardown", "v2", "B")
        c = self.publish("c", "news", "v1", "C")
        self.snap(a, "2026-09-05", 1000, 30.0, likes=10, subs=1)
        self.snap(b, "2026-09-05", 2000, 50.0, likes=20, subs=3)
        self.snap(c, "2026-09-05", 500, 80.0, likes=5, subs=0)

    def test_groups_by_format_and_averages(self):
        rows = {r["bucket"]: r for r in self.store.performance_by("format_id")}
        self.assertEqual(rows["teardown"]["videos"], 2)
        self.assertEqual(rows["teardown"]["avg_views"], 1500.0)
        self.assertEqual(rows["teardown"]["avg_retention"], 40.0)
        self.assertEqual(rows["teardown"]["subs"], 4)

    def test_sorted_by_retention_descending(self):
        buckets = [r["bucket"] for r in self.store.performance_by("format_id")]
        self.assertEqual(buckets[0], "news")

    def test_groups_by_voice(self):
        rows = {r["bucket"]: r for r in self.store.performance_by("voice_id")}
        self.assertEqual(rows["v1"]["videos"], 2)
        self.assertEqual(rows["v2"]["videos"], 1)

    def test_min_videos_filters_thin_buckets(self):
        rows = self.store.performance_by("format_id", min_videos=2)
        self.assertEqual([r["bucket"] for r in rows], ["teardown"])

    def test_top_videos_ordered_by_views(self):
        self.assertEqual(self.store.top_videos("youtube", 2)[0]["views"], 2000)

    def test_coverage_counts_distinct_videos(self):
        self.assertEqual(self.store.metrics_coverage("youtube"), (3, 3))

    def test_rejects_non_groupable_column(self):
        with self.assertRaises(ValueError):
            self.store.performance_by("title; DROP TABLE videos")


class TestReportOutput(MetricsCase):
    def test_empty_report_tells_you_what_to_run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            report(self.cfg, self.store)
        self.assertIn("run `python run.py metrics`", buf.getvalue())

    def test_report_renders_buckets_and_labels(self):
        vid = self.publish("a", "teardown", "v1", "A")
        vid2 = self.publish("b", "teardown", "v1", "B")
        self.snap(vid, "2026-09-05", 1000, 30.0)
        self.snap(vid2, "2026-09-05", 1200, 35.0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            report(self.cfg, self.store, min_videos=2)
        out = buf.getvalue()
        self.assertIn("BY FORMAT", out)
        self.assertIn("Single-tool teardown", out)   # label, not raw id
        self.assertIn("TOP VIDEOS", out)

    def test_report_survives_null_retention(self):
        vid = self.publish("a", "teardown", "v1", "A")
        self.store.record_metrics(vid, "youtube", "2026-09-05", {"views": 5})
        buf = io.StringIO()
        with redirect_stdout(buf):
            report(self.cfg, self.store, min_videos=1)
        self.assertIn("BY FORMAT", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
