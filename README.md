# Faceless channel pipeline

Autonomous short-form video: discovers a topic, writes a script, grades it,
narrates it, renders a 1080x1920 MP4, and publishes to YouTube Shorts, TikTok,
and Instagram Reels on a schedule.

Niche: AI tools & automation. Everything niche-specific lives in `config.yaml`.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in the keys
python run.py demo            # renders out/demo.mp4 with no API keys at all
python run.py doctor          # tells you exactly what is still missing
```

Once `doctor` is clean:

```bash
python run.py once            # one full cycle: make a video, publish it
python run.py daemon          # run forever on the configured schedule
```

## What each command does

| Command | Effect |
|---|---|
| `doctor` | Checks ffmpeg, keys, platform credentials, rotation config |
| `discover` | Refreshes the topic pool from RSS / Hacker News / GitHub |
| `make` | Produces one video, no upload |
| `publish` | Uploads anything rendered but not yet sent |
| `once` | `make` + `publish` |
| `daemon` | Sleeps until each schedule slot, then runs a cycle |
| `metrics` | Pulls YouTube performance data into the database |
| `report` | Ranks formats and voices by retention |
| `status` | Last 15 videos and today's per-platform counts |
| `demo` | Renders a sample with no credentials, to sanity-check the video path |
| `auth youtube` | One-time OAuth consent |

## Closing the loop

Rotation starts out random. Once there is data, it should not stay that way.

```bash
python run.py metrics    # pull performance into the db (also runs each cycle)
python run.py report     # rank formats and voices by retention
```

`metrics` stores a dated snapshot per video, so history accumulates rather than
being overwritten; aggregates always use the newest snapshot per video. After a
few weeks, `report` looks like this:

```
BY FORMAT
                                 n    views  retention   likes   subs
  The thing people get wrong     4     2228      65.5%    42.0     10
  Head-to-head                   4     1455      62.9%    28.0      6
  Single-tool teardown           4     1118      49.4%    18.0      8
  Do-this-in-N-steps             4      579      43.1%    14.0     16
  What shipped                   4      565      37.9%    12.0     15
```

Read the retention column, not views. Views follow distribution and luck;
retention tells you whether the format itself works. When a format sits at the
bottom across 10+ videos, delete it from `formats.variants` and let the
rotation concentrate on what earns attention.

**Analytics needs a scope the upload token does not have.** If you authorised
YouTube before this was added, `python run.py doctor` will say so — re-run
`python run.py auth youtube` to re-consent. Analytics also lags roughly 48
hours, so today's uploads legitimately show nothing.

## Tests

```bash
python -m unittest discover -s tests -t .
```

81 tests, ~10 seconds, no API keys or network required. Covers topic dedupe and
the similarity window, format/voice cooldown rotation, upload queueing and daily
caps, word-timing maths, ASS subtitle structure, the quality floors, metrics
snapshotting and aggregation, and a real end-to-end render probed with ffprobe.

The Analytics integration is covered against a fake service: request shape,
chunking across large channels, a failing chunk not aborting the rest, and
response columns being mapped by name rather than position. That last one is
mutation-checked — rewriting the mapping positionally makes the test fail with
`subscribersGained` landing in the views column, which is exactly the bug it
exists to catch.

### Mutation testing

```bash
python tests/mutate.py
```

Dedupe and cooldown fail silently when they break — the pipeline keeps
producing videos, they just start repeating. A green suite is not evidence the
tests would notice. This breaks that logic 14 ways, one line at a time, and
checks the suite turns red for each.

A `SURVIVED` line means the code was broken and nothing complained; treat it as
a missing test, not a curiosity. The first run found two real holes: an order
test whose two inputs happened to normalise to the same sequence anyway, and an
unpinned `>=` boundary on the similarity threshold. Both are covered now.

## How it works

```
topics.py    RSS + HN + GitHub  ->  ranked by freshness x source weight
state.py     sqlite: rejects anything too similar to the last 120 days
script.py    Claude writes a structured script for a rotating format
             Claude grades it independently; below the floors it is rewritten
voice.py     TTS with word-level timings (ElevenLabs) or estimated timings
captions.py  timings -> ASS with karaoke word highlighting
render.py    ffmpeg: animated gradient or b-roll, captions burned in, music bed
publish/     YouTube Data API, TikTok Content Posting API, Instagram Graph API
```

### The quality gate

This is the part that matters for running unattended. Every script is scored by
a second, independent Claude call on four dimensions, each with a hard floor in
`config.yaml`:

- `hook_strength` - would a scrolling viewer stop
- `factual_grounding` - is every claim traceable to the source material
- `originality` - does it say more than the headline
- `policy_safety` - free of advice-giving, bait, and unverifiable superlatives

Any dimension below its floor fails the script outright, regardless of the
average. The writer gets the grader's specific fix and retries up to
`quality.max_retries` times, then abandons the topic. **Publishing an unreviewed
video is a choice you are making in config; the floors are what make it survivable.**

### Anti-repetition

YouTube's inauthentic-content policy targets mass-produced, formulaic uploads.
Three mechanisms push against that:

- **Format rotation** - 5 structures, none repeating within `formats.cooldown` videos
- **Voice rotation** - `voice.cooldown` videos before a voice can recur
- **Topic dedupe** - token-normalised similarity against everything published in
  the last `topics.dedupe_days` days

Each format also carries its own colour theme, so consecutive videos do not
look alike either.

## Credentials

| Key | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Scripts and grading | Required |
| `ELEVENLABS_API_KEY` | Voice + caption timing | Strongly recommended - only provider here returning word-level timestamps |
| `OPENAI_API_KEY` | Fallback voice | Captions fall back to estimated timing |
| `PEXELS_API_KEY` | Stock b-roll | Optional; generated gradients are used otherwise |

Without any voice key the pipeline still renders, with silent audio. Useful for
testing the visual path; not something to publish.

### YouTube

1. Google Cloud Console -> enable **YouTube Data API v3**
2. Create an **OAuth client ID** of type *Desktop app*, download the JSON
3. Save it to `secrets/youtube_client_secret.json`
4. `python run.py auth youtube` (needs a browser once; copy the resulting
   `secrets/youtube_token.json` to a headless server afterwards)

Quota: an upload costs 1600 units against a 10,000/day default, so about six
uploads per day before you need a quota increase.

### TikTok

Requires an approved developer app with the `video.publish` scope. Until the app
passes TikTok's audit, posts arrive as private drafts in the creator inbox no
matter what the API call says. Disabled in `config.yaml` by default.

### Instagram

Requires a Business or Creator account linked to a Facebook app. Instagram does
not accept file uploads - it fetches the video from a URL, so `out/` must be
served over public HTTPS and `PUBLIC_MEDIA_BASE_URL` set to that origin.
Disabled by default.

## Fonts

Captions default to DejaVu Sans Bold, which is merely adequate. A heavier face
reads far better at this size - install Montserrat ExtraBold, Inter Black, or
Anton and set `video.font` in `config.yaml` to the family name.

## Running it for real

```bash
# systemd, cron, or just:
nohup python run.py daemon > channel.log 2>&1 &
```

The daemon catches exceptions per cycle rather than dying, so one bad night does
not take the channel offline. Failed uploads stay queued and retry on the next
cycle; the rendered file is never lost to a platform outage.

## Cost per video

Roughly, at defaults (Claude Opus 5 for both the write and the grade):

| Item | Cost |
|---|---|
| Script + grade (1 attempt) | ~$0.06 |
| ElevenLabs narration (~100 words) | ~$0.02 |
| Render | $0 (local ffmpeg) |
| **Total** | **~$0.08** |

At 3 videos/day that is about **$7/month**. Retries and rejected topics push it
somewhat higher. Switching `MODEL` in `core/script.py` to `claude-sonnet-5`
cuts the Claude portion by roughly 60% - worth measuring against your own
quality bar before doing it, since the grader is the safety mechanism.
