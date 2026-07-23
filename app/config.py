"""全局配置:环境变量驱动,含 LLM/存档/数据库等。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path):
    """轻量 .env 加载:CLI 与 API 都自动读取项目根目录 .env;已存在的环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


class Settings:
    # 数据库:默认 SQLite(开发/测试),生产用 PostgreSQL
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR}/data/app.db")

    # LLM:OpenAI 兼容接口,provider=mock 时离线可用
    llm_provider: str = os.getenv("LLM_PROVIDER", "mock")  # mock | openai_compat
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "")
    llm_screen_model: str = os.getenv("LLM_SCREEN_MODEL", "")  # 粗筛用小模型,空则同 llm_model
    # Embedding 独立配置(很多聊天模型不支持向量接口,需分开);留空则回退聊天模型/接口
    llm_embed_model: str = os.getenv("LLM_EMBED_MODEL", "")
    llm_embed_base_url: str = os.getenv("LLM_EMBED_BASE_URL", "")
    llm_embed_api_key: str = os.getenv("LLM_EMBED_API_KEY", "")

    # 原文存档
    archive_root: str = os.getenv("ARCHIVE_ROOT", str(BASE_DIR / "data" / "archive"))
    archive_max_assets: int = int(os.getenv("ARCHIVE_MAX_ASSETS", "50"))
    archive_asset_byte_cap: int = int(os.getenv("ARCHIVE_ASSET_BYTE_CAP", str(20 * 1024 * 1024)))
    playwright_enabled: bool = os.getenv("PLAYWRIGHT_ENABLED", "0") == "1"

    # 抓取
    fetch_timeout: float = float(os.getenv("FETCH_TIMEOUT", "20"))
    fetch_user_agent: str = os.getenv(
        "FETCH_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    )
    crawl_delay_seconds: float = float(os.getenv("CRAWL_DELAY_SECONDS", "2"))

    # 鉴权
    jwt_secret: str = os.getenv("JWT_SECRET", "dev-secret-change-me")
    jwt_expire_hours: int = int(os.getenv("JWT_EXPIRE_HOURS", "12"))

    # 去重阈值
    simhash_hamming_max: int = int(os.getenv("SIMHASH_HAMMING_MAX", "3"))
    semantic_recall_threshold: float = float(os.getenv("SEMANTIC_RECALL_THRESHOLD", "0.88"))
    fingerprint_window_days: int = int(os.getenv("FINGERPRINT_WINDOW_DAYS", "14"))

    # 粗筛(过滤不相干内容):入选与人工待定阈值。默认偏宽以免广泛搜集时漏掉相关内容,
    # 宁可多进人工待定也不直接丢弃;要更严可在设置页调高。
    screen_keep_threshold: float = float(os.getenv("SCREEN_KEEP_THRESHOLD", "0.5"))
    screen_manual_threshold: float = float(os.getenv("SCREEN_MANUAL_THRESHOLD", "0.3"))

    # 增量翻页早停:列表/公众号按时间倒序,连续遇到 N 条已采过即判定"新内容抓全",停止翻页
    crawl_stop_consecutive_seen: int = int(os.getenv("CRAWL_STOP_CONSECUTIVE_SEEN", "15"))

    # 时效窗口:只采集发布时间在近 N 天内的内容(超出的视为历史,不入库);默认 1825=近5年
    collect_recency_days: int = int(os.getenv("COLLECT_RECENCY_DAYS", "1825"))

    # 搜索型源单源关键词上限(防 400 词硬打慢站空跑几十分钟);页面型不受此限
    search_source_query_cap: int = int(os.getenv("SEARCH_SOURCE_QUERY_CAP", "30"))
    # 单个源一次采集的时长上限(秒),超时停止该源剩余查询/翻页,避免拖垮整批
    source_time_budget_seconds: int = int(os.getenv("SOURCE_TIME_BUDGET_SECONDS", "180"))
    # 根域页面型源自动发现相关栏目:每站最多注册/抓取的栏目数
    auto_column_max: int = int(os.getenv("AUTO_COLUMN_MAX", "8"))
    # 栏目验证:候选栏目页需≥这么多篇文章且一致性达标才确认为有效栏目并入库
    column_min_articles: int = int(os.getenv("COLUMN_MIN_ARTICLES", "5"))
    column_consistency_min: float = float(os.getenv("COLUMN_CONSISTENCY_MIN", "0.5"))
    # 栏目发现结果记录后多久重算一次(天),期间直接复用不重复识别;应对栏目动态变化
    auto_column_refresh_days: int = int(os.getenv("AUTO_COLUMN_REFRESH_DAYS", "7"))

    # 同稿去重:回溯比对天数;缺标题时判"正文长度相近"的比值下限
    dedup_lookback_days: int = int(os.getenv("DEDUP_LOOKBACK_DAYS", "30"))
    dedup_len_ratio_min: float = float(os.getenv("DEDUP_LEN_RATIO_MIN", "0.6"))
    # 列表/RSS 单页最多取多少条
    list_max_items: int = int(os.getenv("LIST_MAX_ITEMS", "80"))
    rss_max_items: int = int(os.getenv("RSS_MAX_ITEMS", "50"))

    # 源自动发现:搜索/采集中出现的新域名累积证据评分≥此值即自动建 trial 源(自动入库,
    # 仍 S4 待人工定级)。越低越激进(新源多但杂),越高越保守。留空则用 discovery.yaml 的值。
    discovery_auto_trial_threshold: float = float(os.getenv("DISCOVERY_AUTO_TRIAL_THRESHOLD", "4.0"))

    # 源健康:连续失败(采集异常/试抓抓不到)达到此次数即自动标记停用(不再采集)。默认 3
    source_auto_retire_fail_streak: int = int(os.getenv("SOURCE_AUTO_RETIRE_FAIL_STREAK", "3"))

    # 浏览器渲染内存保护:同一浏览器实例连续渲染这么多页后回收重启,防长跑内存膨胀。0=不回收
    render_recycle_after: int = int(os.getenv("RENDER_RECYCLE_AFTER", "300"))

    # 每日自动采集:进程内轻量调度(无需 Celery/Redis),到点自动跑一轮采集并出日报
    daily_auto_enabled: bool = os.getenv("DAILY_AUTO_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    daily_auto_hour: int = int(os.getenv("DAILY_AUTO_HOUR", "1"))          # 每天几点(UTC)跑,默认 01:00 UTC≈北京9点
    daily_auto_limit_sources: int = int(os.getenv("DAILY_AUTO_LIMIT_SOURCES", "999"))  # 每日全量跑
    daily_need_id: str = os.getenv("DAILY_NEED_ID", "sec_events")

    # 日报邮件推送(可选):未配置 smtp_host 则不发,仅页面查看/下载
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "465"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "")
    digest_email_to: str = os.getenv("DIGEST_EMAIL_TO", "")               # 逗号分隔收件人

    # Celery
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # 配置文件路径
    config_dir: Path = BASE_DIR / "config"
    schema_dir: Path = BASE_DIR / "schema"


settings = Settings()
