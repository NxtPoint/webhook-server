"""In-flight lock checks for the video trim worker.

No pytest (CLAUDE.md rule #1) — run with:
    .venv/Scripts/python -m video_pipeline.tests.test_trim_lock

Guards the fix for the 2026-07-26 OOM: the main API's stale-trim sweep re-fires
a long-running trim because it cannot see inside the worker, and /trim used to
spawn another ffmpeg unconditionally. Two concurrent encodes of a multi-GB source
exhausted the instance. The lock must make a duplicate a no-op while the holder
lives, and must NOT wedge a task whose holder died.

POSIX-only paths (os.kill signal 0) are skipped on Windows; the rest runs
everywhere so the dev box still gets coverage.
"""
from __future__ import annotations

import os
import sys
import tempfile

FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def main() -> int:
    # Point the lock dir somewhere disposable BEFORE importing the app, and
    # satisfy the module's required-env guard.
    tmp = tempfile.mkdtemp(prefix="trimlock_")
    os.environ["TRIM_LOCK_DIR"] = tmp
    os.environ.setdefault("VIDEO_WORKER_OPS_KEY", "test-key-not-used")

    from video_pipeline.video_worker_app import (
        _acquire_trim_lock,
        _lock_path,
        _pid_alive,
        _release_trim_lock,
        _write_lock_pid,
    )

    task = "df594aea-78ef-47b1-8c10-60174a58d8b0"

    print("acquire / duplicate / release")
    acquired, holder = _acquire_trim_lock(task)
    check(acquired and holder == 0, "first acquire succeeds")
    check(os.path.exists(_lock_path(task)), "lock file created")

    # A live holder must block a duplicate. This process is certainly alive.
    _write_lock_pid(task, os.getpid())
    acquired2, holder2 = _acquire_trim_lock(task)
    check(not acquired2, "duplicate refused while holder is alive")
    check(holder2 == os.getpid(), f"reports the holder pid (got {holder2})")

    _release_trim_lock(task)
    check(not os.path.exists(_lock_path(task)), "release removes the lock")

    acquired3, _ = _acquire_trim_lock(task)
    check(acquired3, "re-acquire works after release")
    _release_trim_lock(task)

    print("stale-lock takeover")
    # A lock naming a dead PID must be reclaimable, or a crashed trim could
    # never be re-fired — the opposite failure mode.
    if os.name == "posix":
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)          # reap → PID is now dead
        check(not _pid_alive(pid), "reaped child reads as not alive")
        _acquire_trim_lock(task)
        _write_lock_pid(task, pid)
        acquired4, _ = _acquire_trim_lock(task)
        check(acquired4, "stale lock (dead holder) is taken over")
        _release_trim_lock(task)
    else:
        # Same logic, without fork: a PID that cannot exist.
        _acquire_trim_lock(task)
        _write_lock_pid(task, 2 ** 31 - 1)
        acquired4, _ = _acquire_trim_lock(task)
        check(acquired4, "stale lock (implausible pid) is taken over")
        _release_trim_lock(task)
        print("  note: fork-based liveness path is POSIX-only, skipped here")

    print("edge cases")
    # An empty lock file (crash between create and pid write) must not wedge.
    open(_lock_path(task), "w", encoding="utf-8").close()
    acquired5, _ = _acquire_trim_lock(task)
    check(acquired5, "empty lock file (no pid recorded) is taken over")
    _release_trim_lock(task)

    # Releasing a lock that isn't there must be silent, not an error.
    try:
        _release_trim_lock(task)
        check(True, "releasing a missing lock is a no-op")
    except Exception as e:
        check(False, "releasing a missing lock is a no-op", str(e))

    # Distinct tasks must not block each other.
    a_ok, _ = _acquire_trim_lock("task-aaa")
    _write_lock_pid("task-aaa", os.getpid())
    b_ok, _ = _acquire_trim_lock("task-bbb")
    check(a_ok and b_ok, "different task_ids get independent locks")
    _release_trim_lock("task-aaa")
    _release_trim_lock("task-bbb")

    # A task_id with path separators must not escape the lock dir.
    nasty = "../../etc/passwd"
    p = _lock_path(nasty)
    check(os.path.dirname(os.path.abspath(p)) == os.path.abspath(tmp),
          f"path traversal in task_id is neutralised (got {p})")

    check(not _pid_alive(0) and not _pid_alive(-1), "pid 0/-1 are not 'alive'")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("all trim-lock checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
