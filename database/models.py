"""
Database models for Class Action Scout
"""
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Float,
    Boolean, DateTime, ForeignKey, JSON,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

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

    # Status tracking
    status = Column(String(50), default="new")  # new, reviewed, pursuing, dismissed
    notes = Column(Text)
    reviewed_at = Column(DateTime)

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


# ── DB initialization ──────────────────────────────────

def init_database(database_url: str):
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    return engine


def get_session(database_url: str):
    engine = create_engine(database_url)
    return sessionmaker(bind=engine)()
