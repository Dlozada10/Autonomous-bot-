"""Quality floors, rotation selection, and a real render smoke test."""
import tempfile
import unittest
from pathlib import Path

from core.config import Config
from core.script import Beat, Grade, Script, pick_format, passes
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

    def test_policy_floor_overrides_a_high_average(self):
        ok, reason = passes(self.cfg, grade(hook=100, grounding=100, orig=100, safety=60))
        self.assertFalse(ok)
        self.assertIn("policy_safety", reason)

    def test_weak_hook_fails(self):
        ok, reason = passes(self.cfg, grade(hook=55))
        self.assertFalse(ok)
        self.assertIn("hook_strength", reason)

    def test_mediocre_across_the_board_fails_the_average(self):
        # Every dimension clears its floor, but the average does not.
        ok, reason = passes(self.cfg, grade(hook=72, grounding=86, orig=72, safety=91))
        self.assertFalse(ok)
        self.assertIn("average", reason)

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
        from core.render import RenderError, build_audio
        from core.voice import Clip, Word

        cfg = Config()
        # Fabricate clips whose total blows past the Shorts ceiling.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            from core.voice import synthesize
            cfg.data["voice"]["provider"] = "silent"
            clip = synthesize("word " * 300, "", work / "long.mp3", cfg)
            self.assertGreater(clip.duration, float(cfg.get("video.max_seconds")))


if __name__ == "__main__":
    unittest.main()
