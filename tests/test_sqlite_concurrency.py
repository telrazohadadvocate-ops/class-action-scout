#!/usr/bin/env python3
"""
Guard test for the SQLite concurrency setup in database/models.py.

Two things here break silently and only surface as a dead backfill hours in:

  1. The WAL / busy_timeout pragmas must be applied to EVERY connection, not
     just the first. busy_timeout is per-connection, so a listener that fires
     once leaves later connections back on pysqlite's 5s default.

  2. commit_with_retry() must snapshot the pending values BEFORE calling
     commit. A failed flush has already rolled back and expired every
     instance, so a snapshot taken in the exception handler is empty — the
     retry then writes nothing, leaves the STALE row in place, and reports
     success. Restoring must also emit no SQL: a plain setattr on an expired
     attribute fires a lazy load, which autoflushes straight back into the
     lock being waited on.

Runs offline against a throwaway SQLite DB, using a real second connection
holding BEGIN EXCLUSIVE to produce genuine "database is locked" errors.

Usage:  python tests/test_sqlite_concurrency.py
"""
import os, sys, time, shutil, sqlite3, tempfile, threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError, OperationalError

from database.models import (
    BUSY_TIMEOUT_MS, init_database, get_session, commit_with_retry,
    _restore_pending, _snapshot_pending, Lead, RawSource,
)

FAILURES = []
TMP = tempfile.mkdtemp(prefix="scout_conc_")
DB_PATH = os.path.join(TMP, "t.db").replace("\\", "/")
DB_URL = "sqlite:///" + DB_PATH

# Short waits keep the test quick; the production values are asserted
# separately in test_pragmas_on_every_connection.
FAST_BUSY_MS = 200
LOCK_HELD_SEC = 1.5
RETRY_DELAY = 0.3

_sessions = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def new_session():
    s = get_session(DB_URL)
    _sessions.append(s)
    return s


def fail_fast(session):
    """
    Drop busy_timeout on this session's connection, and on any it opens later.

    Without the listener a pool reconnect would silently restore the 30s
    production timeout and stall the test for half a minute per attempt.
    """
    engine = session.get_bind()

    @event.listens_for(engine, "connect")
    def _shorten(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=%d" % FAST_BUSY_MS)
        cur.close()

    session.execute(text("PRAGMA busy_timeout=%d" % FAST_BUSY_MS))


def hold_write_lock(seconds):
    """Take a real exclusive write lock on another connection, then release it."""
    ready = threading.Event()

    def run():
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.execute("BEGIN EXCLUSIVE")
        ready.set()
        time.sleep(seconds)
        conn.execute("ROLLBACK")
        conn.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    ready.wait(5)
    return t


# ── tests ──────────────────────────────────────────────

def test_pragmas_on_every_connection():
    print("\n### WAL + busy_timeout are set on every connection")
    a = new_session()
    check("journal_mode is WAL",
          a.execute(text("PRAGMA journal_mode")).scalar() == "wal")
    check("busy_timeout is %dms" % BUSY_TIMEOUT_MS,
          a.execute(text("PRAGMA busy_timeout")).scalar() == BUSY_TIMEOUT_MS)

    # A second, independently created engine must be configured too — that is
    # what a background script gets while the web app already holds one.
    b = new_session()
    check("second independent connection is WAL too",
          b.execute(text("PRAGMA journal_mode")).scalar() == "wal")
    check("second independent connection keeps the long busy_timeout",
          b.execute(text("PRAGMA busy_timeout")).scalar() == BUSY_TIMEOUT_MS)

    # Hold two connections open at once so the pool is forced to open a second
    # DBAPI connection on the SAME engine. Configuration that runs once per
    # engine (or once per process) leaves this one on pysqlite's 5s default.
    engine = a.get_bind()
    c1 = engine.connect()
    c2 = engine.connect()
    try:
        check("a concurrently-opened connection on the same engine is WAL",
              c2.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal")
        check("a concurrently-opened connection on the same engine has the "
              "long busy_timeout",
              c2.exec_driver_sql("PRAGMA busy_timeout").scalar() == BUSY_TIMEOUT_MS)
    finally:
        c1.close()
        c2.close()


def test_reader_not_blocked_by_writer():
    print("\n### WAL lets a reader through an open write transaction")
    writer, reader = new_session(), new_session()
    writer.add(Lead(title="pre-existing", priority_score=1.0))
    commit_with_retry(writer)

    writer.add(Lead(title="uncommitted writer row", priority_score=2.0))
    writer.flush()                      # holds the write lock, not committed
    try:
        reader.execute(text("PRAGMA busy_timeout=200"))
        titles = [t for (t,) in reader.query(Lead.title).all()]
        check("reader sees committed rows while a write is open",
              "pre-existing" in titles)
        check("reader does not see the uncommitted row",
              "uncommitted writer row" not in titles)
    except OperationalError as e:
        check("reader blocked by open write transaction: %s" % e, False)
    finally:
        writer.rollback()


def test_insert_recovers_from_lock():
    print("\n### an INSERT blocked by a lock is retried, not lost")
    db = new_session()
    fail_fast(db)
    t = hold_write_lock(LOCK_HELD_SEC)

    db.add(Lead(title="inserted under lock", company="ACME", priority_score=7.7))
    commit_with_retry(db, attempts=8, base_delay=RETRY_DELAY)
    t.join()

    fresh = new_session()
    row = fresh.query(Lead).filter_by(title="inserted under lock").one_or_none()
    check("row eventually landed", row is not None)
    check("inserted values intact",
          row is not None and (row.company, row.priority_score) == ("ACME", 7.7))


def test_update_recovers_with_values_intact():
    """The regression that reported success while persisting stale values."""
    print("\n### an UPDATE blocked by a lock keeps its pending values")
    db = new_session()
    fail_fast(db)
    lead = db.query(Lead).filter_by(title="inserted under lock").one()
    check("starting from the pre-update value", lead.priority_score == 7.7)

    t = hold_write_lock(LOCK_HELD_SEC)
    lead.priority_score = 9.9
    lead.notes = "updated under lock"
    lead.status = "pursuing"
    commit_with_retry(db, attempts=8, base_delay=RETRY_DELAY)
    t.join()

    fresh = new_session()
    row = fresh.query(Lead).filter_by(title="inserted under lock").one()
    check("new priority_score persisted (not the stale one)", row.priority_score == 9.9)
    check("new notes persisted", row.notes == "updated under lock")
    check("new status persisted", row.status == "pursuing")
    check("untouched column not clobbered", row.company == "ACME")


def test_restore_emits_no_sql():
    """
    Re-applying a snapshot must not touch the database.

    A plain setattr on an expired attribute fires a lazy load; with autoflush
    live that load flushes the half-restored state straight back into the lock
    being waited on, and the retry deadlocks instead of recovering.
    """
    print("\n### restoring a snapshot issues no SQL")
    db = new_session()
    lead = db.query(Lead).filter_by(title="inserted under lock").one()
    lead.notes = "pending value"
    snapshot = _snapshot_pending(db)
    db.rollback()                       # what a failed commit leaves behind
    check("rollback expired the instance", "notes" not in lead.__dict__)

    engine = db.get_bind()
    statements = []

    @event.listens_for(engine, "after_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    try:
        _restore_pending(db, snapshot)
        check("no statement issued while restoring", statements == [])
        check("restored value is pending again", lead.notes == "pending value")
        check("object is dirty, so the retry will write it", lead in db.dirty)
    finally:
        event.remove(engine, "after_cursor_execute", _count)
        db.rollback()


def test_non_lock_error_is_not_retried():
    print("\n### a non-lock error is re-raised immediately")
    db = new_session()
    db.add(RawSource(url="https://example.test/dup", title="first"))
    commit_with_retry(db)

    db.add(RawSource(url="https://example.test/dup", title="second"))
    t0 = time.time()
    try:
        commit_with_retry(db, attempts=5, base_delay=1.0)
        check("IntegrityError raised rather than swallowed", False)
    except IntegrityError:
        elapsed = time.time() - t0
        check("IntegrityError raised rather than swallowed", True)
        check("raised without burning the retry budget", elapsed < 1.0)
    finally:
        db.rollback()


def test_lock_that_never_clears_still_raises():
    print("\n### a lock that never clears raises after the last attempt")
    db = new_session()
    fail_fast(db)
    lead = db.query(Lead).filter_by(title="inserted under lock").one()

    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("BEGIN EXCLUSIVE")
    try:
        lead.notes = "never lands"
        try:
            commit_with_retry(db, attempts=3, base_delay=0.1)
            check("OperationalError propagates after exhausting attempts", False)
        except OperationalError:
            check("OperationalError propagates after exhausting attempts", True)
    finally:
        conn.execute("ROLLBACK")
        conn.close()
        db.rollback()

    fresh = new_session()
    row = fresh.query(Lead).filter_by(title="inserted under lock").one()
    check("failed write left the row unchanged", row.notes == "updated under lock")


def main():
    init_database(DB_URL)
    test_pragmas_on_every_connection()
    test_reader_not_blocked_by_writer()
    test_insert_recovers_from_lock()
    test_update_recovers_with_values_intact()
    test_restore_emits_no_sql()
    test_non_lock_error_is_not_retried()
    test_lock_that_never_clears_still_raises()

    print()
    if FAILURES:
        print("FAILED (%d):" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        for s in _sessions:                       # open handles on Windows
            try:
                s.close()
                s.get_bind().dispose()
            except Exception:
                pass
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
