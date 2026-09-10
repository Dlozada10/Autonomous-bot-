"""The YouTube Analytics call itself, against a fake service.

No credentials and no network. What is actually under test is the part that
would fail silently in production: how the request is shaped, how the response
is mapped onto columns, and what happens when YouTube returns something other
than the happy path.
"""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from core import metrics
from core.config import Config
from core.state import Store


class FakeReports:
    """Records every query and replays queued responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0) if self.responses else {"rows": []}
        return _FakeRequest(response)


class _FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeService:
    def __init__(self, responses):
        self._reports = FakeReports(responses)

    def reports(self):
        return self._reports


class AnalyticsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.cfg = Config()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def publish(self, slug, remote_id, fmt="teardown"):
        vid = self.store.add_video(slug=slug, topic_id=None, format_id=fmt,
                                   voice_id="v1", title=slug, script={},
                                   path=f"/tmp/{slug}.mp4")
        self.store.add_upload(vid, "youtube", "ok", remote_id)
        return vid

    def run_pull(self, responses, days=90):
        """Run a pull against the fake service, swallowing progress output."""
        service = FakeService(responses)
        with redirect_stdout(io.StringIO()):
            with patch.object(metrics, "_analytics", return_value=service):
                count = metrics.pull(self.cfg, self.store, days=days)
        return count, service._reports


class TestRequestShape(AnalyticsCase):
    def test_sends_the_expected_query_parameters(self):
        self.publish("a", "YT_A")
        self.publish("b", "YT_B")
        _, reports = self.run_pull([{"columnHeaders": [], "rows": []}])

        self.assertEqual(len(reports.calls), 1)
        call = reports.calls[0]
        self.assertEqual(call["ids"], "channel==MINE")
        self.assertEqual(call["dimensions"], "video")
        self.assertCountEqual(
            call["filters"].removeprefix("video==").split(","), ["YT_A", "YT_B"]
        )
        for metric in ("views", "averageViewPercentage", "subscribersGained"):
            self.assertIn(metric, call["metrics"].split(","))

    def test_date_window_matches_the_requested_days(self):
        import datetime as dt

        self.publish("a", "YT_A")
        _, reports = self.run_pull([{"rows": []}], days=30)
        call = reports.calls[0]
        start = dt.date.fromisoformat(call["startDate"])
        end = dt.date.fromisoformat(call["endDate"])
        self.assertEqual((end - start).days, 30)
        self.assertEqual(end, dt.date.today())

    def test_no_published_videos_makes_no_call(self):
        service = FakeService([])
        with redirect_stdout(io.StringIO()):
            with patch.object(metrics, "_analytics", return_value=service) as spy:
                count = metrics.pull(self.cfg, self.store)
        self.assertEqual(count, 0)
        spy.assert_not_called()


class TestColumnMapping(AnalyticsCase):
    """The load-bearing behaviour: values follow their column names."""

    def test_columns_are_mapped_by_name_not_position(self):
        vid = self.publish("a", "YT_A")
        # Deliberately NOT the order the request asked for.
        response = {
            "columnHeaders": [
                {"name": "video"},
                {"name": "subscribersGained"},
                {"name": "averageViewPercentage"},
                {"name": "views"},
                {"name": "likes"},
            ],
            "rows": [["YT_A", 7, 61.5, 1234, 42]],
        }
        count, _ = self.run_pull([response])
        self.assertEqual(count, 1)

        row = self.store.conn.execute(
            "SELECT * FROM metrics WHERE video_id = ?", (vid,)
        ).fetchone()
        # If this were positional, views would be 7 and retention 1234.
        self.assertEqual(row["views"], 1234)
        self.assertEqual(row["avg_view_pct"], 61.5)
        self.assertEqual(row["subs_gained"], 7)
        self.assertEqual(row["likes"], 42)

    def test_metric_absent_from_the_response_is_stored_as_null(self):
        vid = self.publish("a", "YT_A")
        response = {
            "columnHeaders": [{"name": "video"}, {"name": "views"}],
            "rows": [["YT_A", 99]],
        }
        self.run_pull([response])
        row = self.store.conn.execute(
            "SELECT * FROM metrics WHERE video_id = ?", (vid,)
        ).fetchone()
        self.assertEqual(row["views"], 99)
        self.assertIsNone(row["avg_view_pct"])
        self.assertIsNone(row["subs_gained"])

    def test_all_declared_metrics_round_trip(self):
        vid = self.publish("a", "YT_A")
        response = {
            "columnHeaders": [{"name": "video"}] + [{"name": m} for m in metrics.METRICS],
            "rows": [["YT_A", 100, 12.5, 21, 55.5, 9, 3, 2, 4]],
        }
        self.run_pull([response])
        row = self.store.conn.execute(
            "SELECT * FROM metrics WHERE video_id = ?", (vid,)
        ).fetchone()
        self.assertEqual(row["views"], 100)
        self.assertEqual(row["watch_minutes"], 12.5)
        self.assertEqual(row["avg_view_secs"], 21)
        self.assertEqual(row["avg_view_pct"], 55.5)
        self.assertEqual(row["likes"], 9)
        self.assertEqual(row["comments"], 3)
        self.assertEqual(row["shares"], 2)
        self.assertEqual(row["subs_gained"], 4)


class TestResponseEdgeCases(AnalyticsCase):
    def test_video_we_did_not_upload_is_ignored(self):
        self.publish("a", "YT_A")
        response = {
            "columnHeaders": [{"name": "video"}, {"name": "views"}],
            "rows": [["YT_A", 10], ["SOMEONE_ELSES_VIDEO", 999]],
        }
        count, _ = self.run_pull([response])
        self.assertEqual(count, 1)
        self.assertEqual(
            self.store.conn.execute("SELECT COUNT(*) c FROM metrics").fetchone()["c"], 1
        )

    def test_missing_rows_key_is_survivable(self):
        self.publish("a", "YT_A")
        count, _ = self.run_pull([{"columnHeaders": [{"name": "video"}]}])
        self.assertEqual(count, 0)

    def test_null_rows_is_survivable(self):
        self.publish("a", "YT_A")
        count, _ = self.run_pull([{"columnHeaders": [], "rows": None}])
        self.assertEqual(count, 0)

    def test_row_without_a_video_column_is_skipped(self):
        self.publish("a", "YT_A")
        count, _ = self.run_pull([
            {"columnHeaders": [{"name": "views"}], "rows": [[10]]}
        ])
        self.assertEqual(count, 0)


class TestChunkingAndFailure(AnalyticsCase):
    def test_splits_large_channels_across_calls(self):
        total = metrics.CHUNK * 2 + 5
        for i in range(total):
            self.publish(f"s{i}", f"YT_{i}")
        _, reports = self.run_pull([{"rows": []}] * 3)

        self.assertEqual(len(reports.calls), 3)
        sent = []
        for call in reports.calls:
            ids = call["filters"].removeprefix("video==").split(",")
            self.assertLessEqual(len(ids), metrics.CHUNK)
            sent.extend(ids)
        # Every published video is asked about exactly once.
        self.assertEqual(len(sent), total)
        self.assertEqual(len(set(sent)), total)

    def test_a_failing_chunk_does_not_abort_the_rest(self):
        for i in range(metrics.CHUNK + 2):
            self.publish(f"s{i}", f"YT_{i}")

        good = {
            "columnHeaders": [{"name": "video"}, {"name": "views"}],
            "rows": [["YT_0", 5]],
        }
        # First chunk explodes, second must still be attempted and recorded.
        count, reports = self.run_pull([RuntimeError("500 backend error"), good])
        self.assertEqual(len(reports.calls), 2)
        self.assertEqual(count, 1)

    def test_rerunning_the_same_day_corrects_rather_than_duplicates(self):
        vid = self.publish("a", "YT_A")
        header = [{"name": "video"}, {"name": "views"}]
        self.run_pull([{"columnHeaders": header, "rows": [["YT_A", 10]]}])
        self.run_pull([{"columnHeaders": header, "rows": [["YT_A", 250]]}])

        rows = self.store.conn.execute(
            "SELECT * FROM metrics WHERE video_id = ?", (vid,)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["views"], 250)


if __name__ == "__main__":
    unittest.main()
