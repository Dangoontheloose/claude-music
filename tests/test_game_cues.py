"""Contract tests for game_cues.py (the game-music layer of the fork).

They cover the pure logic: cue sheet loading and validation, bar and loop math,
the seam rotation, the gain plan, loudnorm parsing, the engine command and the
provenance record. No GPU, no ACE-Step, no ffmpeg.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import game_cues as gc
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_sheet(tmp_path: Path, cues: list[dict], defaults: dict | None = None) -> Path:
    sheet = {
        "name": "act1-m02",
        "paths": {"raw": "raw", "game": "game", "provenance": "prov"},
        "defaults": defaults or {},
        "cues": cues,
    }
    path = tmp_path / "sheet.json"
    path.write_text(json.dumps(sheet), encoding="utf-8")
    return path


LOOP = {"id": "MUS_M02_Calm", "type": "loop", "caption": "uneasy village theme", "bpm": 110, "key": "D minor",
        "duration": 120}
LAYER = {"id": "MUS_M02_Combat", "type": "layer", "parent": "MUS_M02_Calm", "caption": "urgent low strings, snare"}
STING = {"id": "MUS_Shared_Win_Act1", "type": "sting", "caption": "short horn call", "duration": 5}


def pcm24(samples: list[int], channels: int = 1) -> bytes:
    """Little-endian signed 24-bit frames (interleaved if channels > 1)."""
    out = bytearray()
    for s in samples:
        for _ in range(channels):
            out += (s & 0xFFFFFF).to_bytes(3, "little")
    return bytes(out)


# ---------------------------------------------------------------------------
# Cue sheet
# ---------------------------------------------------------------------------

def test_load_sheet_merges_defaults_and_prefix(tmp_path):
    path = write_sheet(tmp_path, [LOOP, LAYER, STING],
                       defaults={"caption_prefix": "acoustic folk, fiddle, harmonium", "takes": 3})
    sheet = gc.load_sheet(path)
    calm = sheet["cues"]["MUS_M02_Calm"]
    assert calm["caption"] == "acoustic folk, fiddle, harmonium, uneasy village theme"
    assert calm["takes"] == 3
    assert calm["beats_per_bar"] == 4
    assert sheet["cues"]["MUS_Shared_Win_Act1"]["loudness"] == gc.DEFAULT_LOUDNESS["sting"]
    assert sheet["paths"]["raw"] == (tmp_path / "raw").resolve()


def test_load_sheet_layer_inherits_parent_timing(tmp_path):
    sheet = gc.load_sheet(write_sheet(tmp_path, [LOOP, LAYER]))
    combat = sheet["cues"]["MUS_M02_Combat"]
    assert combat["bpm"] == 110
    assert combat["key"] == "D minor"
    assert combat["duration"] == 120
    assert combat["cover_strength"] == gc.DEFAULT_COVER_STRENGTH


def test_cue_overrides_default(tmp_path):
    cue = dict(LOOP, takes=1, quality="max")
    sheet = gc.load_sheet(write_sheet(tmp_path, [cue], defaults={"takes": 4, "quality": "draft"}))
    assert sheet["cues"]["MUS_M02_Calm"]["takes"] == 1
    assert sheet["cues"]["MUS_M02_Calm"]["quality"] == "max"


@pytest.mark.parametrize("cues, message", [
    ([LOOP, LOOP], "duplicate"),
    ([dict(LOOP, type="song")], "type"),
    ([{k: v for k, v in LOOP.items() if k != "bpm"}], "bpm"),
    ([LAYER], "parent"),
    ([STING, dict(LAYER, parent="MUS_Shared_Win_Act1")], "parent"),
    ([dict(LOOP, id="bad id!")], "id"),
    ([dict(LOOP, caption="x" * 600)], "512"),
    ([dict(STING, duration=0)], "duration"),
])
def test_load_sheet_rejects_bad_cues(tmp_path, cues, message):
    with pytest.raises(gc.SheetError, match=message):
        gc.load_sheet(write_sheet(tmp_path, cues))


# ---------------------------------------------------------------------------
# Bars and loops
# ---------------------------------------------------------------------------

def test_bar_seconds():
    assert gc.bar_seconds(120, 4) == pytest.approx(2.0)
    assert gc.bar_seconds(90, 3) == pytest.approx(2.0)


def test_loop_bars_rounds_and_has_a_floor():
    assert gc.loop_bars(120, 110, 4) == 55  # 120 s / 2.18 s = 55.0
    assert gc.loop_bars(3, 120, 4) == gc.MIN_LOOP_BARS


def test_generation_seconds_adds_tail_and_respects_engine_limits():
    assert gc.generation_seconds(100.0, tail=4.0) == pytest.approx(104.0)
    assert gc.generation_seconds(3.0, tail=0.0) == gc.ENGINE_MIN_SECONDS
    assert gc.generation_seconds(700.0, tail=4.0) == gc.ENGINE_MAX_SECONDS


def test_seam_plan_rotates_at_a_bar_near_the_middle():
    sr = 48000
    bar = 96000  # 120 BPM, 4/4
    plan = gc.seam_plan(total_frames=8 * bar, bar_frames=bar, seam_bars=1)
    assert plan["rotate_at"] == 4 * bar
    assert plan["join_at"] == 4 * bar
    assert plan["repaint_start"] == pytest.approx(3 * bar / sr)
    assert plan["repaint_end"] == pytest.approx(5 * bar / sr)


def test_seam_plan_odd_bar_count():
    bar = 1000
    plan = gc.seam_plan(total_frames=7 * bar, bar_frames=bar, seam_bars=1)
    assert plan["rotate_at"] in (3 * bar, 4 * bar)
    assert plan["join_at"] == 7 * bar - plan["rotate_at"]


def test_rotate_then_unrotate_is_identity():
    frame = 6  # stereo, 24-bit
    data = bytes(range(256)) * 6  # 1536 bytes = 256 frames
    rotated = gc.rotate_frames(data, 100, frame)
    assert rotated != data
    assert rotated[:frame] == data[100 * frame:101 * frame]
    restored = gc.rotate_frames(rotated, 256 - 100, frame)
    assert restored == data


def test_fit_frames_pads_with_silence_and_trims():
    frame = 3
    data = b"\x01\x02\x03" * 10
    assert len(gc.fit_frames(data, 12, frame)) == 36
    assert gc.fit_frames(data, 12, frame).endswith(b"\x00" * 6)
    assert gc.fit_frames(data, 4, frame) == b"\x01\x02\x03" * 4


def test_seam_metrics_tell_a_smooth_join_from_a_click():
    sr = 48000
    n = sr  # one second of a 100 Hz sine: 100 whole cycles, so it loops cleanly
    smooth = [round(4_000_000 * math.sin(2 * math.pi * 100 * i / sr)) for i in range(n)]
    clicky = smooth[: n // 2 + 37]  # cut mid-cycle
    good = gc.seam_metrics(pcm24(smooth), channels=1, sample_width=3, sample_rate=sr)
    bad = gc.seam_metrics(pcm24(clicky), channels=1, sample_width=3, sample_rate=sr)
    assert good["jump"] < bad["jump"]
    assert good["jump"] < 0.01
    assert bad["jump"] > 0.05
    assert 0.9 < good["rms_ratio"] < 1.1


# ---------------------------------------------------------------------------
# Loudness
# ---------------------------------------------------------------------------

def test_plan_gain_reaches_target_when_peaks_allow():
    gain, limited = gc.plan_gain(measured_i=-14.0, measured_tp=-3.0, target_i=-20.0, max_tp=-1.0)
    assert gain == pytest.approx(-6.0)
    assert limited is False


def test_plan_gain_is_capped_by_true_peak():
    gain, limited = gc.plan_gain(measured_i=-30.0, measured_tp=-4.0, target_i=-20.0, max_tp=-1.0)
    assert gain == pytest.approx(3.0)
    assert limited is True


def test_parse_loudnorm_reads_the_json_block_from_ffmpeg_noise():
    stderr = """
    [Parsed_loudnorm_0 @ 000001] some line
    {
        "input_i" : "-17.42",
        "input_tp" : "-2.10",
        "input_lra" : "6.30",
        "input_thresh" : "-27.60"
    }
    size=N/A time=00:00:10.00
    """
    stats = gc.parse_loudnorm(stderr)
    assert stats["input_i"] == pytest.approx(-17.42)
    assert stats["input_tp"] == pytest.approx(-2.10)


def test_parse_loudnorm_fails_loudly_without_json():
    with pytest.raises(gc.ToolError):
        gc.parse_loudnorm("no stats here")


# ---------------------------------------------------------------------------
# Engine command, state, provenance
# ---------------------------------------------------------------------------

def test_engine_command_uses_wav_uuid_names_and_the_ace_step_dir(tmp_path):
    cmd = gc.engine_command(
        uv="uv", engine=Path("/x/music_engine.py"), ace_step_dir=Path("/ace"), quality="high",
        output_dir=tmp_path, seed=7, batch=2, task="generate",
        task_args=["--caption", "c", "--instrumental"])
    assert cmd[:4] == ["uv", "run", "python", str(Path("/x/music_engine.py"))]
    joined = " ".join(cmd)
    assert "--format wav" in joined
    assert "--naming uuid" in joined
    assert "--seed 7" in joined
    assert "--batch 2" in joined
    assert cmd.index("generate") < cmd.index("--caption")


def test_pick_requires_an_existing_take():
    state = {"MUS_M02_Calm": {"takes": [{"take": 1}, {"take": 2}], "pick": None}}
    gc.set_pick(state, "MUS_M02_Calm", 2)
    assert state["MUS_M02_Calm"]["pick"] == 2
    with pytest.raises(gc.SheetError):
        gc.set_pick(state, "MUS_M02_Calm", 5)
    with pytest.raises(gc.SheetError):
        gc.set_pick(state, "MUS_M02_Other", 1)


def test_provenance_has_what_the_asset_log_needs(tmp_path):
    sheet = gc.load_sheet(write_sheet(tmp_path, [LOOP]))
    cue = sheet["cues"]["MUS_M02_Calm"]
    take = {"take": 2, "seed": 1234, "model": "acestep-v15-turbo", "lm_model": None, "quality": "high"}
    record = gc.provenance(sheet, cue, take, finished={"frames": 10, "sample_rate": 48000},
                           tool_versions={"fork": "abc123", "ace_step": "def456"})
    for key in ("cue", "type", "sheet", "caption", "bpm", "key", "seed", "take", "model", "quality",
                "tool_versions", "generated_at", "finished", "ai_generated"):
        assert key in record
    assert record["ai_generated"] is True
    assert record["seed"] == 1234
