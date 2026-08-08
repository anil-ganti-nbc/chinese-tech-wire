"""SQLAlchemy models for Chinese Tech Wire V0.1."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    JSON,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    source_article_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title_original: Mapped[str] = mapped_column(Text, nullable=False)
    title_english: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    summary_original: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary_english: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    upstream_source: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    upstream_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rumor_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    rumor_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    novelty_score: Mapped[float] = mapped_column(Float, default=50.0)
    relevance_score: Mapped[float] = mapped_column(Float, default=50.0)
    priority_score: Mapped[float] = mapped_column(Float, default=50.0)
    duplicate_group_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("story_clusters.id"), nullable=True, index=True
    )
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("source", "source_article_id", name="uq_source_article"),
    )

    cluster: Mapped[Optional["StoryCluster"]] = relationship(
        "StoryCluster", back_populates="articles"
    )
    entities: Mapped[List["ArticleEntity"]] = relationship(
        "ArticleEntity", back_populates="article", cascade="all, delete-orphan"
    )


class StoryCluster(Base):
    __tablename__ = "story_clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    first_seen_source: Mapped[str] = mapped_column(String(32), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    representative_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # V0.3 chronology
    first_signal_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    first_signal_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_media_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    first_media_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_documentary_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    first_documentary_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    articles: Mapped[List[Article]] = relationship("Article", back_populates="cluster")


class EntityRecord(Base):
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)


class ArticleEntity(Base):
    __tablename__ = "article_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(Integer, ForeignKey("articles.id"), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, ForeignKey("entities.id"), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    article: Mapped[Article] = relationship("Article", back_populates="entities")
    entity: Mapped[EntityRecord] = relationship("EntityRecord")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(Integer, ForeignKey("articles.id"), nullable=False)
    cluster_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("story_clusters.id"), nullable=True)
    priority_score: Mapped[float] = mapped_column(Float, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    discord_message_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_high_priority: Mapped[bool] = mapped_column(Boolean, default=False)


class SourceRun(Base):
    """Per-attempt operational telemetry for a single source adapter run.

    Despite the "articles_*" naming (kept for migration compatibility — this
    table predates COMMUNITY/DOCUMENTARY layers), these fields are layer-generic:
    articles_found/articles_new mean "records found"/"records new" for any layer.
    """

    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    # NEWS | COMMUNITY | DOCUMENTARY. Nullable/defaulted for pre-V0.5.5.1 rows,
    # which are all honestly NEWS — no other layer ever wrote this table before.
    layer: Mapped[str] = mapped_column(String(16), default="NEWS", server_default="NEWS")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    articles_found: Mapped[int] = mapped_column(Integer, default=0)
    articles_new: Mapped[int] = mapped_column(Integer, default=0)
    parse_errors: Mapped[int] = mapped_column(Integer, default=0)
    request_errors: Mapped[int] = mapped_column(Integer, default=0)
    response_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # True when a zero/degraded result is known to stem from a caught fetch
    # failure (HTTP block, anti-bot interstitial, etc) rather than a
    # genuinely-empty listing. Set from BaseSource.fetch_error_log.
    soft_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class TranslationCache(Base):
    """Persistent cache for translated strings. Survives process restarts."""

    __tablename__ = "translation_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_language: Mapped[str] = mapped_column(String(16), nullable=False, default="zh")
    target_language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    translation: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# V0.3 Community Intelligence
# ---------------------------------------------------------------------------

class CommunityThread(Base):
    """Normalized forum/community thread — separate from news Article."""

    __tablename__ = "community_threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    region: Mapped[str] = mapped_column(String(8), default="CN")
    language_variant: Mapped[str] = mapped_column(String(16), default="zh-CN")

    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    title_original: Mapped[str] = mapped_column(Text, nullable=False)
    title_english: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    author_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    author_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    board: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    op_text_original: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    op_text_english: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    image_count: Mapped[int] = mapped_column(Integer, default=0)
    attachment_count: Mapped[int] = mapped_column(Integer, default=0)

    external_urls: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # list of {url, source_type}

    signal_type: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    signal_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    firsthand_score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    community_score: Mapped[float] = mapped_column(Float, default=40.0)
    velocity_score: Mapped[float] = mapped_column(Float, default=0.0)
    relevance_score: Mapped[float] = mapped_column(Float, default=50.0)
    novelty_score: Mapped[float] = mapped_column(Float, default=50.0)
    corroboration_score: Mapped[float] = mapped_column(Float, default=0.0)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0)

    story_cluster_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("story_clusters.id"), nullable=True, index=True
    )
    first_signal_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("platform", "thread_id", name="uq_platform_thread"),
    )


class CommunityPost(Base):
    """Selective reply ingestion — OP always stored; other replies by rule."""

    __tablename__ = "community_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_db_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("community_threads.id"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    post_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    author_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    text_original: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text_english: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    image_count: Mapped[int] = mapped_column(Integer, default=0)
    attachment_count: Mapped[int] = mapped_column(Integer, default=0)
    external_urls: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    is_op: Mapped[bool] = mapped_column(Boolean, default=False)
    is_selected_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("platform", "post_id", name="uq_platform_post"),
    )


class ThreadMetrics(Base):
    """Snapshots for velocity calculation."""

    __tablename__ = "thread_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_db_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("community_threads.id"), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    view_count: Mapped[int] = mapped_column(Integer, default=0)


class AuthorProfile(Base):
    """Public forum identity for future longitudinal analysis. No trust score yet."""

    __tablename__ = "author_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    author_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    post_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rank: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claim_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("platform", "author_id", name="uq_platform_author"),
    )


# Extend StoryCluster with first_signal / first_media chronology (nullable columns)
# Added via ALTER-safe pattern: only if not already present — SQLAlchemy create_all
# adds new columns only on fresh DB; for existing DBs we document migration note.



# ---------------------------------------------------------------------------
# V0.4 Documentary Intelligence
# ---------------------------------------------------------------------------

class DocumentaryRecord(Base):
    __tablename__ = "documentary_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    # RETAIL_LISTING | BENCHMARK_RECORD | REGULATORY_RECORD | CERTIFICATION_RECORD
    source: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    region: Mapped[str] = mapped_column(String(8), default="GLOBAL")
    language_variant: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    manufacturer: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    brand: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    product: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    model_number: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    record_status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    # ACTIVE | MISSING | REMOVED | REAPPEARED

    evidence_type: Mapped[str] = mapped_column(String(32), default="DOCUMENTARY")
    evidence_score: Mapped[float] = mapped_column(Float, default=50.0)
    novelty_score: Mapped[float] = mapped_column(Float, default=50.0)
    relevance_score: Mapped[float] = mapped_column(Float, default=50.0)
    priority_score: Mapped[float] = mapped_column(Float, default=50.0)
    source_quality: Mapped[float] = mapped_column(Float, default=0.7)
    corroboration_score: Mapped[float] = mapped_column(Float, default=0.0)

    story_cluster_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("story_clusters.id"), nullable=True, index=True
    )

    raw_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    raw_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    missing_streak: Mapped[int] = mapped_column(Integer, default=0)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("source", "source_record_id", name="uq_doc_source_record"),
    )


class DocumentarySnapshot(Base):
    __tablename__ = "documentary_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documentary_records.id"), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    structured_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)


class DocumentaryEvent(Base):
    __tablename__ = "documentary_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documentary_records.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # NEW_RECORD | PRICE_ADDED | PRICE_CHANGED | SPEC_ADDED | SPEC_CHANGED |
    # MODEL_REVEALED | IMAGE_ADDED | AVAILABILITY_CHANGED | BENCHMARK_APPEARED |
    # CERTIFICATION_APPEARED | RECORD_REMOVED | RECORD_REAPPEARED
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    before_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    after_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)



# ---------------------------------------------------------------------------
# V0.5 Newsroom Intelligence
# ---------------------------------------------------------------------------

class StoryLead(Base):
    __tablename__ = "story_leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("story_clusters.id"), nullable=True, index=True
    )

    lead_status: Mapped[str] = mapped_column(String(32), default="NEW", index=True)
    # NEW | WATCHING | ACTIONABLE | ESCALATED | STALE | RESOLVED | DISMISSED
    lead_type: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)

    headline_hint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    primary_entities: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_signal_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    news_count: Mapped[int] = mapped_column(Integer, default=0)
    community_count: Mapped[int] = mapped_column(Integer, default=0)
    documentary_count: Mapped[int] = mapped_column(Integer, default=0)
    source_count: Mapped[int] = mapped_column(Integer, default=0)

    novelty_score: Mapped[float] = mapped_column(Float, default=50.0)
    evidence_score: Mapped[float] = mapped_column(Float, default=50.0)
    momentum_score: Mapped[float] = mapped_column(Float, default=0.0)
    source_diversity_score: Mapped[float] = mapped_column(Float, default=0.0)
    exclusivity_score: Mapped[float] = mapped_column(Float, default=50.0)
    confidence_score: Mapped[float] = mapped_column(Float, default=50.0)
    media_saturation_score: Mapped[float] = mapped_column(Float, default=0.0)
    relevance_score: Mapped[float] = mapped_column(Float, default=50.0)
    editorial_value_score: Mapped[float] = mapped_column(Float, default=50.0)
    priority_score: Mapped[float] = mapped_column(Float, default=50.0)

    why_now: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    why_it_matters: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    uncertainty_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    first_signal_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    first_documentary_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    first_media_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    lead_time_minutes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_notified_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_notified_priority: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    score_breakdown: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    raw_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("cluster_id", name="uq_lead_cluster"),
    )


class LeadEvent(Base):
    __tablename__ = "lead_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("story_leads.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)


class LeadFeedback(Base):
    __tablename__ = "lead_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("story_leads.id"), nullable=False, index=True
    )
    feedback: Mapped[str] = mapped_column(String(32), nullable=False)
    # USEFUL | NOT_USEFUL | WRITTEN | DUPLICATE | FALSE_POSITIVE
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# V0.5.6 Editorial Validation
# ---------------------------------------------------------------------------

class LeadOutcome(Base):
    """What ultimately happened editorially to a StoryLead.

    Distinct from LeadFeedback (the existing lightweight USEFUL/WRITTEN/
    NOT_USEFUL/DUPLICATE/FALSE_POSITIVE quick-tap buttons, left untouched)
    and distinct from lead_status (current lifecycle state). Append-only
    history — a lead can accumulate multiple outcome records over time
    (e.g. USEFUL, then later WRITTEN with an article URL). The most recent
    row by recorded_at is the "current" outcome; is_final flags outcomes
    that are not expected to change further.
    """

    __tablename__ = "lead_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("story_leads.id"), nullable=False, index=True
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # UNKNOWN | USEFUL | WRITTEN | CONFIRMED | OFFICIALLY_ANNOUNCED | FALSE |
    # DUPLICATE | IGNORED | STALLED | EXPIRED | MISSED_OPPORTUNITY
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    recorded_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    related_article_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    related_article_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    external_confirmation_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # MANUAL | GUI | CLI (informational — who/what recorded this row)
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)


class MissedStory(Base):
    """Operator-recorded editorial miss — a real story CTW should plausibly
    have surfaced but didn't (or surfaced too late/too quietly). Manual
    entry only in V0.5.6; no automatic English-media crawling yet.
    """

    __tablename__ = "missed_stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    reported_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    article_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    related_entities: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expected_scope: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    # UNKNOWN | SOURCE_NOT_MONITORED | SOURCE_BLOCKED | SOURCE_FAILED |
    # PARSER_MISSED | ENTITY_MISSED | CLUSTER_MISSED | LOW_SCORE |
    # LIFECYCLE_FILTER | ALERT_FILTER | DUPLICATE_ERROR | TOO_LATE | OTHER
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    matched_lead_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("story_leads.id"), nullable=True, index=True
    )
    matched_cluster_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("story_clusters.id"), nullable=True, index=True
    )
    metadata_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)



# ---------------------------------------------------------------------------
# V0.5.2 Scheduled ingestion history
# ---------------------------------------------------------------------------

class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="MANUAL")
    # MANUAL | SCHEDULED
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    # RUNNING | SUCCESS | PARTIAL | FAILED
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    articles_new: Mapped[int] = mapped_column(Integer, default=0)
    community_threads_new: Mapped[int] = mapped_column(Integer, default=0)
    documentary_records_new: Mapped[int] = mapped_column(Integer, default=0)
    documentary_events_new: Mapped[int] = mapped_column(Integer, default=0)
    leads_created: Mapped[int] = mapped_column(Integer, default=0)
    alerts_sent: Mapped[int] = mapped_column(Integer, default=0)
    alerts_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    alerts_eligible: Mapped[int] = mapped_column(Integer, default=0)
    alerts_attempted: Mapped[int] = mapped_column(Integer, default=0)
    alerts_failed: Mapped[int] = mapped_column(Integer, default=0)
    alerts_suppressed: Mapped[int] = mapped_column(Integer, default=0)
    alert_decision_summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# V0.5.3 StoryLead notification ledger
# ---------------------------------------------------------------------------

class LeadNotification(Base):
    """Persistent StoryLead alert attempts and outcomes. Never stores webhook URLs."""

    __tablename__ = "lead_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("story_leads.id"), nullable=True, index=True
    )
    cluster_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # SENT | FAILED | DRY_RUN | SUPPRESSED | POLICY_ACTIVATED
    reason_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    alert_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # FIRST_ELIGIBLE | BECAME_ACTIONABLE | MATERIAL_SCORE_INCREASE | ...
    lead_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    priority_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    evidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    relevance_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    webhook_http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ingestion_run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    payload_version: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
