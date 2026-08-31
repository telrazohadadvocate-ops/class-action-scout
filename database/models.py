"""
Database models for Class Action Scout
"""
import logging
import time
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine, event, inspect as sa_inspect, Column, Integer, String, Text,
    Float, Boolean, DateTime, ForeignKey, JSON, text,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.orm.attributes import flag_modified, set_committed_value

logger = logging.getLogger(__name__)

Base = declarative_base()


class Lead(Base):
    """A potential class action opportunity"""
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(Text, nullable=False)
    source_name = Column(String(100))      # classaction_org, themarker, etc.
    source_url = Column(Text)
    source_type = Column(String(20))       # international / local
    company = Column(String(255))
    sector = Column(String(100))
    country_of_origin = Column(String(100))

    # Raw scraped content
    raw_content = Column(Text)
    scraped_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # AI classification (stage 1)
    relevance_score = Column(Float)        # 1-10 Israel relevance
    relevance_reasoning = Column(Text)
    operates_in_israel = Column(Boolean)
    israeli_law_basis = Column(Text)       # e.g. "חוק הגנת הצרכן סעיף 2"
    estimated_class_size = Column(String(50))

    # Deep analysis (stage 2, only if relevance >= threshold)
    legal_analysis = Column(Text)
    strength_score = Column(Float)         # 1-10 case strength
    priority = Column(String(20))          # high / medium / low
    recommended_action = Column(Text)
    comparable_cases = Column(Text)        # known IL/intl precedents

    # PACER enrichment (stage 3.5)
    pacer_case_number = Column(String(100))
    pacer_dismissal_type = Column(String(50))
    pacer_docket_count = Column(Integer)
    pacer_url = Column(Text)

    # פנקס check (stage 4)
    pinkas_checked = Column(Boolean, default=False)
    pinkas_exists = Column(Boolean)        # True = similar case already filed
    pinkas_details = Column(Text)

    # Firm-specific
    matches_expertise = Column(Boolean)
    expertise_area = Column(String(255))
    is_duplicate_of_known = Column(Boolean, default=False)
    known_case_ref = Column(String(255))

    # Value estimation (rough triage estimates — NOT legal/financial advice)
    value_low = Column(Float)               # low end of estimated range, NIS
    value_high = Column(Float)              # high end of estimated range, NIS
    est_class_size = Column(Integer)        # estimated midpoint class members
    est_damage_per_member = Column(Float)   # estimated avg damage per member, NIS
    priority_score = Column(Float)          # 1-10 composite (value+cert+expertise)
    value_confidence = Column(String(10))   # "high" / "medium" / "low"
    value_reasoning = Column(Text)          # brief explanation of the estimate
    israel_applicable = Column(Boolean)     # True = genuine Israeli nexus confirmed

    # Already-filed detection — an Israeli class action ALREADY filed on this
    # matter is a missed opportunity, not a lead. Downranked, never deleted.
    already_filed_il = Column(Boolean)      # True = Israeli class action already filed
    already_filed_details = Column(Text)    # short evidence snippet from the source

    # Composite score rationale — JSON {summary,value,strength,israel,change}
    score_reasoning = Column(Text)

    # Suspected-duplicate review — borderline similarity [REVIEW, AUTO) is flagged
    # for a human call rather than auto-merged (same company / different lawsuit).
    suspected_dup_of = Column(Integer)      # id of the lead this may duplicate
    suspected_dup_score = Column(Float)     # cosine similarity to that lead
    dup_review = Column(String(20))         # None / "pending" / "merged" / "separate"

    # Semantic deduplication
    embedding = Column(Text)            # JSON-serialized list[float] from Voyage AI
    dedup_group_id = Column(String(50)) # str(canonical_lead.id) for the cluster

    # Status tracking
    status = Column(String(50), default="new")  # new, reviewed, pursuing, dismissed
    notes = Column(Text)
    reviewed_at = Column(DateTime)

    # Alerting — set once, when this lead has been included in an alert email.
    # NULL means "never alerted". This is the only thing that stops a lead being
    # alerted twice, so the send condition is "over threshold AND alerted_at IS
    # NULL" rather than a time window over scraped_at.
    alerted_at = Column(DateTime)

    # Relationships
    raw_sources = relationship("RawSource", back_populates="lead")


class RawSource(Base):
    """Raw scraped items before classification"""
    __tablename__ = "raw_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_name = Column(String(100))
    url = Column(Text, unique=True)
    title = Column(Text)
    content = Column(Text)
    date_published = Column(DateTime)
    scraped_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True)

    lead = relationship("Lead", back_populates="raw_sources")


class ScrapeLog(Base):
    """Log of scraping runs"""
    __tablename__ = "scrape_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_name = Column(String(100))
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime)
    items_found = Column(Integer, default=0)
    items_new = Column(Integer, default=0)
    errors = Column(Text)
    success = Column(Boolean, default=True)


class PinkasCache(Base):
    """Cache of פנקס התובענות הייצוגיות search results"""
    __tablename__ = "pinkas_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    search_query = Column(String(255))
    results_json = Column(JSON)
    searched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AlertLog(Base):
    """Record of high-priority alert emails sent"""
    __tablename__ = "alert_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sent_at = Column(DateTime, default=lambda: datetime.utcnow())
    lead_count = Column(Integer, default=0)
    status = Column(String(50))  # "sent", "error"


# ── Migrations ─────────────────────────────────────────

def _run_migrations(engine) -> None:
    """
    ALTER TABLE migrations for columns added after initial deploy.
    Each statement is tried independently; OperationalError means the
    column already exists, which is silently ignored.
    """
    new_cols = [
        ("value_low",            "REAL"),
        ("value_high",           "REAL"),
        ("est_class_size",       "INTEGER"),
        ("est_damage_per_member","REAL"),
        ("priority_score",       "REAL"),
        ("value_confidence",     "TEXT"),
        ("value_reasoning",      "TEXT"),
        ("israel_applicable",    "INTEGER"),
        ("already_filed_il",     "INTEGER"),
        ("already_filed_details","TEXT"),
        ("score_reasoning",      "TEXT"),
        ("suspected_dup_of",     "INTEGER"),
        ("suspected_dup_score",  "REAL"),
        ("dup_review",           "TEXT"),
        ("embedding",            "TEXT"),
        ("dedup_group_id",       "TEXT"),
        ("alerted_at",           "DATETIME"),
    ]
    with engine.connect() as conn:
        for col, dtype in new_cols:
            try:
                conn.execute(text(f"ALTER TABLE leads ADD COLUMN {col} {dtype}"))
                conn.commit()
            except Exception:
                pass  # column already exists


# ── SQLite concurrency ─────────────────────────────────
#
# SQLite's rollback-journal default takes an exclusive lock for the whole of
# every write and blocks readers while it is held, so a long backfill running
# alongside the web app produces "database is locked". Two settings fix that,
# and both have to be applied to EVERY connection:
#
#   journal_mode=WAL   readers and the single writer no longer block each
#                      other. Persisted in the DB file, so it is set once and
#                      is a cheap no-op on later connections.
#   busy_timeout       on writer-vs-writer contention, wait for the lock
#                      instead of failing instantly. The default is 5s; the
#                      web app's writes are short, so 30s is ample headroom.

BUSY_TIMEOUT_MS = 30_000


def _configure_sqlite(engine) -> None:
    """Apply WAL + busy_timeout to every new connection on a SQLite engine."""
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        try:
            # WAL is unavailable for :memory: and on some network filesystems.
            # It is an optimisation, not a correctness requirement, so a
            # failure here is logged and the connection is still usable.
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
            except Exception as exc:  # pragma: no cover - filesystem dependent
                logger.warning("Could not enable SQLite WAL mode: %s", exc)
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        finally:
            cursor.close()


def _make_engine(database_url: str):
    # connect_args timeout covers the window before the busy_timeout PRAGMA
    # above has run on a fresh connection; pysqlite's own default is 5s.
    kwargs = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": BUSY_TIMEOUT_MS / 1000}
    engine = create_engine(database_url, **kwargs)
    _configure_sqlite(engine)
    return engine


def _is_locked_error(exc: Exception) -> bool:
    msg = str(getattr(exc, "orig", exc)).lower()
    return "database is locked" in msg or "database is busy" in msg


def _snapshot_pending(session):
    """
    Capture the loaded column values of everything the session is about to
    write, so they can be re-applied if the commit has to be retried.

    Must be called BEFORE the commit: a failed flush rolls the transaction
    back and expires every instance, leaving nothing to read afterwards.
    Only values already in the instance dict are copied — touching an unloaded
    attribute would emit SQL, which a broken transaction cannot serve.
    """
    snapshot = []
    for obj in list(session.new) + list(session.dirty):
        state = sa_inspect(obj)
        columns = state.mapper.columns.keys()
        values = {k: v for k, v in state.dict.items() if k in columns}
        snapshot.append((obj, values, obj in session.new))
    return snapshot


def _restore_pending(session, snapshot) -> None:
    """
    Re-mark the snapshotted objects as pending after a rolled-back attempt,
    without emitting any SQL.

    The rollback expired every persistent instance, and a plain setattr on an
    expired attribute fires a lazy load. Two guards, both load-bearing:
    no_autoflush stops that load flushing the half-restored state straight
    back into the lock being waited on, and seeding the values as
    already-loaded (set_committed_value) then flagging them modified avoids
    the load altogether. Rolled-back INSERTs are detached rather than expired,
    so they only need re-adding.
    """
    with session.no_autoflush:
        for obj, values, is_new in snapshot:
            if is_new:
                for key, value in values.items():
                    setattr(obj, key, value)
                session.add(obj)
                continue
            for key, value in values.items():
                set_committed_value(obj, key, value)
            for key in values:
                flag_modified(obj, key)


def commit_with_retry(session, attempts: int = 5, base_delay: float = 1.0):
    """
    Commit, retrying if SQLite reports the database as locked.

    busy_timeout already makes SQLite *wait* for a contended lock, so getting
    here means that wait itself expired — rare, but otherwise fatal to an
    hours-long backfill. A failed flush rolls the transaction back and expires
    the instances, discarding the values that were being written, so the
    pending column values are snapshotted up front and re-applied to the same
    objects before each retry. Backoff is linear: 1s, 2s, 3s...

    Any non-lock error, and a lock error on the final attempt, is re-raised
    unchanged — this retries contention, it does not swallow failures.
    """
    snapshot = _snapshot_pending(session)
    for attempt in range(1, attempts + 1):
        try:
            session.commit()
            return
        except OperationalError as exc:
            if not _is_locked_error(exc) or attempt == attempts:
                raise
            session.rollback()  # clear the failed transaction before retrying
            _restore_pending(session, snapshot)
            delay = base_delay * attempt
            logger.warning(
                "Database locked on commit (attempt %d/%d); retrying in %.1fs",
                attempt, attempts, delay,
            )
            time.sleep(delay)


# ── DB initialization ──────────────────────────────────

def init_database(database_url: str):
    engine = _make_engine(database_url)
    Base.metadata.create_all(engine)
    _run_migrations(engine)
    return engine


def get_session(database_url: str):
    engine = _make_engine(database_url)
    return sessionmaker(bind=engine)()
