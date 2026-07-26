"""End-to-end check of the trim encode path against a REAL ffmpeg.

Runs INSIDE a container that has the same ffmpeg as the video-worker image
(python:3.11-slim + apt ffmpeg). No S3, no DB, no match footage — it generates a
synthetic clip with a burnt-in timecode and a tone, then drives the real
_build_pass_cmd / concat-demuxer code over it and asserts the output durations.

Run from the repo root (Windows PowerShell):

    docker build -t tf-ffmpeg-test -f <scratch>/Dockerfile.ffprobe <scratch>
    docker run --rm -v "${PWD}:/app" -w /app tf-ffmpeg-test `
        python -m video_pipeline.tests.e2e_trim_docker

It skips itself with a clear message if ffmpeg is absent, so it is safe to run
anywhere. This is what covers the parts test_trim_cmd.py cannot: that ffmpeg
actually honours per-input -ss/-t, that the concat filter joins N seek inputs,
and that the multi-pass concat-demuxer join produces one continuous file.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Import the real implementation — this test exercises production code paths,
# not a re-implementation of them.
from video_pipeline.ffmpeg_trim_worker import (
    FFMPEG_BIN,
    _build_concat_demuxer_cmd,
    _build_concat_filter,
    _build_pass_cmd,
    _chunk,
    _normalize_segments,
    _probe_duration,
    _probe_source,
    _run,
    _sum_segment_durations,
    _write_concat_list,
)

FAILURES: list[str] = []
SRC_DURATION_S = 60
SRC_FPS = 30


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def close(got: float, want: float, tol: float, label: str) -> None:
    check(abs(got - want) <= tol, label, f"got={got:.3f} want={want:.3f} tol={tol}")


def _make_source(path: Path, *, with_audio: bool) -> None:
    """A 60s 640x360 clip: moving test pattern + (optionally) a tone.

    Keyframes every 2s (-g 60) so input-seek has realistic keyframe spacing to
    land on, which is what makes the accurate-seek behaviour meaningful here.
    """
    cmd = [
        FFMPEG_BIN, "-y", "-hide_banner", "-nostdin",
        "-f", "lavfi", "-i", f"testsrc=size=640x360:rate={SRC_FPS}:duration={SRC_DURATION_S}",
    ]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={SRC_DURATION_S}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-b:a", "64k", "-shortest"]
    cmd += [str(path)]
    _run(cmd, timeout=180)


def _encode(src: Path, segments, *, has_audio: bool, out: Path, td: Path,
            force_fps: float | None = None) -> None:
    fscript = td / f"filter_{out.stem}.txt"
    fscript.write_text(_build_concat_filter(len(segments), has_audio), encoding="utf-8")
    _run(_build_pass_cmd(
        src=str(src), segments=segments, has_audio=has_audio,
        filter_script=fscript, out_path=out,
        stream_input=False, force_fps=force_fps,
    ), timeout=600)


def test_single_pass(td: Path, src: Path, has_audio: bool) -> None:
    print(f"single pass, N seek inputs (has_audio={has_audio})")

    raw = [{"start_s": 5.0, "end_s": 11.5}, {"start_s": 20.0, "end_s": 27.0},
           {"start_s": 48.0, "end_s": 55.25}]
    segs = _normalize_segments(raw, float(SRC_DURATION_S))
    expected = _sum_segment_durations(segs)

    out = td / f"single_{int(has_audio)}.mp4"
    _encode(src, segs, has_audio=has_audio, out=out, td=td)

    check(out.exists() and out.stat().st_size > 0, "produced an output file")
    got = _probe_duration(out)
    # One frame of slack per segment boundary for container rounding.
    close(got, expected, 3 * (1.0 / SRC_FPS) + 0.25, f"output duration == kept {expected}s")

    info = _probe_source(str(out))
    check(info.has_audio == has_audio, "output audio presence matches source")


def test_multipass_join(td: Path, src: Path) -> None:
    """The path a long match takes: several passes, then a stream-copy join."""
    print("multi-pass + concat-demuxer join")

    raw = [{"start_s": float(s), "end_s": float(s) + 4.0} for s in range(2, 50, 6)]
    segs = _normalize_segments(raw, float(SRC_DURATION_S))
    expected = _sum_segment_durations(segs)
    batches = list(_chunk(segs, 3))
    check(len(batches) > 1, f"test exercises multiple passes (got {len(batches)})")

    info = _probe_source(str(src))
    parts: list[Path] = []
    for i, batch in enumerate(batches):
        part = td / f"part_{i:04d}.mp4"
        _encode(src, batch, has_audio=info.has_audio, out=part, td=td, force_fps=info.fps)
        check(part.exists() and part.stat().st_size > 0, f"pass {i + 1} produced a part")
        parts.append(part)

    lst = td / "parts.txt"
    _write_concat_list(parts, lst)
    joined = td / "joined.mp4"
    _run(_build_concat_demuxer_cmd(lst, joined), timeout=600)

    check(joined.exists(), "concat demuxer produced the final file")
    got = _probe_duration(joined)
    close(got, expected, len(segs) * (1.0 / SRC_FPS) + 0.5,
          f"joined duration == total kept {expected}s")

    # The join must not silently drop parts: it should exceed any single part.
    longest_part = max(_probe_duration(p) for p in parts)
    check(got > longest_part, "joined file is longer than its longest part")


def test_seek_accuracy(td: Path, src: Path) -> None:
    """Input -ss is keyframe-based, but ffmpeg's default accurate_seek decodes
    forward to the exact timestamp. Cut mid-GOP (keyframes are every 2s here)
    and confirm the duration is still what we asked for, not keyframe-rounded."""
    print("mid-GOP cut accuracy (accurate_seek)")

    segs = _normalize_segments([{"start_s": 7.37, "end_s": 12.71}], float(SRC_DURATION_S))
    expected = _sum_segment_durations(segs)
    out = td / "midgop.mp4"
    info = _probe_source(str(src))
    _encode(src, segs, has_audio=info.has_audio, out=out, td=td)

    got = _probe_duration(out)
    close(got, expected, 0.3, f"mid-GOP cut duration == requested {expected}s")


def main() -> int:
    if not shutil.which(FFMPEG_BIN):
        print(f"SKIP: {FFMPEG_BIN} not on PATH — run this inside the test container "
              f"(see module docstring)")
        return 0

    ver = subprocess.run([FFMPEG_BIN, "-version"], capture_output=True, text=True)
    print(f"ffmpeg: {ver.stdout.splitlines()[0] if ver.stdout else '?'}\n")

    with tempfile.TemporaryDirectory() as td_raw:
        td = Path(td_raw)

        src_av = td / "src_av.mp4"
        print(f"building synthetic source ({SRC_DURATION_S}s, {SRC_FPS}fps, with audio)")
        _make_source(src_av, with_audio=True)
        info = _probe_source(str(src_av))
        print(f"  source: {info.duration_s:.2f}s has_audio={info.has_audio} "
              f"fps={info.fps:.3f}\n")

        test_single_pass(td, src_av, has_audio=True)
        test_multipass_join(td, src_av)
        test_seek_accuracy(td, src_av)

        src_v = td / "src_v.mp4"
        print("\nbuilding video-only source")
        _make_source(src_v, with_audio=False)
        test_single_pass(td, src_v, has_audio=False)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("all end-to-end ffmpeg trim checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
