"""FastAPI 应用入口。根路径 / 返回管理后台前端;/api/v1 为接口;/docs 为调试文档。"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.routes import api
from app.db import init_db

app = FastAPI(title="通用信息搜索框架 · 安全事件库", version="0.1.0")
app.include_router(api)

_WEB = Path(__file__).resolve().parent / "web" / "index.html"


@app.on_event("startup")
def _startup():
    init_db()
    # 载入页面保存过的运行时配置(覆盖 .env 默认)
    from app.db import SessionLocal
    from app.services.settings_service import load_from_db
    db = SessionLocal()
    try:
        load_from_db(db)
        # 启动即自动校正源键并查重合并(同采集目标的重复源自动并一),无需人工扫描
        from app.services import discovery
        try:
            discovery.recompute_keys(db)
            db.commit()
        except Exception:  # noqa: BLE001 一致性维护失败不阻断启动
            db.rollback()
    finally:
        db.close()
    # 每日自动采集调度(进程内轻量,daily_auto_enabled 关闭时线程空转不做事)
    from app.services import daily
    daily.start()


@app.get("/", include_in_schema=False)
def home():
    """管理后台首页(真正给人用的界面,不是 /docs 那个接口调试页)。"""
    return FileResponse(_WEB)


@app.get("/healthz")
def healthz():
    return {"ok": True}
