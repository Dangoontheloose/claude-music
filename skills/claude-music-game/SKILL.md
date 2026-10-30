---
name: claude-music-game
description: >
  Game music from a cue sheet: seamless loops cut to whole bars, calm and combat
  layers that share bar lines, short stings and oneshots, game loudness, WAV
  masters plus OGG copies for the engine, and a provenance record per cue.
when_to_use: >
  Use when the user asks for game music, a cue sheet, a loop, a combat layer,
  a sting, mission or menu music, or to render, pick or finish game cues.
allowed-tools:
  - Bash
  - Read
  - Write
---

# claude-music-game: game music from a cue sheet

Script: `skills/claude-music/scripts/game_cues.py` (stdlib Python, JSON on stdout, progress on stderr).
Needs ffmpeg and ffprobe on PATH, and uv plus ACE-Step from `config.json` for `render` and loop `finish`.

## The workflow

1. **Write a cue sheet** (JSON, below). One sheet per batch, for example one per mission.
2. **Render takes:** `python game_cues.py render SHEET [--cue ID] [--takes N]`. Takes land in
   `<raw>/<cue>/takes/<cue>_takeNN.wav`, 48 kHz stereo 24-bit.
3. **The user listens and picks.** `python game_cues.py listen SHEET` writes `<raw>/<sheet>-listen.html`, a local
   page with a player per take and the pick command next to it. Never pick for them: `python game_cues.py pick SHEET CUE TAKE`.
4. **Finish:** `python game_cues.py finish SHEET [--cue ID]`. For each picked cue:
   - `loop`: trim to whole bars, then hide the end-to-start seam (below);
   - `layer`: same length as its parent, then the same seam step;
   - `sting` and `oneshot`: drop leading silence, cut to length, short fade-in, fade-out;
   - one constant gain to the loudness target, capped by the true peak (never dynamic, so loops stay seamless);
   - writes `<raw>/<cue>/finished/<cue>.wav` (master), `<game>/<cue>.ogg` (Vorbis) and `<provenance>/<cue>.json`.
5. **Layers come after their parent is finished:** `render` covers the parent's finished loop
   (`finished/seamed.wav`) at a high cover strength (1.0 = closest to the source), so both share tempo, key, length and bar lines.
6. `python game_cues.py status SHEET` shows takes, picks and finished cues.

## Seams

- `repaint` (default): rotate the loop so the join sits mid-file, regenerate `seam_bars` bars either side
  of it with ACE-Step's repaint task, rotate back. The wrap is now musical, not a crossfade.
- `crossfade`: fade the audio that followed the loop end into the loop start (equal power,
  `crossfade_seconds`). Uses the extra `tail_bars` rendered after a loop; loops only, not layers.

`finish` reports `seam_metrics` per loop: `jump` (end-to-start sample step, full scale = 1; a click is
above about 0.05) and `rms_ratio` (last 50 ms over first 50 ms; far below 1 means it fades out before the
wrap). These catch obvious faults; the user's ear decides: each finished loop also gets
`<raw>/<cue>/finished/<cue>_loop3.wav`, the loop played three times in a row, to hear the seam.

## Cue sheet

```json
{
  "name": "act1-m02",
  "paths": {"raw": "E:/MusicRaw/act1-m02", "game": "../../../Assets/_Project/Audio/Music/Act1",
            "provenance": "../provenance"},
  "defaults": {
    "caption_prefix": "instrumental, acoustic folk, fiddle, harmonium, low strings",
    "quality": "high", "takes": 2, "beats_per_bar": 4, "seam": "repaint", "seam_bars": 1,
    "loudness": {"loop": -20, "layer": -20, "sting": -16, "oneshot": -16}, "true_peak": -1,
    "ogg_quality": 8, "cover_strength": 0.7
  },
  "cues": [
    {"id": "MUS_M02_Calm", "type": "loop", "caption": "uneasy village theme, sparse guitar",
     "bpm": 110, "key": "D minor", "duration": 120},
    {"id": "MUS_M02_Combat", "type": "layer", "parent": "MUS_M02_Calm",
     "caption": "urgent low string ostinato, snare, metal percussion"},
    {"id": "MUS_Shared_Win_Act1", "type": "sting", "caption": "short warm horn call", "duration": 5}
  ]
}
```

- Relative paths resolve against the sheet's folder.
- `caption_prefix` is joined to every cue's caption (the act's palette). The full caption must fit 512 chars.
- A cue overrides any default. `layer` inherits `bpm`, `key`, `beats_per_bar` and `duration` from its parent.
- `duration`: loops are rounded to whole bars (at least 2); stings and oneshots are cut to it.
  ACE-Step renders at least 10 s, so short stings are cut from a 10 s take.
- `seed`: -1 for random; the seeds used are stored per take.

## Rules

- Instrumental always (wordless voice through the caption is fine).
- Captions describe instruments and moods. Never name a game, film, composer or artist.
- Provenance files are the record for asset logs and AI-content disclosure: keep them with the game files.
