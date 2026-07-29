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
    # 输出上限:抽取记录较大,推理模型(如 MiniMax-M3)还会先输出思维链,需足够 token 否则 JSON 被截断
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "8192"))
    # 单次 LLM 请求超时(秒)。推理模型偶尔很慢,超时即放弃该篇(不重试,重试也会超时),避免卡住
    llm_timeout: float = float(os.getenv("LLM_TIMEOUT", "90"))
    # 抽取提示词里 schema/词表 的最大字符数。schema 必须完整(含 $defs 的金额三态等结构定义),
    # 截断会让模型看不到字段结构 → 抽取结构崩坏、校验全失败,故默认给足。
    prompt_schema_chars: int = int(os.getenv("PROMPT_SCHEMA_CHARS", "40000"))
    prompt_dict_chars: int = int(os.getenv("PROMPT_DICT_CHARS", "8000"))

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
    # 并发度:多源同时抓取、多文档同时抽取(LLM 调用是网络等待,并发可大幅提速)。1=串行
    crawl_concurrency: int = int(os.getenv("CRAWL_CONCURRENCY", "5"))
    process_concurrency: int = int(os.getenv("PROCESS_CONCURRENCY", "6"))
    # 开启浏览器渲染时的抓取并发上限:同步 Playwright 要求每线程独立浏览器,并发几个源就有
    # 几个 Chromium(每个约数百 MB),故单独设更小的上限保护内存
    render_max_concurrency: int = int(os.getenv("RENDER_MAX_CONCURRENCY", "2"))
    # SQLite 写锁等待(毫秒)。并行采集下多 worker 争写锁,过小会直接 "database is locked"
    sqlite_busy_timeout_ms: int = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "30000"))
    # 根域页面型源自动发现相关栏目:每站最多注册/抓取的栏目数
    auto_column_max: int = int(os.getenv("AUTO_COLUMN_MAX", "8"))
    # 栏目验证:候选栏目页需≥这么多篇文章且一致性达标才确认为有效栏目并入库。
    # 默认 3(不是 5):低频栏目(如一年发几条的执法通报)整页也就三五条,卡太严会把
    # 最该采的权威栏目挡在门外,宁可多收几个再靠内容相关度筛。
    column_min_articles: int = int(os.getenv("COLUMN_MIN_ARTICLES", "3"))
    column_consistency_min: float = float(os.getenv("COLUMN_CONSISTENCY_MIN", "0.5"))
    # 栏目内容相关度下限:栏目里文章标题命中安全相关词的比例。结构一致只能说明"是个栏目",
    # 还必须内容相关才算"能精准定位到相关内容的栏目",否则会把"要闻/领导活动"当栏目抓进来。
    column_relevance_min: float = float(os.getenv("COLUMN_RELEVANCE_MIN", "0.3"))
    # 判定相关度所需的最少标题样本数;不足(如全是图片链接无文字)则不卡相关度,只看结构
    column_relevance_min_titles: int = int(os.getenv("COLUMN_RELEVANCE_MIN_TITLES", "3"))
    # 根域源识别不到有效栏目时怎么办:
    # search=转"站内检索"(按 site:域名+关键词精准定位相关页面集合,默认)/ root=抓根页(旧行为,噪声大)/ skip=跳过
    root_no_column_fallback: str = os.getenv("ROOT_NO_COLUMN_FALLBACK", "search")
    # 栏目发现结果记录后多久重算一次(天),期间直接复用不重复识别;应对栏目动态变化
    auto_column_refresh_days: int = int(os.getenv("AUTO_COLUMN_REFRESH_DAYS", "7"))
    # 单个根域源"栏目自动发现"的时间上限(秒):要抓根页+逐个验证候选栏目,不设限会拖垮整批
    column_discovery_budget_seconds: int = int(os.getenv("COLUMN_DISCOVERY_BUDGET_SECONDS", "60"))
    # 整个采集任务的总时长上限(秒)。超时即收尾并标注未完成的源,保证任务不会永远挂着
    job_max_seconds: int = int(os.getenv("JOB_MAX_SECONDS", "3600"))

    # 同稿去重:回溯比对天数;缺标题时判"正文长度相近"的比值下限
    dedup_lookback_days: int = int(os.getenv("DEDUP_LOOKBACK_DAYS", "30"))
    dedup_len_ratio_min: float = float(os.getenv("DEDUP_LEN_RATIO_MIN", "0.6"))
    # 列表/RSS 单页最多取多少条
    list_max_items: int = int(os.getenv("LIST_MAX_ITEMS", "80"))
    rss_max_items: int = int(os.getenv("RSS_MAX_ITEMS", "50"))

    # 源自动发现:搜索/采集中出现的新域名累积证据评分≥此值即自动建 trial 源(自动入库,
    # 仍 S4 待人工定级)。越低越激进(新源多但杂),越高越保守。留空则用 discovery.yaml 的值。
    discovery_auto_trial_threshold: float = float(os.getenv("DISCOVERY_AUTO_TRIAL_THRESHOLD", "4.0"))
    # 主动找源命中的渠道:LLM 初评相关度≥此值即视为"第二重证据",可单通道自动入库试运行。
    # 否则主动找源找到的渠道永远卡在候选池(单通道过不了多通道闸门)。0=关闭这条通路
    discovery_probe_pass: float = float(os.getenv("DISCOVERY_PROBE_PASS", "0.7"))
    # 候选池自动清理:已初评且相关度低于此值、且超过 N 天没再出现的候选自动清掉,
    # 免得池子无限膨胀最后没人看。任一项设 0 = 不清理
    candidate_prune_relevance: float = float(os.getenv("CANDIDATE_PRUNE_RELEVANCE", "0.2"))
    candidate_prune_days: int = int(os.getenv("CANDIDATE_PRUNE_DAYS", "30"))

    # 源健康:连续失败(采集异常/试抓抓不到)达到此次数即自动标记停用(不再采集)。默认 3
    source_auto_retire_fail_streak: int = int(os.getenv("SOURCE_AUTO_RETIRE_FAIL_STREAK", "3"))
    # 冗余度:没有哪个站天天出稿,"连续 N 次没产出"不等于源坏了。自动停用还需同时满足
    # "距上次成功产出已超过这么多天";未达天数只标『观察中』不停用。默认 30 天
    source_quiet_tolerance_days: int = int(os.getenv("SOURCE_QUIET_TOLERANCE_DAYS", "30"))
    # 这些可信度等级的源永不自动停用(官方权威源低频但不可替代,误杀代价远大于留着)
    auto_retire_protect_credibility: str = os.getenv("AUTO_RETIRE_PROTECT_CREDIBILITY", "S1,S2")
    # 自动停用的源隔这么多天自动复检一次,能出数据就自动恢复(误杀自愈)。0=不复检
    retired_recheck_days: int = int(os.getenv("RETIRED_RECHECK_DAYS", "14"))

    # 主动找源(D5):用"找源专用检索词"定期去搜索引擎捞新渠道,而不是只等已采内容引用
    prospect_enabled: bool = os.getenv("PROSPECT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    # 默认带上搜狗微信:用户要的执法通报(网警/网信办)大多发在公众号里,只搜网页会漏掉一大类
    prospect_engines: str = os.getenv("PROSPECT_ENGINES", "bing_search,sogou_wechat,baidu_search")
    # 候选引擎池:自动调优每轮把池子里每个都测一遍——掉线的踢出、恢复的加回来,
    # 不必人工先点自检再去改上面的列表
    prospect_engines_all: str = os.getenv("PROSPECT_ENGINES_ALL",
                                          "bing_search,sogou_wechat,baidu_search")
    prospect_autotune: bool = os.getenv("PROSPECT_AUTOTUNE", "true").lower() in ("1", "true", "yes", "on")
    # 公众号文章要抓一次才知道属于哪个号,单轮最多解析这么多条(每条一次网络请求)
    prospect_wechat_resolve_max: int = int(os.getenv("PROSPECT_WECHAT_RESOLVE_MAX", "30"))
    prospect_pages_per_query: int = int(os.getenv("PROSPECT_PAGES_PER_QUERY", "2"))
    prospect_query_cap: int = int(os.getenv("PROSPECT_QUERY_CAP", "150"))      # 单轮最多跑多少条找源词
    # 百度/必应的结果链接是自家跳转链,不还原就只能得到 baidu.com——单轮最多还原这么多条
    prospect_resolve_max: int = int(os.getenv("PROSPECT_RESOLVE_MAX", "400"))
    # 某引擎在本轮内连续失败到这个次数就停用,不再拿剩下的词去撞反爬(0=不停用)
    prospect_engine_fail_streak: int = int(os.getenv("PROSPECT_ENGINE_FAIL_STREAK", "8"))
    # 两次搜索请求之间的间隔(秒),连错时按 2 倍退避,封顶 30 秒。0=不等
    prospect_delay_seconds: float = float(os.getenv("PROSPECT_DELAY_SECONDS", "1.5"))
    # 某个"我们早就有的域"在一轮里霸榜到这个次数,后续的词就用 -site: 把它排掉
    prospect_exclude_after_hits: int = int(os.getenv("PROSPECT_EXCLUDE_AFTER_HITS", "25"))
    prospect_exclude_max_sites: int = int(os.getenv("PROSPECT_EXCLUDE_MAX_SITES", "6"))
    # 已有源站点上搜到的相关页面 → 反推该站漏采的栏目,每站最多补这么多个
    prospect_column_hint_max: int = int(os.getenv("PROSPECT_COLUMN_HINT_MAX", "8"))
    prospect_weekday: int = int(os.getenv("PROSPECT_WEEKDAY", "0"))            # 每日自动化里周几跑(0=周一)
    # 候选源 LLM 相关度初评:抓候选站首页抽样标题,让模型判"是否持续产出国内安全事件内容"
    probe_llm_enabled: bool = os.getenv("PROBE_LLM_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    probe_sample_titles: int = int(os.getenv("PROBE_SAMPLE_TITLES", "12"))
    probe_ttl_days: int = int(os.getenv("PROBE_TTL_DAYS", "30"))               # 初评结果多久重评一次
    probe_max_per_round: int = int(os.getenv("PROBE_MAX_PER_ROUND", "20"))     # 单轮最多评多少个候选

    # 覆盖度盘点:按行业统计近 N 天事件数,低于下限即判为"覆盖空白",据此生成找源方向
    coverage_window_days: int = int(os.getenv("COVERAGE_WINDOW_DAYS", "90"))
    coverage_min_events: int = int(os.getenv("COVERAGE_MIN_EVENTS", "3"))

    # 源库自动运维(自动驾驶):到点自己做整理/定位/体检/找源/定级,不用人按按钮
    autopilot_enabled: bool = os.getenv("AUTOPILOT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    autopilot_hour: int = int(os.getenv("AUTOPILOT_HOUR", "4"))        # 每天几点(UTC)检查一次到期任务
    autopilot_dedup_days: int = int(os.getenv("AUTOPILOT_DEDUP_DAYS", "7"))
    autopilot_locate_days: int = int(os.getenv("AUTOPILOT_LOCATE_DAYS", "7"))
    autopilot_locate_max: int = int(os.getenv("AUTOPILOT_LOCATE_MAX", "10"))    # 每轮最多定位几个站
    autopilot_health_days: int = int(os.getenv("AUTOPILOT_HEALTH_DAYS", "3"))
    autopilot_health_max: int = int(os.getenv("AUTOPILOT_HEALTH_MAX", "25"))    # 每轮最多体检几个源
    autopilot_prospect_days: int = int(os.getenv("AUTOPILOT_PROSPECT_DAYS", "7"))
    autopilot_grade_days: int = int(os.getenv("AUTOPILOT_GRADE_DAYS", "1"))
    autopilot_candidates_days: int = int(os.getenv("AUTOPILOT_CANDIDATES_DAYS", "1"))
    autopilot_engines_days: int = int(os.getenv("AUTOPILOT_ENGINES_DAYS", "3"))
    autopilot_seeds_days: int = int(os.getenv("AUTOPILOT_SEEDS_DAYS", "7"))

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
