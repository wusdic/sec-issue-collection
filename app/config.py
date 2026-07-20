"""全局配置:环境变量驱动,含 LLM/存档/数据库等。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    # 数据库:默认 SQLite(开发/测试),生产用 PostgreSQL
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR}/data/app.db")

    # LLM:OpenAI 兼容接口,provider=mock 时离线可用
    llm_provider: str = os.getenv("LLM_PROVIDER", "mock")  # mock | openai_compat
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "")
    llm_screen_model: str = os.getenv("LLM_SCREEN_MODEL", "")  # 粗筛用小模型,空则同 llm_model

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

    # Celery
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # 配置文件路径
    config_dir: Path = BASE_DIR / "config"
    schema_dir: Path = BASE_DIR / "schema"


settings = Settings()
