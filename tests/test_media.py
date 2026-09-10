"""Timing maths and subtitle structure.

The ASS field-count test is a regression guard: the Events `Format` line
originally omitted MarginV, which silently rendered a stray comma at the
start of every caption. It looked like a font problem, not a parser problem.
"""
import re
import tempfile
import unittest
from pathlib import Path

from core.captions import THEMES, Cue, build
from core.voice import Word, _chars_to_words, _weight, estimate_words


class TestWeight(unittest.TestCase):
    def test_longer_words_weigh_more(self):
        self.assertGreater(_weight("automation"), _weight("the"))

    def test_non_alphabetic_still_has_weight(self):
        self.assertGreater(_weight("---"), 0)

    def test_vowel_groups_count_once(self):
        # "queue" is one vowel run, so it should weigh like a one-syllable word.
        self.assertEqual(_weight("queue"), 1.0)


class TestEstimateWords(unittest.TestCase):
    def test_spans_exactly_the_duration(self):
        words = estimate_words("one two three four five", 5.0)
        self.assertAlmostEqual(words[0].start, 0.0)
        self.assertAlmostEqual(words[-1].end, 5.0, places=6)

    def test_is_contiguous_with_no_gaps(self):
        words = estimate_words("alpha beta gamma delta", 4.0)
        for prev, nxt in zip(words, words[1:]):
            self.assertAlmostEqual(prev.end, nxt.start, places=9)

    def test_word_count_is_preserved(self):
        text = "Anthropic shipped a memory tool for agents"
        self.assertEqual(len(estimate_words(text, 3.0)), len(text.split()))

    def test_empty_text_yields_nothing(self):
        self.assertEqual(estimate_words("   ", 3.0), [])


class TestCharAlignment(unittest.TestCase):
    def test_folds_characters_into_words(self):
        text = "hi there"
        chars = list(text)
        starts = [i * 0.1 for i in range(len(chars))]
        ends = [(i + 1) * 0.1 for i in range(len(chars))]
        words = _chars_to_words(text, chars, starts, ends)
        self.assertEqual([w.text for w in words], ["hi", "there"])
        self.assertAlmostEqual(words[0].start, 0.0)
        self.assertAlmostEqual(words[1].end, 0.8)

    def test_trailing_word_is_not_dropped(self):
        text = "a b"
        chars = list(text)
        words = _chars_to_words(text, chars, [0, 0.1, 0.2], [0.1, 0.2, 0.3])
        self.assertEqual([w.text for w in words], ["a", "b"])

    def test_falls_back_when_alignment_is_empty(self):
        words = _chars_to_words("one two", [], [], [])
        self.assertEqual(len(words), 2)


class TestAssStructure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cue = Cue("THE CATCH", estimate_words("memory tool ships today now", 4.0))
        self.path = build(
            [cue], Path(self.tmp.name) / "c.ass", 1080, 1920, theme=THEMES["ticker"]
        )
        self.text = self.path.read_text()

    def tearDown(self):
        self.tmp.cleanup()

    def _dialogue_lines(self):
        return [l for l in self.text.splitlines() if l.startswith("Dialogue:")]

    def test_events_format_declares_ten_fields(self):
        line = next(l for l in self.text.splitlines()
                    if l.startswith("Format:") and "Layer" in l)
        fields = [f.strip() for f in line.split(":", 1)[1].split(",")]
        self.assertEqual(len(fields), 10)
        self.assertIn("MarginV", fields)

    def test_every_dialogue_line_matches_the_declared_field_count(self):
        # THE regression test: 9 commas before Text, so text never inherits one.
        for line in self._dialogue_lines():
            payload = line.split(":", 1)[1]
            head = payload.split(",", 9)
            self.assertEqual(len(head), 10, f"wrong field count: {line}")

    def test_caption_text_does_not_start_with_a_comma(self):
        for line in self._dialogue_lines():
            text = line.split(":", 1)[1].split(",", 9)[9]
            self.assertFalse(text.startswith(","), f"stray comma in: {line}")

    def test_label_is_uppercased_and_present(self):
        self.assertIn("THE CATCH", self.text)

    def test_active_word_uses_the_theme_accent(self):
        self.assertIn(THEMES["ticker"]["accent"], self.text)

    def test_timestamps_are_well_formed(self):
        pattern = re.compile(r"^\d:\d{2}:\d{2}\.\d{2}$")
        for line in self._dialogue_lines():
            _, start, end = line.split(":", 1)[1].split(",")[:3]
            self.assertRegex(start, pattern)
            self.assertRegex(end, pattern)

    def test_one_word_event_per_word(self):
        word_events = [l for l in self._dialogue_lines() if ",Word," in l]
        self.assertEqual(len(word_events), 5)

    def test_braces_in_text_cannot_break_override_tags(self):
        cue = Cue("", [Word("{evil}", 0.0, 1.0)])
        out = build([cue], Path(self.tmp.name) / "e.ass", 1080, 1920)
        body = out.read_text()
        self.assertIn("(evil)", body)

    def test_empty_cue_is_skipped(self):
        out = build([Cue("NOTHING", [])], Path(self.tmp.name) / "z.ass", 1080, 1920)
        self.assertEqual(
            [l for l in out.read_text().splitlines() if l.startswith("Dialogue:")], []
        )


if __name__ == "__main__":
    unittest.main()
