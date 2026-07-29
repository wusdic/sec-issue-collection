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
    # identity_key = 采集目标唯一键(栏目粒度:页面→归一化URL、站内检索→site:域名、公众号→mp:账号);
    # site_key = 站点/发布主体身份(注册域/公众号,不唯一)——同站不同栏目共享 site_key、各有 identity_key。
    identity_key: Mapped[str | None] = mapped_column(String(400), unique=True, nullable=True)
    site_key: Mapped[str | None] = mapped_column(String(256), index=True, nullable=True)
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
    # 是哪条找源词把它捞出来的。候选后来真进了源库时,这条词才拿得到最强的正反馈,
    # 关键词进化才有"产出"这个分子可算
    found_by_query: Mapped[str | None] = mapped_column(String(200), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    hit_count: Mapped[int] = mapped_column(Integer, default=1)


class SourceProbe(Base):
    """候选源 LLM 相关度初评结果(抓其首页抽样标题让模型打 0-1 分)。

    候选评分公式里 LLM 相关度权重最高之一,此前从没有人算过、恒为 0,等于候选排序
    只看"被提到几次"不看"提的是不是这行的内容"。这张表把初评结果落地并按 TTL 复用。
    """
    __tablename__ = "source_probe"
    identity_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    relevance: Mapped[float] = mapped_column(Float, default=0.0)      # 0-1
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_titles: Mapped[list] = mapped_column(JSON, default=list)
    # 站点/号的名字(抓首页时顺手取的 <title>)。候选展示名要用"渠道名",
    # 而不是搜索结果的文章标题——否则候选池里会出现"什么是网警?-安康市公安局"这种名字
    site_title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    probed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)           # False=抓不到/评不了


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
    urls_skipped: Mapped[int] = mapped_column(Integer, default=0)  # 已采过、本次自动跳过(增量)
    urls_failed: Mapped[int] = mapped_column(Integer, default=0)   # 抓取失败
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
    # 记录类型:单一事件 / 通报情报(监管通报汇总·威胁情报·风险提示·态势统计,无单一受害方但有参考价值)
    record_type: Mapped[str] = mapped_column(String(16), default="单一事件", index=True)
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


class ActionLog(Base):
    """动作台账:系统(或人)执行过的每一个有后果的动作,按模块分类、按影响分级。

    自动化程度越高,越需要"系统到底动了什么"一目了然。高级别动作(碰发布红线、影响面大、
    不易逆转的)在相应模块顶部优先提示并要求确认,低级别的只记账不打扰。
    """
    __tablename__ = "action_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    module: Mapped[str] = mapped_column(String(16), index=True)   # sources/crawl/events/review/config
    action: Mapped[str] = mapped_column(String(48), index=True)   # 动作键,见 services/actions.CATALOG
    level: Mapped[int] = mapped_column(Integer, default=1, index=True)  # 1一般 2关注 3重要 4紧急
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    target: Mapped[str | None] = mapped_column(String(256), nullable=True)
    count: Mapped[int] = mapped_column(Integer, default=1)        # 影响面(条数),用于升级判定
    actor: Mapped[str] = mapped_column(String(32), default="auto")  # auto / user:<id>
    reversible: Mapped[str | None] = mapped_column(Text, nullable=True)  # 怎么撤销(给人看)
    at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    ack_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ack_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)


class AutoOpsRun(Base):
    """自动运维每一步的执行记录(源库自维护:查重/定位栏目/体检/找源/自动定级)。

    目标是把源库维护从"人工按按钮"变成"系统按周期自己做";这张表让人能事后核对
    系统究竟做了什么、做对没有,而不是黑箱自动化。
    """
    __tablename__ = "auto_ops_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"), index=True)
    task: Mapped[str] = mapped_column(String(32), index=True)   # dedup/locate/health/prospect/grade
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/done/failed/skipped
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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


class RunTrace(Base):
    """端到端诊断留痕:LLM 调用(提示词+原始返回)、粗筛/抽取/去重/建草稿每步的输入输出。

    用于把一次采集"到底发生了什么"完整记录下来供离线分析(可整包下载)。detail 存结构化
    明细(JSON),ref 关联到具体文档 URL / 事件号,便于跨步骤对齐同一篇的处理链路。
    """
    __tablename__ = "run_trace"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_job.id"), index=True, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # llm/screen/extract/dedup/draft/error/note
    ref: Mapped[str | None] = mapped_column(String(400), nullable=True)   # 关联文档URL/事件号
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)      # 人读一行摘要
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)      # 结构化明细


class DailyDigest(Base):
    """每日简报:某需求某天的产出汇总(新增事件/线索/行业热点/源健康),可页面查看与下载。"""
    __tablename__ = "daily_digest"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    content: Mapped[dict] = mapped_column(JSON, default=dict)   # 结构化简报
    markdown: Mapped[str | None] = mapped_column(Text, nullable=True)  # 渲染好的 md 文本
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)    # 是否已推送
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("need_id", "day", name="uq_digest_need_day"),)


class SearchQueryStat(Base):
    """找源检索词的表现台账 —— 关键词进化机制的底座。

    此前找源词是一份静态清单:同一批词每周重复跑,从没人知道哪条词真的带回过渠道。
    「零售 数据泄露」这种加了限定反而把召回压死的词,和「数据泄露」这种有效词,
    在系统里长得一模一样。这张表按词记账,让词表能按实际产出自我淘汰和自我扩张。

    关键在于 anchor/modifier 的拆分:2 词组合里 anchor 是主锚点(单独也会跑,提供基线),
    modifier 是限定词。有了基线才能算出"加上这个限定词到底是帮忙还是帮倒忙"。
    """
    __tablename__ = "search_query_stat"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"), index=True)
    query: Mapped[str] = mapped_column(String(200), index=True)
    terms: Mapped[list] = mapped_column(JSON, default=list)
    anchor: Mapped[str | None] = mapped_column(String(64), nullable=True)     # 主锚点词
    modifier: Mapped[str | None] = mapped_column(String(64), nullable=True)   # 限定词(单词查询为空)
    origin: Mapped[str] = mapped_column(String(16), default="combo")
    # base/combo/coverage/anchor/drop/swap/harvest/llm —— 这条词是怎么来的
    parent: Mapped[str | None] = mapped_column(String(200), nullable=True)    # 由哪条词变异而来
    runs: Mapped[int] = mapped_column(Integer, default=0)
    results: Mapped[int] = mapped_column(Integer, default=0)      # 原始结果条数(会被页脚链之类灌水)
    usable: Mapped[int] = mapped_column(Integer, default=0)       # 去掉页脚/大平台/已有源后仍有效的
    new_channels: Mapped[int] = mapped_column(Integer, default=0)  # 带回的新候选渠道
    admitted: Mapped[int] = mapped_column(Integer, default=0)      # 其中最终进了源库的
    barren_streak: Mapped[int] = mapped_column(Integer, default=0)  # 连续多少轮没带回新渠道
    state: Mapped[str] = mapped_column(String(12), default="active", index=True)
    # active=正常轮换 / resting=暂时歇着(低频复测,不是判死) / retired=确认无效
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("need_id", "query", name="uq_qstat_need_query"),)


class TermStat(Base):
    """词一级的表现:同一个词在多条组合里的平均增益。

    单看某条组合的好坏容易被偶然性带偏;把同一个限定词在所有组合里的增益汇总起来,
    才判得出"零售"这类词是不是普遍在拖后腿——是就把它整个从组合池里降级。
    """
    __tablename__ = "term_stat"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    need_id: Mapped[str] = mapped_column(ForeignKey("need_profile.id"), index=True)
    term: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(12), default="modifier")   # anchor/modifier
    samples: Mapped[int] = mapped_column(Integer, default=0)   # 参与统计的组合条数
    lift: Mapped[float] = mapped_column(Float, default=1.0)    # 组合产出 / 锚点单独产出 的中位数
    solo_value: Mapped[float] = mapped_column(Float, default=0.0)  # 单独跑时的每轮产出
    state: Mapped[str] = mapped_column(String(12), default="active", index=True)
    # active / weak=不再参与组合(仍可单独跑) / retired
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("need_id", "term", "role", name="uq_termstat"),)
