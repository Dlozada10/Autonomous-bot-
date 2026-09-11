"""Article fetching.

Feeds routinely carry a headline and nothing else, which the writer
correctly refuses as too thin. These cover pulling the real body out of
the page, and doing it without a HTML parser dependency.
"""
import unittest
from unittest.mock import patch

from core import topics

ARTICLE = """<html><head><style>.x{color:red}</style>
<script>track();</script></head><body>
<nav><p>Home About Contact Subscribe to our newsletter today</p></nav>
<p>Anthropic released a memory tool that lets agents persist state to a file
store between runs, replacing the vector database most teams bolt on for this
single job and removing an entire service from the stack.</p>
<p>Too short to keep.</p>
<p>It ships inside the standard API surface, though developers still implement
the storage backend themselves, which is the tradeoff against a managed
offering that handles persistence for them.</p>
<footer><p>Copyright 2026 Example Media, all rights reserved worldwide</p></footer>
</body></html>"""


class FakeResponse:
    def __init__(self, text, content_type="text/html; charset=utf-8"):
        self.text = text
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        pass


class TestArticleText(unittest.TestCase):
    def test_extracts_body_paragraphs(self):
        with patch.object(topics.requests, "get", return_value=FakeResponse(ARTICLE)):
            text = topics.article_text("https://example.com/post")
        self.assertIn("memory tool", text)
        self.assertIn("storage backend", text)

    def test_excludes_navigation_and_footer_chrome(self):
        with patch.object(topics.requests, "get", return_value=FakeResponse(ARTICLE)):
            text = topics.article_text("https://example.com/post")
        self.assertNotIn("Subscribe to our newsletter", text)
        self.assertNotIn("Copyright", text)

    def test_drops_fragments_too_short_to_be_prose(self):
        with patch.object(topics.requests, "get", return_value=FakeResponse(ARTICLE)):
            text = topics.article_text("https://example.com/post")
        self.assertNotIn("Too short to keep", text)

    def test_scripts_and_styles_never_reach_the_output(self):
        with patch.object(topics.requests, "get", return_value=FakeResponse(ARTICLE)):
            text = topics.article_text("https://example.com/post")
        self.assertNotIn("track()", text)
        self.assertNotIn("color:red", text)

    def test_respects_the_length_limit(self):
        long_page = "<html><body>" + ("<p>" + "word " * 200 + "</p>") * 20 + "</body></html>"
        with patch.object(topics.requests, "get", return_value=FakeResponse(long_page)):
            text = topics.article_text("https://example.com/post", limit=500)
        self.assertLessEqual(len(text), 500)

    def test_non_html_response_yields_nothing(self):
        with patch.object(topics.requests, "get",
                          return_value=FakeResponse("%PDF-1.4", "application/pdf")):
            self.assertEqual(topics.article_text("https://example.com/a.pdf"), "")

    def test_empty_url_is_not_fetched(self):
        with patch.object(topics.requests, "get") as spy:
            self.assertEqual(topics.article_text(""), "")
        spy.assert_not_called()


class TestEnrich(unittest.TestCase):
    def test_a_substantial_summary_is_used_without_fetching(self):
        topic = {"summary": "x" * (topics.MIN_SOURCE_CHARS + 10), "url": "https://e.com"}
        with patch.object(topics.requests, "get") as spy:
            result = topics.enrich(topic)
        spy.assert_not_called()
        self.assertEqual(result, topic["summary"])

    def test_a_thin_summary_triggers_a_fetch(self):
        topic = {"summary": "Short headline blurb.", "url": "https://e.com/post"}
        with patch.object(topics.requests, "get", return_value=FakeResponse(ARTICLE)):
            result = topics.enrich(topic)
        self.assertIn("memory tool", result)
        self.assertGreater(len(result), topics.MIN_SOURCE_CHARS)

    def test_a_failed_fetch_falls_back_to_the_summary(self):
        topic = {"summary": "Short blurb.", "url": "https://e.com/post"}
        with patch.object(topics.requests, "get", side_effect=RuntimeError("timeout")):
            self.assertEqual(topics.enrich(topic), "Short blurb.")

    def test_the_longer_source_wins(self):
        # A page that yields almost nothing must not replace a decent summary.
        topic = {"summary": "A summary of moderate length that says something real.",
                 "url": "https://e.com/post"}
        with patch.object(topics.requests, "get",
                          return_value=FakeResponse("<html><body><p>hi</p></body></html>")):
            self.assertEqual(topics.enrich(topic), topic["summary"])


if __name__ == "__main__":
    unittest.main()
