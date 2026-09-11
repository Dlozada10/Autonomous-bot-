"""Quality floors, rotation selection, and a real render smoke test."""
import tempfile
import unittest
from pathlib import Path

from core.config import Config
from core.script import Beat, Grade, Script, eligible_formats, pick_format, passes
from core.state import Store
from core.voice import pick_voice


def grade(hook=90, grounding=90, orig=90, safety=95):
    return Grade(hook_strength=hook, factual_grounding=grounding,
                 originality=orig, policy_safety=safety,
                 verdict="v", fix="f")


class TestQualityFloors(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_strong_script_passes(self):
        ok, _ = passes(self.cfg, grade())
        self.assertTrue(ok)

    def test_grounding_floor_overrides_a_high_average(self):
        # Everything else perfect; fabricated facts must still fail.
        ok, reason = passes(self.cfg, grade(hook=100, grounding=40, orig=100, safety=100))
        self.assertFalse(ok)
        self.assertIn("factual_grounding", reason)

    def test_every_failing_dimension_is_reported(self):
        """Naming only the first failure made the message contradict the
        grader's own verdict about which dimension was actually weakest."""
        ok, reason = passes(self.cfg, grade(hook=10, grounding=10, orig=10, safety=10))
        self.assertFalse(ok)
        for dim in ("hook_strength", "factual_grounding", "originality", "policy_safety"):
            self.assertIn(dim, reason)

    def test_policy_floor_overrides_a_high_average(self):
        ok, reason = passes(self.cfg, grade(hook=100, grounding=100, orig=100, safety=60))
        self.assertFalse(ok)
        self.assertIn("policy_safety", reason)

    def test_weak_hook_fails(self):
        ok, reason = passes(self.cfg, grade(hook=55))
        self.assertFalse(ok)
        self.assertIn("hook_strength", reason)

    def test_scraping_every_floor_is_not_good_enough(self):
        """Exactly at every floor must fail on the average.

        This is the whole reason min_score exists alongside the floors; if it
        ever sits below their mean it becomes dead configuration.
        """
        floors = self.cfg.get("quality.floors")
        g = grade(hook=floors["hook_strength"], grounding=floors["factual_grounding"],
                  orig=floors["originality"], safety=floors["policy_safety"])
        ok, reason = passes(self.cfg, g)
        self.assertFalse(ok)
        self.assertIn("average", reason)

    def _at_average(self, target: float):
        """A grade clearing every floor whose mean is exactly `target`."""
        floors = self.cfg.get("quality.floors")
        dims = {k: int(v) for k, v in floors.items()}
        # Push the surplus onto whichever dimension has the most headroom.
        surplus = target * 4 - sum(dims.values())
        dims["policy_safety"] += surplus
        self.assertLessEqual(dims["policy_safety"], 100, "no headroom for this target")
        return grade(hook=dims["hook_strength"], grounding=dims["factual_grounding"],
                     orig=dims["originality"], safety=dims["policy_safety"])

    def test_exactly_at_the_average_minimum_passes(self):
        # Pins the comparison as strict (<): at the minimum, good enough.
        minimum = float(self.cfg.get("quality.min_score"))
        g = self._at_average(minimum)
        dims = [g.hook_strength, g.factual_grounding, g.originality, g.policy_safety]
        self.assertEqual(sum(dims) / 4, minimum)
        ok, _ = passes(self.cfg, g)
        self.assertTrue(ok)

    def test_a_hair_below_the_average_minimum_fails(self):
        minimum = float(self.cfg.get("quality.min_score"))
        g = self._at_average(minimum - 0.25)
        ok, reason = passes(self.cfg, g)
        self.assertFalse(ok)
        self.assertIn("average", reason)

    def test_unknown_floor_dimension_is_a_loud_config_error(self):
        self.cfg.data["quality"]["floors"]["hook_strenght"] = 70   # typo
        with self.assertRaises(ValueError) as caught:
            passes(self.cfg, grade())
        self.assertIn("hook_strenght", str(caught.exception))

    def test_exactly_at_the_floor_passes(self):
        floors = self.cfg.get("quality.floors")
        ok, _ = passes(self.cfg, grade(
            hook=floors["hook_strength"], grounding=100,
            orig=floors["originality"], safety=100,
        ))
        self.assertTrue(ok)


class TestRotation(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _record(self, fmt, voice="v1"):
        self._n = getattr(self, "_n", 0) + 1
        self.store.add_video(slug=f"s{self._n}", topic_id=None,
                             format_id=fmt, voice_id=voice, title="t", script={})

    def test_format_never_repeats_inside_the_cooldown(self):
        cooldown = int(self.cfg.get("formats.cooldown"))
        chosen = []
        for _ in range(12):
            fmt = pick_format(self.cfg, self.store)
            self.assertNotIn(fmt["id"], chosen[-cooldown:])
            chosen.append(fmt["id"])
            self._record(fmt["id"])
        self.assertGreater(len(set(chosen)), 1)

    def test_format_selection_survives_a_full_cooldown(self):
        # More cooldown slots than variants: must still return something.
        self.cfg.data["formats"]["cooldown"] = 99
        for variant in self.cfg.get("formats.variants"):
            self._record(variant["id"])
        self.assertIn(pick_format(self.cfg, self.store)["id"],
                      {v["id"] for v in self.cfg.get("formats.variants")})

    def test_voice_never_repeats_inside_the_cooldown(self):
        cooldown = int(self.cfg.get("voice.cooldown"))
        chosen = []
        for _ in range(10):
            voice = pick_voice(self.cfg, self.store)
            self.assertNotIn(voice["id"], chosen[-cooldown:])
            chosen.append(voice["id"])
            self.store.add_video(slug=f"v{len(chosen)}", topic_id=None,
                                 format_id="teardown", voice_id=voice["id"],
                                 title="t", script={})

    def test_eligible_formats_excludes_the_cooldown_window(self):
        cooldown = int(self.cfg.get("formats.cooldown"))
        used = []
        for _ in range(cooldown):
            fmt = eligible_formats(self.cfg, self.store)[0]
            self._record(fmt["id"])
            used.append(fmt["id"])
        offered = {f["id"] for f in eligible_formats(self.cfg, self.store)}
        self.assertFalse(offered & set(used), "a cooling format was still offered")

    def test_eligible_formats_never_returns_empty(self):
        # More cooldown slots than variants: the writer still needs a choice.
        self.cfg.data["formats"]["cooldown"] = 99
        for variant in self.cfg.get("formats.variants"):
            self._record(variant["id"])
        self.assertTrue(eligible_formats(self.cfg, self.store))

    def test_voice_selection_handles_an_empty_pool(self):
        self.cfg.data["voice"]["pool"] = []
        self.assertIn("id", pick_voice(self.cfg, self.store))


class TestRenderSmoke(unittest.TestCase):
    """Proves the whole video path still produces a playable file."""

    def test_renders_a_real_mp4(self):
        import subprocess

        from core.ffmpeg import FFMPEG
        from core.render import render
        from core.voice import synthesize

        cfg = Config()
        cfg.data["voice"]["provider"] = "silent"

        script = Script(
            format_id="teardown",
            title="t",
            hook=Beat(voiceover="Short hook line here.", caption="Hook", b_roll="x"),
            beats=[Beat(voiceover="One body beat only.", caption="Body", b_roll="y")],
            payoff=Beat(voiceover="And the payoff line.", caption="Payoff", b_roll="z"),
            description="d", hashtags=["ai"],
        )
        fmt = {"id": "teardown", "label": "L", "beats": 1, "visual": "gradient_drift"}

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "w"
            work.mkdir()
            clips = [synthesize(b.voiceover, "", work / f"b{i}.mp3", cfg)
                     for i, b in enumerate(script.lines())]
            out = Path(tmp) / "out.mp4"
            path, duration = render(script, clips, fmt, out, cfg, work)

            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 10_000)
            self.assertGreater(duration, 1.0)

            probe = subprocess.run([FFMPEG, "-i", str(path), "-hide_banner"],
                                   capture_output=True, text=True).stderr
            self.assertIn("1080x1920", probe)
            self.assertIn("Video: h264", probe)
            self.assertIn("Audio: aac", probe)

    def test_overlong_narration_is_refused(self):
        """The Shorts ceiling must stop the render, not truncate it."""
        from core.render import RenderError, render
        from core.voice import synthesize

        cfg = Config()
        cfg.data["voice"]["provider"] = "silent"
        ceiling = float(cfg.get("video.max_seconds"))

        script = Script(
            format_id="teardown",
            title="t",
            hook=Beat(voiceover="word " * 70, caption="A", b_roll="x"),
            beats=[Beat(voiceover="word " * 70, caption="B", b_roll="y")],
            payoff=Beat(voiceover="word " * 70, caption="C", b_roll="z"),
            description="d", hashtags=["ai"],
        )
        fmt = {"id": "teardown", "label": "L", "beats": 1, "visual": "gradient_drift"}

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "w"
            work.mkdir()
            clips = [synthesize(b.voiceover, "", work / f"b{i}.mp3", cfg)
                     for i, b in enumerate(script.lines())]
            self.assertGreater(sum(c.duration for c in clips), ceiling)

            with self.assertRaises(RenderError) as caught:
                render(script, clips, fmt, Path(tmp) / "out.mp4", cfg, work)
            self.assertIn("ceiling", str(caught.exception))
            self.assertFalse((Path(tmp) / "out.mp4").exists())

    def test_narration_exactly_at_the_ceiling_is_allowed(self):
        """Pins the ceiling as strict (>): at the limit, still publishable."""
        from core.render import build_audio, render
        from core.voice import synthesize

        cfg = Config()
        cfg.data["voice"]["provider"] = "silent"
        script = Script(
            format_id="teardown",
            title="t",
            hook=Beat(voiceover="Short hook line.", caption="A", b_roll="x"),
            beats=[Beat(voiceover="One body beat.", caption="B", b_roll="y")],
            payoff=Beat(voiceover="Closing payoff line.", caption="C", b_roll="z"),
            description="d", hashtags=["ai"],
        )
        fmt = {"id": "teardown", "label": "L", "beats": 1, "visual": "gradient_drift"}

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "w"
            work.mkdir()
            clips = [synthesize(b.voiceover, "", work / f"b{i}.mp3", cfg)
                     for i, b in enumerate(script.lines())]
            _, _, exact = build_audio(clips, work / "probe.wav")

            # Set the ceiling to precisely this narration's length.
            cfg.data["video"]["max_seconds"] = exact
            out = Path(tmp) / "out.mp4"
            path, duration = render(script, clips, fmt, out, cfg, work)
            self.assertTrue(path.exists())
            self.assertAlmostEqual(duration, exact, places=6)

    def test_captions_are_actually_burned_into_the_frame(self):
        """Probing the streams proves nothing about what is on screen.

        A dark generated background peaks around Y=31; white caption text
        peaks near 250. Sampling the caption band separates the two.
        """
        import subprocess

        from core.ffmpeg import FFMPEG
        from core.render import render
        from core.voice import synthesize

        cfg = Config()
        cfg.data["voice"]["provider"] = "silent"
        script = Script(
            format_id="teardown",
            title="t",
            hook=Beat(voiceover="Captions must appear on the screen here.",
                      caption="Hook", b_roll="x"),
            beats=[Beat(voiceover="This second beat also carries words.",
                        caption="Body", b_roll="y")],
            payoff=Beat(voiceover="And the payoff line closes it out.",
                        caption="Payoff", b_roll="z"),
            description="d", hashtags=["ai"],
        )
        fmt = {"id": "teardown", "label": "L", "beats": 1, "visual": "gradient_drift"}

        def peak_luma(video, at, crop):
            out = subprocess.run(
                [FFMPEG, "-ss", str(at), "-i", str(video), "-frames:v", "1",
                 "-vf", f"{crop},signalstats,"
                        "metadata=print:key=lavfi.signalstats.YMAX",
                 "-f", "null", "-"],
                capture_output=True, text=True,
            ).stderr
            values = [int(l.split("=")[1]) for l in out.splitlines() if "YMAX=" in l]
            return values[-1] if values else 0

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "w"
            work.mkdir()
            clips = [synthesize(b.voiceover, "", work / f"b{i}.mp3", cfg)
                     for i, b in enumerate(script.lines())]
            out = Path(tmp) / "out.mp4"
            render(script, clips, fmt, out, cfg, work)

            caption_band = peak_luma(out, 2.0, "crop=1080:400:0:1330")
            empty_band = peak_luma(out, 2.0, "crop=1080:400:0:700")

            self.assertGreater(caption_band, 150,
                               "no bright text found where captions belong")
            self.assertLess(empty_band, 120,
                            "unexpected bright content mid-frame")


class TestAudioTimeline(unittest.TestCase):
    """Word timings must be shifted onto the video timeline, gaps included."""

    def _clips(self, work, texts):
        from core.voice import synthesize
        cfg = Config()
        cfg.data["voice"]["provider"] = "silent"
        return cfg, [synthesize(t, "", work / f"b{i}.mp3", cfg)
                     for i, t in enumerate(texts)]

    def test_each_beat_is_offset_by_the_previous_beats_plus_the_gap(self):
        from core.render import BEAT_GAP, build_audio

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _, clips = self._clips(work, ["first beat here", "second beat here"])
            _, cues, total = build_audio(clips, work / "voice.wav")

            self.assertEqual(len(cues), 2)
            self.assertAlmostEqual(cues[0].words[0].start, 0.0, places=6)
            expected = clips[0].duration + BEAT_GAP
            self.assertAlmostEqual(cues[1].words[0].start, expected, places=6)

    def test_total_duration_includes_every_gap(self):
        from core.render import BEAT_GAP, build_audio

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _, clips = self._clips(work, ["one two", "three four", "five six"])
            _, _, total = build_audio(clips, work / "voice.wav")
            expected = sum(c.duration for c in clips) + BEAT_GAP * len(clips)
            self.assertAlmostEqual(total, expected, places=6)

    def test_beats_are_actually_separated(self):
        """The gap must be real, not merely consistent with itself.

        Asserting against BEAT_GAP alone still passes if the gap is zero,
        so assert separation independently of the constant.
        """
        from core.render import BEAT_GAP, build_audio

        self.assertGreater(BEAT_GAP, 0.05)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _, clips = self._clips(work, ["one two", "three four"])
            _, cues, total = build_audio(clips, work / "voice.wav")
            self.assertGreater(total, sum(c.duration for c in clips))
            silence = cues[1].words[0].start - cues[0].words[-1].end
            self.assertGreater(silence, 0.05)

    def test_beats_never_overlap(self):
        from core.render import build_audio

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _, clips = self._clips(work, ["alpha beta", "gamma delta", "epsilon zeta"])
            _, cues, _ = build_audio(clips, work / "voice.wav")
            for earlier, later in zip(cues, cues[1:]):
                self.assertLessEqual(earlier.words[-1].end, later.words[0].start)

    def test_produces_a_readable_audio_file(self):
        from core.render import build_audio
        from core.voice import probe_duration

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _, clips = self._clips(work, ["one two three", "four five six"])
            path, _, total = build_audio(clips, work / "voice.wav")
            self.assertTrue(path.exists())
            self.assertAlmostEqual(probe_duration(path), total, delta=0.15)


if __name__ == "__main__":
    unittest.main()
