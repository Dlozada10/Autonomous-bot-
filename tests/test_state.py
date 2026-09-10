"""Dedupe and rotation. A silent bug here makes the channel repeat itself,
which is both an audience problem and a monetisation-review problem.
"""
import tempfile
import unittest
from pathlib import Path

from core.state import Store, normalise


class TestNormalise(unittest.TestCase):
    def test_strips_case_punctuation_and_order(self):
        a = normalise("OpenAI Ships a New Agents SDK!")
        b = normalise("openai ships the new agents sdk")
        self.assertEqual(a, b)

    def test_drops_stopwords(self):
        self.assertNotIn("the", normalise("the best of the tools").split())

    def test_empty_input_is_empty(self):
        self.assertEqual(normalise("!!! ??? "), "")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class TestDedupe(StoreCase):
    def test_exact_duplicate_is_rejected(self):
        first = self.store.add_topic("Anthropic ships memory tool", "u", "s", "")
        second = self.store.add_topic("Anthropic Ships Memory Tool!", "u2", "s", "")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_untitled_topic_is_rejected(self):
        self.assertIsNone(self.store.add_topic("!!!", "u", "s", ""))

    def test_similarity_only_counts_published_topics(self):
        self.store.add_topic("Anthropic ships a memory tool", "u", "s", "")
        # Banked but not yet used - must not block a similar topic.
        self.assertFalse(
            self.store.is_too_similar("Anthropic ships the memory tool", 120, 0.62)
        )

    def test_similar_published_topic_is_blocked(self):
        tid = self.store.add_topic("Anthropic ships a memory tool", "u", "s", "")
        self.store.mark_topic_used(tid)
        self.assertTrue(
            self.store.is_too_similar("Anthropic ships the memory tool", 120, 0.62)
        )

    def test_unrelated_topic_passes(self):
        tid = self.store.add_topic("Anthropic ships a memory tool", "u", "s", "")
        self.store.mark_topic_used(tid)
        self.assertFalse(
            self.store.is_too_similar("Notion adds database automations", 120, 0.62)
        )

    def test_dedupe_window_expires(self):
        tid = self.store.add_topic("Anthropic ships a memory tool", "u", "s", "")
        self.store.mark_topic_used(tid)
        # Published 200 days ago is outside a 120-day window.
        self.store.conn.execute(
            "UPDATE topics SET used_at = used_at - ? WHERE id = ?", (200 * 86400, tid)
        )
        self.store.conn.commit()
        self.assertFalse(
            self.store.is_too_similar("Anthropic ships the memory tool", 120, 0.62)
        )


class TestRotation(StoreCase):
    def _video(self, slug, fmt, voice):
        return self.store.add_video(
            slug=slug, topic_id=None, format_id=fmt, voice_id=voice,
            title="t", script={}, path=f"/tmp/{slug}.mp4",
        )

    def test_recent_values_is_newest_first(self):
        for i, fmt in enumerate(["teardown", "compare", "news"]):
            vid = self._video(f"v{i}", fmt, "voice")
            # Force distinct, increasing timestamps.
            self.store.conn.execute(
                "UPDATE videos SET created_at = ? WHERE id = ?", (1000 + i, vid)
            )
        self.store.conn.commit()
        self.assertEqual(
            self.store.recent_values("format_id", 3), ["news", "compare", "teardown"]
        )

    def test_recent_values_respects_limit(self):
        for i in range(6):
            self._video(f"v{i}", f"fmt{i}", "voice")
        self.assertEqual(len(self.store.recent_values("format_id", 4)), 4)

    def test_rejects_non_rotatable_column(self):
        # Guards against SQL injection through the column name.
        with self.assertRaises(ValueError):
            self.store.recent_values("title; DROP TABLE videos", 3)


class TestUploads(StoreCase):
    def setUp(self):
        super().setUp()
        self.vid = self.store.add_video(
            slug="v1", topic_id=None, format_id="teardown", voice_id="a",
            title="t", script={}, path="/tmp/v1.mp4",
        )

    def test_rendered_video_is_pending_everywhere(self):
        self.assertEqual(len(self.store.pending_uploads("youtube")), 1)
        self.assertEqual(len(self.store.pending_uploads("tiktok")), 1)

    def test_success_clears_pending_for_that_platform_only(self):
        self.store.add_upload(self.vid, "youtube", "ok", "abc")
        self.assertEqual(len(self.store.pending_uploads("youtube")), 0)
        self.assertEqual(len(self.store.pending_uploads("tiktok")), 1)

    def test_failed_upload_stays_pending_for_retry(self):
        self.store.add_upload(self.vid, "youtube", "error", detail="boom")
        self.assertEqual(len(self.store.pending_uploads("youtube")), 1)

    def test_daily_count_ignores_failures(self):
        self.store.add_upload(self.vid, "youtube", "ok", "a")
        self.store.add_upload(self.vid, "youtube", "error", detail="b")
        self.assertEqual(self.store.uploads_today("youtube"), 1)

    def test_daily_count_ignores_old_uploads(self):
        self.store.add_upload(self.vid, "youtube", "ok", "a")
        self.store.conn.execute(
            "UPDATE uploads SET created_at = created_at - ?", (2 * 86400,)
        )
        self.store.conn.commit()
        self.assertEqual(self.store.uploads_today("youtube"), 0)

    def test_unrendered_video_is_not_pending(self):
        self.store.add_video(
            slug="v2", topic_id=None, format_id="news", voice_id="a",
            title="t", script={}, path=None,
        )
        self.assertEqual(len(self.store.pending_uploads("youtube")), 1)


if __name__ == "__main__":
    unittest.main()
