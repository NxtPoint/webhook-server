"""Docker Postgres lifecycle helper for the silver-builder bench.

Spins up an ephemeral Postgres container on a known port, exposes a
connection string + sqlalchemy engine, supports teardown. The bench's
unit of work is "spin up → restore bronze fixture → run silver builder →
query results → compare baseline → tear down (or reuse)".

Design choice: subprocess + docker CLI, not docker-py. One less Python
dep; `docker ps` works the same on the box, Tomo's machine, and CI;
no Docker socket fiddling. The docker CLI is a hard prereq either way.

Container name + host port are deterministic so re-runs reuse the running
container instead of paying ~10-15s of startup per bench run. Use
`teardown()` (or `bench_silver --teardown`) to clean up explicitly.

This module owns the container lifecycle only. Schema initialization and
fixture restore are the bench orchestrator's responsibility — they read
the connection string from `start()` and apply their own DDL + data.
"""
from __future__ import annotations

import logging
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)


# ---- Container identity ----
CONTAINER_NAME = "ten-fifty5-silver-bench-pg"
HOST_PORT = 5544                 # avoid default 5432; less collision risk
PG_IMAGE = "postgres:15-alpine"
PG_USER = "bench"
PG_PASSWORD = "bench"            # local-only, never exposed off-host
PG_DB = "bench"
READY_TIMEOUT_SEC = 30


def connection_string(*, host: str = "localhost", port: int = HOST_PORT) -> str:
    """psycopg-compatible URL for the bench Postgres."""
    return f"postgresql+psycopg://{PG_USER}:{PG_PASSWORD}@{host}:{port}/{PG_DB}"


def is_running() -> bool:
    """True iff a container with our deterministic name is in the running state."""
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--filter", f"name=^{CONTAINER_NAME}$",
             "--format", "{{.Names}}"],
            text=True,
        )
        return out.strip() == CONTAINER_NAME
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def is_present() -> bool:
    """True iff the container exists at all (running or stopped). Used to
    decide between `docker start` (resume an existing container) and
    `docker run` (create new)."""
    try:
        out = subprocess.check_output(
            ["docker", "ps", "-a", "--filter", f"name=^{CONTAINER_NAME}$",
             "--format", "{{.Names}}"],
            text=True,
        )
        return out.strip() == CONTAINER_NAME
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _wait_for_ready(timeout_sec: int = READY_TIMEOUT_SEC) -> None:
    """Block until `pg_isready` succeeds inside the container, or timeout."""
    deadline = time.time() + timeout_sec
    last_err = None
    while time.time() < deadline:
        try:
            subprocess.check_output(
                ["docker", "exec", CONTAINER_NAME, "pg_isready",
                 "-U", PG_USER, "-d", PG_DB],
                text=True, stderr=subprocess.STDOUT,
            )
            return
        except subprocess.CalledProcessError as e:
            last_err = e
            time.sleep(0.5)
    raise RuntimeError(
        f"Postgres container {CONTAINER_NAME!r} not ready within "
        f"{timeout_sec}s. Last pg_isready output: "
        f"{last_err.output if last_err else 'n/a'}"
    )


def start(*, reuse: bool = True) -> str:
    """Ensure the bench Postgres is running. Returns the connection string.

    If a container with our name is already running and `reuse=True` (the
    default), short-circuit and return its connection string. Otherwise
    (re-)create the container.

    Idempotent across bench invocations — designed to be the "first call"
    pattern: `start()` returns a usable URL every time.
    """
    if reuse and is_running():
        logger.info("bench_silver Postgres already running (reuse=True)")
        return connection_string()

    if is_present():
        # Container exists but stopped — `docker start` resumes it.
        logger.info("Starting existing bench_silver Postgres container")
        subprocess.run(["docker", "start", CONTAINER_NAME], check=True,
                       stdout=subprocess.DEVNULL)
    else:
        logger.info(
            "Creating bench_silver Postgres container %s on port %d (image=%s)",
            CONTAINER_NAME, HOST_PORT, PG_IMAGE,
        )
        subprocess.run([
            "docker", "run", "-d",
            "--name", CONTAINER_NAME,
            "-e", f"POSTGRES_USER={PG_USER}",
            "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "-e", f"POSTGRES_DB={PG_DB}",
            "-p", f"{HOST_PORT}:5432",
            PG_IMAGE,
        ], check=True, stdout=subprocess.DEVNULL)

    _wait_for_ready()
    url = connection_string()
    logger.info("bench_silver Postgres ready: %s", url)
    return url


def stop(*, remove: bool = True) -> None:
    """Stop the bench Postgres container. If ``remove`` (default), also
    delete the container — fully clean state on next ``start()``. Set
    ``remove=False`` to keep the data for inspection.
    """
    if not is_present():
        logger.info("bench_silver Postgres not present — nothing to stop")
        return

    if is_running():
        logger.info("Stopping bench_silver Postgres container")
        subprocess.run(["docker", "stop", CONTAINER_NAME], check=True,
                       stdout=subprocess.DEVNULL)

    if remove:
        logger.info("Removing bench_silver Postgres container")
        subprocess.run(["docker", "rm", CONTAINER_NAME], check=True,
                       stdout=subprocess.DEVNULL)


def reset() -> str:
    """Tear down (if present) and create a fresh container. Returns the new
    connection string. Use this when the bench needs guaranteed-clean state
    (e.g. when loading a fresh fixture)."""
    stop(remove=True)
    return start(reuse=False)


def get_engine(connection_string_override: Optional[str] = None):
    """SQLAlchemy engine factory. Lazy-imports sqlalchemy so this module
    can be imported and inspected without the dep."""
    from sqlalchemy import create_engine
    return create_engine(connection_string_override or connection_string())


def exec_psql(sql: str) -> str:
    """Execute a SQL statement via `docker exec ... psql` and return stdout.

    Useful for one-shot DDL or restore commands that should not go through
    SQLAlchemy (e.g. `\\i fixture.sql`, `pg_restore`). Returns the trimmed
    output for inspection; raises on non-zero exit.
    """
    out = subprocess.check_output(
        ["docker", "exec", "-i", CONTAINER_NAME,
         "psql", "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-c", sql],
        text=True, stderr=subprocess.STDOUT,
    )
    return out.strip()


if __name__ == "__main__":
    # Smoke test: start, ping, stop. `python -m ml_pipeline.diag.bench_silver.db_helper`.
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("reset")
    sub.add_parser("status")
    sub.add_parser("ping")
    args = ap.parse_args()

    if args.cmd == "start":
        url = start()
        print(url)
    elif args.cmd == "stop":
        stop()
    elif args.cmd == "reset":
        url = reset()
        print(url)
    elif args.cmd == "status":
        print("running" if is_running()
              else "stopped" if is_present()
              else "absent")
    elif args.cmd == "ping":
        out = exec_psql("SELECT version();")
        print(out)
