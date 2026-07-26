"""Command + segment-math checks for the ffmpeg trim worker.

No pytest in this repo (CLAUDE.md rule #1) -- run with:
    .venv/Scripts/python -m video_pipeline.tests.test_trim_cmd

ffmpeg can't be exercised against real match video on the dev box, so this
locks down everything that CAN be checked without it: segment normalisation,
chunking, the filtergraph, and -- the point of the 2026-07-26 rewrite -- that the
generated command only ever asks ffmpeg to decode the footage we keep.

The end-to-end behaviour (does ffmpeg actually honour these args, does the
concat-demuxer join hold) is covered by video_pipeline/tests/e2e_trim_docker.py,
which runs a real ffmpeg over a synthetic clip in Docker.
"""
from __future__ import annotations

import sys
from pathlib import Path

from video_pipeline.ffmpeg_trim_worker import (
    _build_concat_demuxer_cmd,
    _build_concat_filter,
    _build_pass_cmd,
    _chunk,
    _normalize_segments,
    _parse_fps,
    _redact,
    _sum_segment_durations,
    _write_concat_list,
)

FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def eq(got, want, label: str) -> None:
    check(got == want, label, f"got={got!r} want={want!r}")


# ============================================================
# Segment normalisation
# ============================================================

def test_normalize() -> None:
    print("_normalize_segments")

    segs = [{"start_s": 10, "end_s": 20}, {"start_s": 5, "end_s": 8}]
    eq(_normalize_segments(segs, 100.0), [(5.0, 8.0), (10.0, 20.0)], "sorts by start")

    eq(_normalize_segments([{"start_s": 90, "end_s": 200}], 100.0),
       [(90.0, 100.0)], "clamps end to source duration")

    eq(_normalize_segments([{"start_s": -5, "end_s": 10}], 100.0),
       [(0.0, 10.0)], "clamps negative start to 0")

    eq(_normalize_segments([{"start_s": 10, "end_s": 10.1}], 100.0),
       [], "drops sub-MIN_KEEP_SEGMENT_S segment")

    eq(_normalize_segments([{"start_s": 30, "end_s": 20}], 100.0),
       [], "drops inverted segment")

    eq(_normalize_segments([{"start_s": 120, "end_s": 130}], 100.0),
       [], "drops segment entirely past source end")

    dup = [{"start_s": 5, "end_s": 9}, {"start_s": 5, "end_s": 9}]
    eq(_normalize_segments(dup, 100.0), [(5.0, 9.0)], "de-duplicates exact repeats")

    try:
        _normalize_segments([{"start_s": "x", "end_s": 9}], 100.0)
        check(False, "raises on non-numeric payload")
    except ValueError:
        check(True, "raises on non-numeric payload")

    eq(_sum_segment_durations([(0.0, 5.0), (10.0, 12.5)]), 7.5, "sums durations")


# ============================================================
# Chunking
# ============================================================

def test_chunk() -> None:
    print("_chunk")
    items = list(range(10))
    eq(list(_chunk(items, 4)), [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]], "splits into chunks")
    eq(list(_chunk(items, 0)), [items], "size 0 = single chunk (escape hatch)")
    eq(list(_chunk(items, 99)), [items], "size >= len = single chunk")
    eq(list(_chunk([1], 4)), [[1]], "single item")
    # 86 segments (the measured df594aea shape) at the default 4 per pass.
    # The default is 4 because 8 concurrent 1080p inputs were SIGKILLed (OOM) on
    # a 0.5 CPU / 512 MB box — see TRIM_SEEK_INPUTS_PER_PASS.
    from video_pipeline.ffmpeg_trim_worker import TRIM_SEEK_INPUTS_PER_PASS
    check(0 < TRIM_SEEK_INPUTS_PER_PASS <= 6,
          f"default inputs-per-pass stays inside the measured-safe range "
          f"(got {TRIM_SEEK_INPUTS_PER_PASS}, OOM observed at 8)")
    eq(len(list(_chunk(list(range(86)), 4))), 22, "86 segments -> 22 passes at default")
    eq(sum(len(c) for c in _chunk(list(range(86)), 4)), 86, "chunking loses no segments")


# ============================================================
# Filtergraph
# ============================================================

def test_concat_filter() -> None:
    print("_build_concat_filter")

    eq(_build_concat_filter(2, True),
       "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]", "2 inputs with audio")

    eq(_build_concat_filter(3, False),
       "[0:v][1:v][2:v]concat=n=3:v=1:a=0[outv]", "3 inputs, video only")

    eq(_build_concat_filter(1, True),
       "[0:v][0:a]concat=n=1:v=1:a=1[outv][outa]", "single input is still valid")

    f = _build_concat_filter(5, True)
    check("trim=" not in f and "split=" not in f,
          "no trim/split filter -- that was the whole-source-decode bug")

    try:
        _build_concat_filter(0, True)
        check(False, "rejects zero inputs")
    except ValueError:
        check(True, "rejects zero inputs")

    # Optional downscale (TRIM_MAX_HEIGHT)
    fs = _build_concat_filter(2, True, 720)
    check("[0:v]scale=-2:720[v0]" in fs and "[1:v]scale=-2:720[v1]" in fs,
          "scales every input when scale_height set")
    check("[v0][0:a][v1][1:a]concat=n=2:v=1:a=1[outv][outa]" in fs,
          "concat consumes the scaled labels")
    check("scale=-2:720" in fs and "scale=720" not in fs,
          "scale keeps aspect + even width (-2)")
    check("scale" not in _build_concat_filter(2, True, 0),
          "no scale filter when scale_height=0 (default)")

    fs_na = _build_concat_filter(2, False, 540)
    check("[v0][v1]concat=n=2:v=1:a=0[outv]" in fs_na,
          "scaled video-only concat is well-formed")


# ============================================================
# Pass command -- the decode-budget guarantee
# ============================================================

def _pass_cmd(segments, **kw):
    defaults = dict(
        src="/tmp/src.mov",
        segments=segments,
        has_audio=True,
        filter_script=Path("/tmp/f.txt"),
        out_path=Path("/tmp/out.mp4"),
        stream_input=False,
        force_fps=None,
    )
    defaults.update(kw)
    return _build_pass_cmd(**defaults)


def test_pass_cmd() -> None:
    print("_build_pass_cmd")

    segs = [(10.0, 20.0), (100.5, 130.25)]
    cmd = _pass_cmd(segs)

    eq(cmd.count("-i"), 2, "one input per segment")

    # -ss must come BEFORE its -i (input seek, not output seek): that is what
    # makes ffmpeg range-request/seek instead of decoding from 0.
    for start, _ in segs:
        i = cmd.index(f"{start:.3f}")
        check(cmd[i - 1] == "-ss" and "-i" in cmd[i:], f"-ss {start} precedes its -i")

    # -t (duration), not -to: as an input option -t is unambiguously relative
    # to the seek point.
    eq([cmd[i + 1] for i, a in enumerate(cmd) if a == "-t"],
       ["10.000", "29.750"], "-t carries each segment duration")
    check("-to" not in cmd, "does not use ambiguous input -to")

    # THE regression guard: ffmpeg is asked to read only the kept footage.
    decoded = sum(float(cmd[i + 1]) for i, a in enumerate(cmd) if a == "-t")
    eq(round(decoded, 3), _sum_segment_durations(segs),
       "total requested decode == total kept footage")
    check(decoded < 45.0, "decode budget is segment-sized, not source-sized")

    eq(cmd[:2], ["ffmpeg", "-y"], "starts with ffmpeg -y")
    check("-nostdin" in cmd, "passes -nostdin (detached subprocess has no stdin)")
    # str(Path(...)) is backslashed on Windows, so compare the same way
    check(cmd[-1] == str(Path("/tmp/out.mp4")), "output path is last")
    check("[outv]" in cmd and "[outa]" in cmd, "maps both output labels when audio")

    # No audio -> no audio map/codec
    cmd_na = _pass_cmd(segs, has_audio=False)
    check("[outa]" not in cmd_na, "no audio map when source has no audio")
    check("-c:a" not in cmd_na, "no audio codec when source has no audio")
    check("[outv]" in cmd_na, "still maps video when source has no audio")

    # Reconnect flags: per-input, streaming only
    cmd_stream = _pass_cmd(segs, stream_input=True, src="https://example/s3?X-Amz-Signature=abc")
    eq(cmd_stream.count("-reconnect"), 2, "reconnect flags repeat per HTTP input")
    check("-reconnect" not in cmd, "no reconnect flags for a local source")

    # CFR forcing only when asked (multi-pass)
    cmd_fps = _pass_cmd(segs, force_fps=29.97)
    check("-r" in cmd_fps and "-fps_mode" in cmd_fps, "forces CFR when force_fps set")
    check("-vsync" not in cmd_fps, "uses -fps_mode, not the deprecated -vsync")
    check("-ar" in cmd_fps and "48000" in cmd_fps, "normalises audio rate when force_fps set")
    check("-r" not in cmd, "single pass does not force CFR")

    try:
        _pass_cmd([(10.0, 10.0)])
        check(False, "rejects zero-length segment")
    except ValueError:
        check(True, "rejects zero-length segment")


# ============================================================
# Concat demuxer join
# ============================================================

def test_concat_demuxer(tmp: Path) -> None:
    print("concat demuxer join")

    cmd = _build_concat_demuxer_cmd(tmp / "parts.txt", tmp / "review.mp4")
    check("-c" in cmd and "copy" in cmd, "joins with stream copy (no re-encode)")
    check("-safe" in cmd and "0" in cmd, "passes -safe 0")
    check("concat" in cmd, "uses the concat demuxer")

    lst = tmp / "parts.txt"
    _write_concat_list([tmp / "part_0000.mp4", tmp / "part_0001.mp4"], lst)
    body = lst.read_text(encoding="utf-8")
    eq(body, "file 'part_0000.mp4'\nfile 'part_0001.mp4'\n", "list uses relative names")


# ============================================================
# Small helpers
# ============================================================

def test_helpers() -> None:
    print("helpers")

    eq(_parse_fps("30000/1001"), 30000 / 1001, "parses rational fps")
    eq(_parse_fps("25/1"), 25.0, "parses integer rational")
    eq(_parse_fps("0/0"), None, "rejects 0/0")
    eq(_parse_fps(""), None, "rejects empty")
    eq(_parse_fps("N/A"), None, "rejects N/A")
    eq(_parse_fps("100000/1"), None, "rejects implausible fps")

    red = _redact(["ffmpeg", "-i", "https://b.s3.amazonaws.com/k?X-Amz-Signature=deadbeef"])
    check("deadbeef" not in red, "redacts presigned signature from logged cmd")
    check("<presigned-url>" in red, "leaves a placeholder for the URL")
    eq(_redact(["ffmpeg", "-i", "/tmp/x.mov"]), "ffmpeg -i /tmp/x.mov", "leaves local paths alone")


# ============================================================

def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_normalize()
        test_chunk()
        test_concat_filter()
        test_pass_cmd()
        test_concat_demuxer(tmp)
        test_helpers()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("all trim command/segment checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
