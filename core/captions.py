"""Build ASS subtitles with karaoke-style word highlighting.

The word-by-word pop is the visual signature of the format. Doing it in ASS
rather than with per-frame drawtext filters keeps the ffmpeg graph to a single
subtitles pass, which is roughly an order of magnitude faster.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .voice import Word

# ASS colours are &HBBGGRR - byte-reversed from hex RGB.
THEMES = {
    "gradient_drift": {"accent": "&H00D9FF&", "bg_a": "#1a1035", "bg_b": "#0d2b4e"},
    "split_slide":    {"accent": "&H6BFF6B&", "bg_a": "#07263a", "bg_b": "#0b3d2e"},
    "step_stack":     {"accent": "&H00A5FF&", "bg_a": "#2b1206", "bg_b": "#3d1a30"},
    "warn_pulse":     {"accent": "&H4747FF&", "bg_a": "#2a0a18", "bg_b": "#111133"},
    "ticker":         {"accent": "&HFFD966&", "bg_a": "#04203a", "bg_b": "#062a2a"},
}
DEFAULT_THEME = THEMES["gradient_drift"]

WORDS_PER_CHUNK = 3


@dataclass
class Cue:
    """One beat placed on the timeline."""
    label: str          # the beat's on-screen caption
    words: list[Word]   # absolute (video-relative) word timings


def _ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def _header(width: int, height: int, font: str, accent: str) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{font},92,&H00FFFFFF,&H000000FF,&H00101010,&HA0000000,-1,0,0,0,100,100,1,0,1,8,4,2,90,90,430,1
Style: Label,{font},52,{accent},&H000000FF,&H00101010,&HA0000000,-1,0,0,0,100,100,3,0,1,5,2,8,90,90,250,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _chunks(words: list[Word], size: int = WORDS_PER_CHUNK) -> list[list[Word]]:
    return [words[i:i + size] for i in range(0, len(words), size)]


def build(cues: list[Cue], out: Path, width: int, height: int,
          font: str = "DejaVu Sans", theme: dict | None = None) -> Path:
    """Write an .ass file covering every beat."""
    theme = theme or DEFAULT_THEME
    accent = theme["accent"]
    lines = [_header(width, height, font, accent)]

    for cue in cues:
        if not cue.words:
            continue
        start, end = cue.words[0].start, cue.words[-1].end

        # Beat label: sits at the top for the duration of the beat, sliding in.
        if cue.label:
            lines.append(
                f"Dialogue: 0,{_ts(start)},{_ts(end)},Label,,0,0,0,"
                f",{{\\fad(180,180)}}{_escape(cue.label.upper())}\n"
            )

        # Karaoke words: one event per word, showing its 3-word chunk.
        for chunk in _chunks(cue.words):
            for idx, word in enumerate(chunk):
                parts = []
                for j, w in enumerate(chunk):
                    text = _escape(w.text)
                    if j == idx:
                        # Active word: accent colour and a slight scale pop.
                        parts.append(
                            f"{{\\c{accent}\\fscx108\\fscy108}}{text}"
                            f"{{\\c&H00FFFFFF&\\fscx100\\fscy100}}"
                        )
                    else:
                        parts.append(text)
                body = " ".join(parts)
                # Hold the final word of a chunk until the chunk truly ends.
                stop = word.end if idx < len(chunk) - 1 else max(word.end, chunk[-1].end)
                lines.append(
                    f"Dialogue: 1,{_ts(word.start)},{_ts(stop)},Word,,0,0,0,,{body}\n"
                )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(lines), encoding="utf-8")
    return out
