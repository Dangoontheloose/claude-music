#!/usr/bin/env python3
"""game_cues.py: game music from a cue sheet (the Plowshare fork of claude-music).

A cue sheet (JSON) lists the cues a game needs: seamless loops, layers that
follow a loop (calm and combat versions of the same music), short stings and
non-looping oneshots. This script renders takes of each cue through
music_engine.py, lets you pick one, then finishes the pick: trims loops to whole
bars, hides the loop seam, sets game loudness, writes a 48 kHz 24-bit WAV master
and an OGG Vorbis copy for the game, and records how it was made.

    game_cues.py render  SHEET [--cue ID ...] [--takes N]
    game_cues.py pick    SHEET CUE TAKE
    game_cues.py finish  SHEET [--cue ID ...]
    game_cues.py status  SHEET

Like music_engine.py: JSON result on stdout, progress on stderr, stdlib only.
Needs ffmpeg and ffprobe on PATH, and uv plus ACE-Step (config.json) to render.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import wave
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENGINE = SCRIPT_DIR / "music_engine.py"
CONFIG_PATH = SCRIPT_DIR.parent / "config.json"
REPO_DIR = SCRIPT_DIR.parents[2]

CUE_TYPES = ("loop", "layer", "sting", "oneshot")
QUALITIES = ("draft", "standard", "high", "max")
SEAM_METHODS = ("repaint", "crossfade")

# Game loudness (integrated LUFS): music beds sit under the battle and the radio,
# stings and oneshots a little above. Overridable per sheet and per cue.
DEFAULT_LOUDNESS = {"loop": -20.0, "layer": -20.0, "sting": -16.0, "oneshot": -16.0}
DEFAULT_TRUE_PEAK = -1.0
DEFAULT_FADE = {"sting": 1.5, "oneshot": 2.0}
DEFAULT_COVER_STRENGTH = 0.7  # ACE-Step: 1.0 = closest to the source, 0.0 = pure noise

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 3  # 24-bit PCM masters
ENGINE_MIN_SECONDS = 10.0  # ACE-Step's duration range
ENGINE_MAX_SECONDS = 600.0
MIN_LOOP_BARS = 2
CAPTION_LIMIT = 512
METRIC_WINDOW_SECONDS = 0.05

SHEET_DEFAULTS = {
    "caption_prefix": "",
    "quality": "high",
    "takes": 2,
    "beats_per_bar": 4,
    "seam": "repaint",
    "seam_bars": 1,
    "tail_bars": 2,
    "crossfade_seconds": 0.5,
    "true_peak": DEFAULT_TRUE_PEAK,
    "ogg_quality": 8,
    "cover_strength": DEFAULT_COVER_STRENGTH,
}

ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class SheetError(ValueError):
    """The cue sheet or a command's arguments are wrong."""


class ToolError(RuntimeError):
    """ffmpeg, uv or the engine failed."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Cue sheet
# ---------------------------------------------------------------------------

def load_sheet(path) -> dict:
    """Read and validate a cue sheet. Returns {name, path, paths, cues: {id: cue}} with defaults merged in."""
    path = Path(path).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SheetError(f"cannot read cue sheet {path}: {e}") from e

    base = path.parent
    paths = raw.get("paths") or {}
    resolved_paths = {}
    for key in ("raw", "game", "provenance"):
        if not paths.get(key):
            raise SheetError(f"paths.{key} is missing")
        p = Path(os.path.expanduser(paths[key]))
        resolved_paths[key] = (p if p.is_absolute() else base / p).resolve()

    defaults = dict(SHEET_DEFAULTS)
    sheet_defaults = raw.get("defaults") or {}
    loudness = dict(DEFAULT_LOUDNESS)
    loudness.update(sheet_defaults.get("loudness") or {})
    defaults.update({k: v for k, v in sheet_defaults.items() if k != "loudness"})

    cues: dict[str, dict] = {}
    for entry in raw.get("cues") or []:
        cue = _resolve_cue(entry, defaults, loudness, cues)
        cues[cue["id"]] = cue
    if not cues:
        raise SheetError("the sheet has no cues")

    return {"name": raw.get("name") or path.stem, "path": path, "paths": resolved_paths, "cues": cues}


def _resolve_cue(entry: dict, defaults: dict, loudness: dict, earlier: dict) -> dict:
    cue_id = entry.get("id", "")
    if not isinstance(cue_id, str) or not ID_PATTERN.match(cue_id):
        raise SheetError(f"bad cue id {cue_id!r}: letters, digits and _ only, starting with a letter")
    if cue_id in earlier:
        raise SheetError(f"duplicate cue id {cue_id}")
    cue_type = entry.get("type")
    if cue_type not in CUE_TYPES:
        raise SheetError(f"{cue_id}: type must be one of {', '.join(CUE_TYPES)}")

    cue = dict(defaults)
    cue.update(entry)
    cue["loudness"] = entry.get("loudness", loudness[cue_type])
    cue.setdefault("key", None)
    cue.setdefault("seed", -1)
    cue.setdefault("fade", DEFAULT_FADE.get(cue_type))

    if cue_type == "layer":
        parent = earlier.get(entry.get("parent", ""))
        if parent is None or parent["type"] != "loop":
            raise SheetError(f"{cue_id}: parent must be a loop cue listed before it")
        for key in ("bpm", "key", "beats_per_bar", "duration"):
            if key not in entry:
                cue[key] = parent[key]

    if cue_type in ("loop", "layer") and not cue.get("bpm"):
        raise SheetError(f"{cue_id}: a {cue_type} needs a bpm")
    if cue.get("bpm") is not None and not 30 <= cue["bpm"] <= 300:
        raise SheetError(f"{cue_id}: bpm must be 30-300")
    duration = cue.get("duration")
    if duration is None or not 0 < float(duration) <= ENGINE_MAX_SECONDS:
        raise SheetError(f"{cue_id}: duration must be above 0 and at most {ENGINE_MAX_SECONDS:.0f} s")
    if cue["quality"] not in QUALITIES:
        raise SheetError(f"{cue_id}: quality must be one of {', '.join(QUALITIES)}")
    if cue["seam"] not in SEAM_METHODS:
        raise SheetError(f"{cue_id}: seam must be one of {', '.join(SEAM_METHODS)}")

    prefix = (cue.get("caption_prefix") or "").strip().rstrip(",")
    text = (entry.get("caption") or "").strip()
    cue["caption"] = f"{prefix}, {text}" if prefix and text else (prefix or text)
    if not cue["caption"]:
        raise SheetError(f"{cue_id}: caption is empty")
    if len(cue["caption"]) > CAPTION_LIMIT:
        raise SheetError(f"{cue_id}: caption is {len(cue['caption'])} chars with the prefix; the limit is 512")
    return cue


# ---------------------------------------------------------------------------
# Bars, loops and seams
# ---------------------------------------------------------------------------

def bar_seconds(bpm: float, beats_per_bar: int) -> float:
    return beats_per_bar * 60.0 / bpm


def loop_bars(duration: float, bpm: float, beats_per_bar: int) -> int:
    """Whole bars nearest the wanted length, at least MIN_LOOP_BARS."""
    return max(MIN_LOOP_BARS, round(duration / bar_seconds(bpm, beats_per_bar)))


def generation_seconds(length: float, tail: float) -> float:
    """What to ask the engine for: the length plus a tail to trim off, inside its range."""
    return min(ENGINE_MAX_SECONDS, max(ENGINE_MIN_SECONDS, length + tail))


def seam_plan(total_frames: int, bar_frames: float, seam_bars: int) -> dict:
    """Where to rotate a loop so its end-to-start join lands mid-file, and the window to repaint around it.

    Rotating by `rotate_at` puts the join at `join_at`; rotating the repainted file by `join_at` restores the
    original order, with the repainted join now split across the end and the start.
    """
    bars = max(1, round(total_frames / bar_frames))
    rotate_at = int(round(round(bars / 2) * bar_frames))
    join_at = total_frames - rotate_at
    half = seam_bars * bar_frames
    return {
        "rotate_at": rotate_at,
        "join_at": join_at,
        "repaint_start": max(0.0, (join_at - half) / SAMPLE_RATE),
        "repaint_end": min(total_frames, join_at + half) / SAMPLE_RATE,
    }


def rotate_frames(data: bytes, frames: int, frame_bytes: int) -> bytes:
    cut = frames * frame_bytes
    return data[cut:] + data[:cut]


def fit_frames(data: bytes, frames: int, frame_bytes: int) -> bytes:
    """Trim to `frames`, or pad the end with silence."""
    want = frames * frame_bytes
    return data[:want] if len(data) >= want else data + b"\x00" * (want - len(data))


def _samples(data: bytes, sample_width: int) -> list[int]:
    return [int.from_bytes(data[i:i + sample_width], "little", signed=True)
            for i in range(0, len(data) - sample_width + 1, sample_width)]


def _pack(samples: list[int], sample_width: int) -> bytes:
    top = (1 << (8 * sample_width - 1)) - 1
    return b"".join(max(-top - 1, min(top, s)).to_bytes(sample_width, "little", signed=True) for s in samples)


def seam_metrics(data: bytes, channels: int, sample_width: int, sample_rate: int) -> dict:
    """How the loop wraps: `jump` is the end-to-start sample step (0..2 of full scale, a click shows as a big
    value); `rms_ratio` is the loudness of the last window over the first (a fade-out shows as well below 1)."""
    frame = channels * sample_width
    full = float(1 << (8 * sample_width - 1))
    first = _samples(data[:frame], sample_width)
    last = _samples(data[-frame:], sample_width)
    jump = max(abs(a - b) for a, b in zip(first, last, strict=True)) / full

    window = min(len(data) // frame // 2, int(METRIC_WINDOW_SECONDS * sample_rate)) * frame

    def rms(chunk: bytes) -> float:
        values = _samples(chunk, sample_width)
        return math.sqrt(sum(v * v for v in values) / max(1, len(values)))

    head, tail = rms(data[:window]), rms(data[-window:])
    return {"jump": round(jump, 5), "rms_ratio": round(tail / head, 3) if head else None}


def crossfade_wrap(loop: bytes, tail: bytes, frames: int, channels: int, sample_width: int) -> bytes:
    """Classic loop crossfade: fade the audio that followed the loop's end into the loop's start, equal power."""
    frame = channels * sample_width
    frames = min(frames, len(tail) // frame, len(loop) // frame)
    head = _samples(loop[:frames * frame], sample_width)
    after = _samples(tail[:frames * frame], sample_width)
    mixed = []
    for i in range(frames * channels):
        t = (i // channels + 0.5) / frames
        mixed.append(round(head[i] * math.sin(t * math.pi / 2) + after[i] * math.cos(t * math.pi / 2)))
    return _pack(mixed, sample_width) + loop[frames * frame:]


# ---------------------------------------------------------------------------
# Loudness
# ---------------------------------------------------------------------------

def plan_gain(measured_i: float, measured_tp: float, target_i: float, max_tp: float) -> tuple[float, bool]:
    """One constant gain (seam-safe, unlike dynamic loudnorm). Capped so the true peak stays under max_tp."""
    gain = target_i - measured_i
    if measured_tp + gain > max_tp:
        return max_tp - measured_tp, True
    return gain, False


def parse_loudnorm(stderr: str) -> dict:
    """The JSON block ffmpeg's loudnorm prints, as floats."""
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr)
    if not match:
        raise ToolError("ffmpeg printed no loudnorm stats")
    stats = json.loads(match.group(0))
    out = {}
    for k, v in stats.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Tools: ffmpeg, the engine, git
# ---------------------------------------------------------------------------

def tool_path(name: str) -> str:
    """A command from PATH, else winget's link folder (shells started before a winget install miss it, and
    Git Bash can mangle a Windows path added to PATH), else the bare name."""
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found = shutil.which(name, path=str(Path(local) / "Microsoft" / "WinGet" / "Links"))
    return found or name


def _run(cmd: list[str], cwd=None, env=None) -> subprocess.CompletedProcess:
    cmd = [tool_path(cmd[0]), *cmd[1:]]
    try:
        result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise ToolError(f"{cmd[0]} not found on PATH") from e
    if result.returncode != 0:
        raise ToolError(f"{Path(cmd[0]).name} failed ({result.returncode}): {(result.stderr or result.stdout)[-800:]}")
    return result


def to_master_format(src: Path, dst: Path) -> None:
    """Any engine output to 48 kHz stereo 24-bit WAV."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
          "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s24le", str(dst)])


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (CHANNELS, SAMPLE_WIDTH, SAMPLE_RATE):
            raise ToolError(f"{path} is not 48 kHz stereo 24-bit")
        return w.readframes(w.getnframes())


def write_wav(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(data)


def measure_loudness(path: Path, target_i: float, max_tp: float) -> dict:
    result = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                   f"loudnorm=I={target_i}:TP={max_tp}:LRA=11:print_format=json", "-f", "null", "-"])
    return parse_loudnorm(result.stderr)


def engine_command(uv: str, engine: Path, ace_step_dir: Path, quality: str, output_dir: Path, seed: int,
                   batch: int, task: str, task_args: list[str]) -> list[str]:
    return [uv, "run", "python", str(engine),
            "--ace-step-dir", str(ace_step_dir), "--quality", quality, "--format", "wav", "--naming", "uuid",
            "--output-dir", str(output_dir), "--seed", str(seed), "--batch", str(batch),
            task, *task_args]


def ace_step_dir() -> Path:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ToolError(f"cannot read {CONFIG_PATH}: run the installer first ({e})") from e
    ace = cfg.get("ace_step_dir")
    if not ace or ace == "CHANGE_ME" or not Path(ace).is_dir():
        raise ToolError(f"ace_step_dir in {CONFIG_PATH} is not set to an ACE-Step folder")
    return Path(ace)


def run_engine(quality: str, output_dir: Path, seed: int, batch: int, task: str, task_args: list[str]) -> dict:
    """Run music_engine.py inside ACE-Step's environment; returns its JSON result."""
    ace = ace_step_dir()
    uv = shutil.which("uv")
    if not uv:
        raise ToolError("uv not found on PATH")
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = engine_command(uv, ENGINE, ace, quality, output_dir, seed, batch, task, task_args)
    env = dict(os.environ, TOKENIZERS_PARALLELISM="false", TORCHAUDIO_USE_BACKEND="ffmpeg", PYTHONIOENCODING="utf-8")
    log(f"[game_cues] engine {task}, {batch} take(s), quality {quality}...")
    proc = subprocess.run(cmd, cwd=ace, env=env, stdout=subprocess.PIPE, stderr=None, text=True,
                          encoding="utf-8", errors="replace")
    start = proc.stdout.find("{")
    try:
        result = json.loads(proc.stdout[start:]) if start >= 0 else {}
    except json.JSONDecodeError:
        result = {}
    if proc.returncode != 0 or not result.get("success"):
        raise ToolError(f"engine {task} failed: {result.get('error') or proc.stdout[-800:]}")
    return result


def git_commit(path: Path) -> str | None:
    try:
        return _run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"]).stdout.strip()
    except ToolError:
        return None


# ---------------------------------------------------------------------------
# State (takes and picks), provenance
# ---------------------------------------------------------------------------

def state_path(sheet: dict) -> Path:
    return sheet["paths"]["raw"] / "state.json"


def load_state(sheet: dict) -> dict:
    path = state_path(sheet)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(sheet: dict, state: dict) -> None:
    path = state_path(sheet)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def set_pick(state: dict, cue_id: str, take: int) -> None:
    entry = state.get(cue_id)
    if not entry or not entry.get("takes"):
        raise SheetError(f"{cue_id} has no takes yet: render it first")
    if take not in [t["take"] for t in entry["takes"]]:
        raise SheetError(f"{cue_id} has no take {take}")
    entry["pick"] = take


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def provenance(sheet: dict, cue: dict, take: dict, finished: dict, tool_versions: dict) -> dict:
    """What ASSET_LOG and the Steam AI disclosure need, per finished cue."""
    return {
        "cue": cue["id"],
        "type": cue["type"],
        "sheet": sheet["name"],
        "caption": cue["caption"],
        "bpm": cue.get("bpm"),
        "key": cue.get("key"),
        "beats_per_bar": cue.get("beats_per_bar"),
        "duration_asked": cue.get("duration"),
        "parent": cue.get("parent"),
        "cover_strength": cue.get("cover_strength") if cue["type"] == "layer" else None,
        "take": take["take"],
        "seed": take.get("seed"),
        "model": take.get("model"),
        "lm_model": take.get("lm_model"),
        "quality": take.get("quality"),
        "tool_versions": tool_versions,
        "generated_at": take.get("rendered_at"),
        "finished_at": now_iso(),
        "finished": finished,
        "ai_generated": True,
        "tool": "claude-music (Plowshare fork) with ACE-Step 1.5, run locally",
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _select(sheet: dict, ids: list[str] | None) -> list[dict]:
    if not ids:
        return list(sheet["cues"].values())
    missing = [i for i in ids if i not in sheet["cues"]]
    if missing:
        raise SheetError(f"not in the sheet: {', '.join(missing)}")
    return [sheet["cues"][i] for i in ids]


def _cue_dir(sheet: dict, cue_id: str) -> Path:
    return sheet["paths"]["raw"] / cue_id


def _loop_frames(cue: dict) -> tuple[int, float]:
    """(frames of the whole loop, frames per bar)."""
    bar = bar_seconds(cue["bpm"], cue["beats_per_bar"]) * SAMPLE_RATE
    bars = loop_bars(float(cue["duration"]), cue["bpm"], cue["beats_per_bar"])
    return int(round(bars * bar)), bar


def _gen_args(cue: dict, duration: float) -> list[str]:
    args = ["--caption", cue["caption"], "--instrumental", "--duration", f"{duration:.3f}"]
    if cue.get("bpm"):
        args += ["--bpm", str(int(cue["bpm"]))]
    if cue.get("key"):
        args += ["--key", cue["key"]]
    if cue.get("beats_per_bar"):
        args += ["--time-sig", str(cue["beats_per_bar"])]
    return args


def require_ffmpeg() -> None:
    """Fail before a long engine run rather than after it."""
    if not shutil.which(tool_path("ffmpeg")):
        raise ToolError("ffmpeg not found on PATH or in winget's link folder")


def cmd_render(sheet: dict, ids: list[str] | None, takes: int | None) -> dict:
    require_ffmpeg()
    state = load_state(sheet)
    rendered = []
    for cue in _select(sheet, ids):
        count = takes or cue["takes"]
        cue_dir = _cue_dir(sheet, cue["id"])
        if cue["type"] in ("loop", "layer"):
            frames, bar = _loop_frames(cue)
            length = frames / SAMPLE_RATE
        else:
            length = float(cue["duration"])

        if cue["type"] == "layer":
            parent_seamed = _cue_dir(sheet, cue["parent"]) / "finished" / "seamed.wav"
            if not parent_seamed.exists():
                raise SheetError(f"{cue['id']}: finish its parent {cue['parent']} first")
            task, task_args = "cover", ["--src-audio", str(parent_seamed),
                                        "--cover-strength", str(cue["cover_strength"])] + _gen_args(cue, length)
        else:
            tail = cue["tail_bars"] * bar / SAMPLE_RATE if cue["type"] == "loop" else 0.0
            task, task_args = "generate", _gen_args(cue, generation_seconds(length, tail))

        result = run_engine(cue["quality"], cue_dir / "engine", int(cue["seed"]), count, task, task_args)
        entry = state.setdefault(cue["id"], {"takes": [], "pick": None})
        for out in result.get("outputs", []):
            number = max([t["take"] for t in entry["takes"]], default=0) + 1
            take_path = cue_dir / "takes" / f"{cue['id']}_take{number:02d}.wav"
            to_master_format(Path(out["path"]), take_path)
            entry["takes"].append({
                "take": number, "path": str(take_path), "seed": out.get("seed"), "model": result.get("model"),
                "lm_model": result.get("lm_model"), "quality": cue["quality"], "task": task,
                "rendered_at": now_iso(),
            })
            rendered.append({"cue": cue["id"], "take": number, "path": str(take_path), "seed": out.get("seed")})
        save_state(sheet, state)
    return {"success": True, "command": "render", "rendered": rendered,
            "next": "listen to the takes, then: game_cues.py pick SHEET CUE TAKE"}


def cmd_pick(sheet: dict, cue_id: str, take: int) -> dict:
    if cue_id not in sheet["cues"]:
        raise SheetError(f"not in the sheet: {cue_id}")
    state = load_state(sheet)
    set_pick(state, cue_id, take)
    save_state(sheet, state)
    return {"success": True, "command": "pick", "cue": cue_id, "take": take}


def _shape_loop(sheet: dict, cue: dict, take_path: Path, work: Path) -> tuple[bytes, dict]:
    """Trim to whole bars and hide the seam. Returns (frames, info)."""
    frame = CHANNELS * SAMPLE_WIDTH
    data = read_wav(take_path)
    frames, bar = _loop_frames(cue)
    if cue["type"] == "layer":
        parent = read_wav(_cue_dir(sheet, cue["parent"]) / "finished" / "seamed.wav")
        frames = len(parent) // frame  # same length as its parent, so both share bar lines
    available = len(data) // frame
    if available < frames:
        bars = max(MIN_LOOP_BARS, int(available // bar))
        log(f"[game_cues] {cue['id']}: take is short, using {bars} bars")
        frames = int(round(bars * bar))
    loop = fit_frames(data, frames, frame)
    info = {"bars": round(frames / bar), "frames": frames, "seam": cue["seam"]}

    if cue["seam"] == "crossfade":
        tail = data[frames * frame:]
        if len(tail) < frame:
            raise SheetError(f"{cue['id']}: crossfade needs audio after the loop end; use seam repaint")
        xfade = int(cue["crossfade_seconds"] * SAMPLE_RATE)
        return crossfade_wrap(loop, tail, xfade, CHANNELS, SAMPLE_WIDTH), info

    plan = seam_plan(frames, bar, cue["seam_bars"])
    rotated_path = work / "rotated.wav"
    write_wav(rotated_path, rotate_frames(loop, plan["rotate_at"], frame))
    result = run_engine(cue["quality"], work / "engine", int(cue["seed"]), 1, "repaint", [
        "--src-audio", str(rotated_path), "--start", f"{plan['repaint_start']:.4f}",
        "--end", f"{plan['repaint_end']:.4f}", "--caption", cue["caption"], "--instrumental",
        "--bpm", str(int(cue["bpm"])), *(["--key", cue["key"]] if cue.get("key") else [])])
    outputs = result.get("outputs") or []
    if not outputs:
        raise ToolError(f"{cue['id']}: repaint returned no audio")
    repainted_path = work / "repainted.wav"
    to_master_format(Path(outputs[0]["path"]), repainted_path)
    repainted = read_wav(repainted_path)
    if abs(len(repainted) // frame - frames) > SAMPLE_RATE // 100:
        log(f"[game_cues] {cue['id']}: repaint changed the length by "
            f"{len(repainted) // frame - frames} frames; fitted back")
    seamed = rotate_frames(fit_frames(repainted, frames, frame), plan["join_at"], frame)
    info.update({"repaint_window": [round(plan["repaint_start"], 3), round(plan["repaint_end"], 3)],
                 "repaint_seed": outputs[0].get("seed")})
    return seamed, info


def _shape_oneshot(cue: dict, take_path: Path, out_path: Path) -> dict:
    """Stings and oneshots: drop leading silence, cut to length, fade out."""
    duration = float(cue["duration"])
    fade = min(float(cue["fade"] or 0), duration / 2)
    chain = ["silenceremove=start_periods=1:start_threshold=-50dB", f"atrim=0:{duration:.3f}",
             "afade=t=in:d=0.005"]
    if fade > 0:
        chain.append(f"afade=t=out:st={duration - fade:.3f}:d={fade:.3f}")
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(take_path), "-af", ",".join(chain),
          "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s24le", str(out_path)])
    return {"duration": duration, "fade": fade}


def cmd_finish(sheet: dict, ids: list[str] | None) -> dict:
    require_ffmpeg()
    state = load_state(sheet)
    versions = {"fork": git_commit(REPO_DIR), "ace_step": None}
    with contextlib.suppress(ToolError):
        versions["ace_step"] = git_commit(ace_step_dir())
    finished = []
    selected = _select(sheet, ids)
    for cue in selected:
        entry = state.get(cue["id"]) or {}
        pick = entry.get("pick")
        if pick is None:
            if ids:
                raise SheetError(f"{cue['id']}: no take picked yet")
            continue
        take = next(t for t in entry["takes"] if t["take"] == pick)
        work = _cue_dir(sheet, cue["id"]) / "finished"
        work.mkdir(parents=True, exist_ok=True)
        shaped = work / "shaped.wav"

        if cue["type"] in ("loop", "layer"):
            data, info = _shape_loop(sheet, cue, Path(take["path"]), work)
            write_wav(work / "seamed.wav", data)  # layers are covered from this
            write_wav(shaped, data)
        else:
            info = _shape_oneshot(cue, Path(take["path"]), shaped)

        stats = measure_loudness(shaped, cue["loudness"], cue["true_peak"])
        gain, limited = plan_gain(stats["input_i"], stats["input_tp"], cue["loudness"], cue["true_peak"])
        master = work / f"{cue['id']}.wav"
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(shaped), "-af", f"volume={gain:.3f}dB",
              "-c:a", "pcm_s24le", str(master)])
        game_file = sheet["paths"]["game"] / f"{cue['id']}.ogg"
        game_file.parent.mkdir(parents=True, exist_ok=True)
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(master), "-c:a", "libvorbis",
              "-q:a", str(cue["ogg_quality"]), str(game_file)])

        info.update({
            "sample_rate": SAMPLE_RATE,
            "loudness_target": cue["loudness"], "loudness_measured": stats["input_i"],
            "true_peak_measured": stats["input_tp"], "gain_db": round(gain, 2),
            "loudness_result": round(stats["input_i"] + gain, 2), "peak_limited": limited,
            "master": str(master), "game_file": str(game_file),
        })
        if cue["type"] in ("loop", "layer"):
            info["seam_metrics"] = seam_metrics(read_wav(master), CHANNELS, SAMPLE_WIDTH, SAMPLE_RATE)
            # The loop played three times in a row, to hear the seam
            preview = work / f"{cue['id']}_loop3.wav"
            _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-stream_loop", "2", "-i", str(master),
                  "-c", "copy", str(preview)])
            info["preview"] = str(preview)

        record = provenance(sheet, cue, take, info, versions)
        prov_path = sheet["paths"]["provenance"] / f"{cue['id']}.json"
        prov_path.parent.mkdir(parents=True, exist_ok=True)
        prov_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        entry["finished"] = {"at": record["finished_at"], "take": pick, "game_file": str(game_file)}
        save_state(sheet, state)
        finished.append({"cue": cue["id"], "take": pick, "game_file": str(game_file), "provenance": str(prov_path),
                         "loudness": info["loudness_result"], "seam_metrics": info.get("seam_metrics")})
    return {"success": True, "command": "finish", "finished": finished}


def cmd_status(sheet: dict) -> dict:
    state = load_state(sheet)
    cues = []
    for cue in sheet["cues"].values():
        entry = state.get(cue["id"]) or {}
        cues.append({"cue": cue["id"], "type": cue["type"], "takes": len(entry.get("takes", [])),
                     "pick": entry.get("pick"), "finished": bool(entry.get("finished"))})
    return {"success": True, "command": "status", "sheet": sheet["name"], "cues": cues}


def listen_html(sheet: dict, state: dict, page_dir: Path) -> str:
    """A local page with a player per take (and the seam preview of finished loops) and the pick command."""
    def src(path: str) -> str:
        return urllib.parse.quote(Path(os.path.relpath(path, page_dir)).as_posix())

    rows = []
    for cue in sheet["cues"].values():
        entry = state.get(cue["id"]) or {}
        timing = f"{cue['bpm']} BPM, " if cue.get("bpm") else ""
        rows.append(f"<h2>{html.escape(cue['id'])} <small>{cue['type']}, {timing}{html.escape(cue.get('key') or '')}"
                    f"</small></h2><p class=cap>{html.escape(cue['caption'])}</p>")
        takes = entry.get("takes") or []
        if not takes:
            rows.append("<p class=none>No takes yet.</p>")
        for take in takes:
            mark = " <b>picked</b>" if entry.get("pick") == take["take"] else ""
            command = f"pick {sheet['path'].name} {cue['id']} {take['take']}"
            rows.append(f"<div class=take><span>Take {take['take']}{mark}</span>"
                        f"<audio controls preload=none src=\"{src(take['path'])}\"></audio>"
                        f"<code>{html.escape(command)}</code></div>")
        preview = Path(take["path"]).parents[1] / "finished" / f"{cue['id']}_loop3.wav" if takes else None
        if preview and entry.get("finished") and preview.exists():
            rows.append(f"<div class=take><span>Finished, looped 3 times</span>"
                        f"<audio controls preload=none src=\"{src(str(preview))}\"></audio></div>")
    style = ("body{font:15px system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px}"
             "h2{margin:28px 0 4px}small{font-weight:normal;color:#666}.cap{color:#444;margin:0 0 8px}"
             ".take{display:flex;gap:12px;align-items:center;margin:6px 0;flex-wrap:wrap}.take span{width:150px}"
             "audio{width:360px;max-width:100%}code{font-size:12px;color:#555}.none{color:#999}")
    return (f"<!doctype html><meta charset=utf-8><title>{html.escape(sheet['name'])} takes</title>"
            f"<style>{style}</style><h1>{html.escape(sheet['name'])}: takes</h1>"
            f"<p>Listen, then run <code>game_cues.py</code> with the pick command next to the take you want.</p>"
            + "".join(rows))


def cmd_listen(sheet: dict) -> dict:
    page = sheet["paths"]["raw"] / f"{sheet['name']}-listen.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(listen_html(sheet, load_state(sheet), page.parent), encoding="utf-8")
    return {"success": True, "command": "listen", "page": str(page)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Game music from a cue sheet (claude-music, Plowshare fork)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("render", help="Render takes of cues")
    p.add_argument("sheet")
    p.add_argument("--cue", action="append", help="Only this cue (repeatable)")
    p.add_argument("--takes", type=int, default=None, help="Takes per cue (overrides the sheet)")
    p = sub.add_parser("pick", help="Pick the take to finish")
    p.add_argument("sheet")
    p.add_argument("cue")
    p.add_argument("take", type=int)
    p = sub.add_parser("finish", help="Loop, loudness, export and provenance for picked takes")
    p.add_argument("sheet")
    p.add_argument("--cue", action="append", help="Only this cue (repeatable)")
    p = sub.add_parser("status", help="Takes, picks and finished cues")
    p.add_argument("sheet")
    p = sub.add_parser("listen", help="Write a local page to listen to the takes and pick one")
    p.add_argument("sheet")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        sheet = load_sheet(args.sheet)
        if args.command == "render":
            result = cmd_render(sheet, args.cue, args.takes)
        elif args.command == "pick":
            result = cmd_pick(sheet, args.cue, args.take)
        elif args.command == "finish":
            result = cmd_finish(sheet, args.cue)
        elif args.command == "listen":
            result = cmd_listen(sheet)
        else:
            result = cmd_status(sheet)
    except (SheetError, ToolError) as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
