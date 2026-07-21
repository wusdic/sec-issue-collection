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


@app.get("/", include_in_schema=False)
def home():
    """管理后台首页(真正给人用的界面,不是 /docs 那个接口调试页)。"""
    return FileResponse(_WEB)


@app.get("/healthz")
def healthz():
    return {"ok": True}
