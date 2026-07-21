"""数据模型:对齐 design/schema.sql,并按通用信息搜索框架增加 need 维度。

可移植性决策(最优解):
- ARRAY/JSONB 统一用 JSON 类型(SQLite/PG 双兼容);
- 事件 embedding 存 JSON 数组,语义召回在应用层算余弦(当前量级足够),
  生产迁移 pgvector 时只需换 dedup.semantic_recall 的实现;
- schema.sql 保留为 PG 生产参考 DDL,代码以本文件为准。
"""
from datetime import datetime, date

from sqlalchemy import (
    JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def now() -> datetime:
    return datetime.utcnow()


# ============ 框架层:信息需求画像 ============

class NeedProfile(Base):
    __tablename__ = "need_profile"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)   # 如 sec_events
    name: Mapped[str] = mapped_column(String(128))
    config: Mapped[dict] = mapped_column(JSON)                      # 画像全文(need_profile yaml)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ============ M9 用户/审计/词表 ============

class AppUser(Base):
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16))  # admin/analyst/reviewer/editor/readonly
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(256))
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now)


class DictionaryRelease(Base):
    __tablename__ = "dictionary_release"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"))
    version: Mapped[str] = mapped_column(String(32))
    content: Mapped[dict] = mapped_column(JSON)
    released_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("need_id", "version"),)


# ============ M1/M10 源 ============

class Source(Base):
    __tablename__ = "source"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    identity_key: Mapped[str | None] = mapped_column(String(256), unique=True, nullable=True)
    discovery_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    homepage: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(8))                    # page / query
    adapter: Mapped[str] = mapped_column(String(64))
    adapter_config: Mapped[dict] = mapped_column(JSON, default=dict)
    credibility: Mapped[str] = mapped_column(String(4))             # S1..S4
    tier: Mapped[str] = mapped_column(String(2), default="B")       # A/B/C
    lifecycle: Mapped[str] = mapped_column(String(16), default="candidate")
    serves_needs: Mapped[list] = mapped_column(JSON, default=list)  # 源可服务多需求
    discovered_from: Mapped[str | None] = mapped_column(String(32), nullable=True)
    manual_assist: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    stat_docs_total: Mapped[int] = mapped_column(Integer, default=0)
    stat_firsthand: Mapped[int] = mapped_column(Integer, default=0)
    stat_events_linked: Mapped[int] = mapped_column(Integer, default=0)
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fail_streak: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SourceMetricDaily(Base):
    __tablename__ = "source_metric_daily"
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    new_docs: Mapped[int] = mapped_column(Integer, default=0)
    firsthand: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)


class SourceDiscoveryEvidence(Base):
    __tablename__ = "source_discovery_evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_key: Mapped[str] = mapped_column(String(256), index=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    kind_guess: Mapped[str | None] = mapped_column(String(16), nullable=True)  # website/wechat_mp/forum/other
    channel: Mapped[str] = mapped_column(String(32))  # event_search/citation/wechat_reference/directory/source_search/manual
    evidence_doc_id: Mapped[int | None] = mapped_column(ForeignKey("raw_document.id"), nullable=True)
    evidence_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    was_cluster_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    hit_count: Mapped[int] = mapped_column(Integer, default=1)


class SourceBlacklist(Base):
    __tablename__ = "source_blacklist"
    identity_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    reason: Mapped[str] = mapped_column(Text)
    by_user: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ============ M2 抓取 / M11 存档 ============

class CrawlRun(Base):
    __tablename__ = "crawl_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    keyword_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/ok/partial/failed
    urls_found: Mapped[int] = mapped_column(Integer, default=0)
    urls_new: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ArchiveManifest(Base):
    __tablename__ = "archive_manifest"
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(8))                  # L-A/L-B/L-C/L-D
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    final_url: Mapped[str] = mapped_column(Text)
    storage_path: Mapped[str] = mapped_column(Text)
    has_full_text: Mapped[bool] = mapped_column(Boolean, default=False)
    image_count: Mapped[int] = mapped_column(Integer, default=0)
    attachment_count: Mapped[int] = mapped_column(Integer, default=0)
    screenshot_pages: Mapped[int] = mapped_column(Integer, default=0)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verify_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class DocCluster(Base):
    __tablename__ = "doc_cluster"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    primary_doc_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, default=1)
    first_published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RawDocument(Base):
    __tablename__ = "raw_document"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    crawl_run_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_run.id"), nullable=True)
    url: Mapped[str] = mapped_column(Text)
    url_normalized: Mapped[str] = mapped_column(String(1024), unique=True)  # 10.1 URL 层去重
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(256), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 带符号 64bit SimHash;BigInteger 保证 PostgreSQL 不溢出(int4 存不下)
    simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("doc_cluster.id"), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("archive_manifest.snapshot_id"), nullable=True)
    screen_status: Mapped[str] = mapped_column(String(16), default="pending")
    screen_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    screen_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    seen_again: Mapped[int] = mapped_column(Integer, default=0)


# ============ M4/M5 事件(记录) ============

class Event(Base):
    __tablename__ = "event"
    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # SEC-YYYYMMDD-NNNN
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft/published/monitoring/closed
    occurred_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    disclosed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    industry_l1: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    industry_l2: Mapped[str | None] = mapped_column(String(64), nullable=True)
    province: Mapped[str | None] = mapped_column(String(32), nullable=True)
    city: Mapped[str | None] = mapped_column(String(64), nullable=True)
    org_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    org_uscc: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    org_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    org_size: Mapped[str | None] = mapped_column(String(8), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(8), nullable=True)
    attack_types: Mapped[list] = mapped_column(JSON, default=list)
    consequences: Mapped[list] = mapped_column(JSON, default=list)
    confidence_overall: Mapped[str | None] = mapped_column(String(16), nullable=True)
    completeness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    dict_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    first_published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EventSource(Base):
    __tablename__ = "event_source"
    event_id: Mapped[str] = mapped_column(ForeignKey("event.event_id"), primary_key=True)
    ref_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_id: Mapped[int | None] = mapped_column(ForeignKey("raw_document.id"), nullable=True)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("archive_manifest.snapshot_id"), nullable=True)
    credibility: Mapped[str] = mapped_column(String(4))
    supports_fields: Mapped[list] = mapped_column(JSON, default=list)


class EventChangeLog(Base):
    __tablename__ = "event_change_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("event.event_id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now)
    by_user: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    field: Mapped[str] = mapped_column(String(128))
    old_value: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ReviewTask(Base):
    __tablename__ = "review_task"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("event.event_id"), index=True)
    stage: Mapped[str] = mapped_column(String(16), default="extracted")
    needs_double: Mapped[bool] = mapped_column(Boolean, default=False)
    assignee: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    first_reviewer: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    second_reviewer: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    comments: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class FollowupTask(Base):
    __tablename__ = "followup_task"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("event.event_id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))                    # T30/T90/T180/T365/manual
    due_date: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="open")  # open/done/skipped
    search_pack: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    findings: Mapped[str | None] = mapped_column(Text, nullable=True)
    done_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ============ 搜索行为(B1-B8 / G1-G8) ============

class KeywordSet(Base):
    __tablename__ = "keyword_set"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"))
    version: Mapped[str] = mapped_column(String(32))
    content: Mapped[dict] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("need_id", "version"),)


class KeywordRun(Base):
    __tablename__ = "keyword_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"), index=True)
    keyword_set_id: Mapped[int | None] = mapped_column(ForeignKey("keyword_set.id"), nullable=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    behavior: Mapped[str] = mapped_column(String(4), default="B1")  # B1..B8
    watch_target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    query: Mapped[str] = mapped_column(Text)
    ran_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    pages_fetched: Mapped[int] = mapped_column(Integer, default=1)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)  # C2 禁止无声截断
    results: Mapped[int] = mapped_column(Integer, default=0)
    new_docs: Mapped[int] = mapped_column(Integer, default=0)
    new_source_candidates: Mapped[int] = mapped_column(Integer, default=0)
    result_snapshot: Mapped[list | None] = mapped_column(JSON, nullable=True)  # C10 可回放


class WatchTarget(Base):
    __tablename__ = "watch_target"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))                   # org/product/attacker_group/topic
    value: Mapped[str] = mapped_column(String(256))
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_event_id: Mapped[str | None] = mapped_column(ForeignKey("event.event_id"), nullable=True)
    tier: Mapped[str] = mapped_column(String(2), default="B")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("need_id", "kind", "value"),)


class SearchWatermark(Base):
    __tablename__ = "search_watermark"
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"), primary_key=True)
    query_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_ran_at: Mapped[datetime] = mapped_column(DateTime)


# ============ M10 对标 / M8 线索 ============

class BenchmarkBatch(Base):
    __tablename__ = "benchmark_batch"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"))
    name: Mapped[str] = mapped_column(String(128))
    period: Mapped[str] = mapped_column(String(8))                  # YYYY-MM
    source_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class BenchmarkItem(Base):
    __tablename__ = "benchmark_item"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("benchmark_batch.id"))
    summary: Mapped[str] = mapped_column(Text)
    matched_event_id: Mapped[str | None] = mapped_column(ForeignKey("event.event_id"), nullable=True)
    is_missed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    miss_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)


class Lead(Base):
    __tablename__ = "lead"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("event.event_id"))
    target_org: Mapped[str] = mapped_column(String(256))
    target_kind: Mapped[str] = mapped_column(String(16))            # victim/same_product/peer
    score: Mapped[float] = mapped_column(Float)
    window_stage: Mapped[str] = mapped_column(String(8))            # 应急期/整改期/预算期/已过窗
    products: Mapped[list] = mapped_column(JSON, default=list)
    talk_track: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="new")
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    feedback: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (UniqueConstraint("event_id", "target_org"),)


class AppSetting(Base):
    """运行时可配置项持久化(页面「设置」编辑,覆盖 .env 默认值)。"""
    __tablename__ = "app_setting"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class CrawlJob(Base):
    """一次采集任务的持久化状态与进度(后台异步执行,任何页面/刷新可查)。"""
    __tablename__ = "crawl_job"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/done/failed/canceled
    phase: Mapped[str] = mapped_column(String(32), default="准备")       # 当前阶段(人读)
    total_sources: Mapped[int] = mapped_column(Integer, default=0)
    done_sources: Mapped[int] = mapped_column(Integer, default=0)
    total_docs: Mapped[int] = mapped_column(Integer, default=0)          # 待处理文档总数
    done_docs: Mapped[int] = mapped_column(Integer, default=0)           # 已处理文档数
    new_docs: Mapped[int] = mapped_column(Integer, default=0)            # 新抓取入库文档
    kept_docs: Mapped[int] = mapped_column(Integer, default=0)           # 粗筛判为相关
    dropped_docs: Mapped[int] = mapped_column(Integer, default=0)        # 粗筛判为不相干
    new_events: Mapped[int] = mapped_column(Integer, default=0)          # 生成草稿事件
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    limit_sources: Mapped[int] = mapped_column(Integer, default=3)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CrawlLog(Base):
    """采集详细日志(故障排查用):每一步、每个源、每次失败都记。"""
    __tablename__ = "crawl_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("crawl_job.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now)
    level: Mapped[str] = mapped_column(String(8), default="info")  # info/warn/error
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message: Mapped[str] = mapped_column(Text)
