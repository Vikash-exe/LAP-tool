"""Stopping a running search job (the Stop button).

A job runs in its own thread. `begin()` ties the thread to the job; from then on `check()` raises `Cancelled` once a stop
was requested, and every database connection the thread opens is remembered (`track`) so that `request()` can also
cancel the statement it is running right now (pg_cancel_backend), e.g. a long index build or search query.
"""
from __future__ import annotations

import threading

import psycopg

from . import config


class Cancelled(Exception):
    """The user pressed Stop."""


_local = threading.local()
_lock = threading.Lock()
_jobs: dict[str, dict] = {}  # job id -> {"event": Event, "pids": set of backend pids}


def begin(job_id: str) -> None:
    with _lock:
        _jobs[job_id] = {"event": threading.Event(), "pids": set()}
    _local.job_id = job_id


def end(job_id: str) -> None:
    with _lock:
        _jobs.pop(job_id, None)
    _local.job_id = None


def _mine() -> dict | None:
    job_id = getattr(_local, "job_id", None)
    return _jobs.get(job_id) if job_id else None


def requested() -> bool:
    """True when the job this thread belongs to was asked to stop."""
    entry = _mine()
    return bool(entry and entry["event"].is_set())


def check() -> None:
    if requested():
        raise Cancelled()


def track(conn: psycopg.Connection) -> psycopg.Connection:
    """Remember a connection opened by the job's thread, so a stop can cancel its running statement."""
    entry = _mine()
    if entry is not None:
        entry["pids"].add(conn.info.backend_pid)
    return conn


def request(job_id: str) -> bool:
    """Ask a job to stop and cancel the statements it is running. False if there is no such running job."""
    with _lock:
        entry = _jobs.get(job_id)
        if entry is None:
            return False
        entry["event"].set()
        pids = list(entry["pids"])
    if pids:
        try:
            with psycopg.connect(**config.DB, connect_timeout=10, autocommit=True) as conn:
                for pid in pids:
                    conn.execute("SELECT pg_cancel_backend(%s)", (pid,))
        except Exception:
            pass  # the thread still stops at its next check
    return True
