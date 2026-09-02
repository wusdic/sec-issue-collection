"""FastAPI 应用入口。根路径 / 返回管理后台前端;/api/v1 为接口;/docs 为调试文档。"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.routes import api
from app.db import init_db

app = FastAPI(title="通用数据采集平台(需求画像驱动)", version="0.2.0")
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
        # 启动即把内置种子源清单载入(幂等:已有的不动,只补新的)。
        # 升级后新增的内置源不该还要人去跑一次 CLI;自动运维里也有同名任务按周期兜底。
        try:
            from pathlib import Path as _P
            from app.services import profiles
            for nid in profiles.active_need_ids(db):
                seeds = profiles.need_paths(nid)["sources"]
                if seeds and _P(seeds).exists():
                    profiles.load_seed_sources(db, nid, seeds)
            db.commit()
        except Exception:  # noqa: BLE001 种子载入失败不阻断启动
            db.rollback()
        # 升级新加的找源引擎补进在用列表(只补"从没被自检评价过"的,
        # 不会把测出来不可用、已被踢掉的老引擎塞回去)
        try:
            from app.services import prospect
            prospect.sync_new_engines(db)
            db.commit()
        except Exception:  # noqa: BLE001 引擎同步失败不阻断启动
            db.rollback()
        # 启动即自动校正源键并查重合并(同采集目标的重复源自动并一),无需人工扫描
        from app.services import discovery
        try:
            discovery.recompute_keys(db)
            db.commit()
        except Exception:  # noqa: BLE001 一致性维护失败不阻断启动
            db.rollback()
    finally:
        db.close()
    # 回收上次进程残留的 running 僵尸任务,否则"开始采集"会一直提示已有任务在跑
    from app.services.crawl_runner import reap_orphan_jobs
    try:
        reap_orphan_jobs()
    except Exception:  # noqa: BLE001
        pass
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
