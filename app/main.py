"""FastAPI 应用入口。"""
from fastapi import FastAPI

from app.api.routes import api
from app.db import init_db

app = FastAPI(title="通用信息搜索框架 · 安全事件库", version="0.1.0")
app.include_router(api)


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/healthz")
def healthz():
    return {"ok": True}
