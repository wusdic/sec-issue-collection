#!/usr/bin/env bash
# 本地一键试用(不影响系统与其它程序):venv 隔离依赖 + SQLite 文件库 + 离线 MockLLM。
# 全部产物都在项目目录内(.venv/ 与 data/),删除即彻底清除,不动任何系统服务。
set -e
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

echo "== 1/4 创建独立虚拟环境 .venv =="
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "== 2/4 安装依赖(隔离在 .venv,不碰系统 Python) =="
pip install -q --upgrade pip
# feedparser 依赖 sgmllib3k 在部分系统编译失败,单独可选安装,失败不影响主功能
pip install -q fastapi uvicorn "sqlalchemy>=2.0" "pydantic>=2.6" httpx PyYAML \
    jsonschema typer beautifulsoup4 lxml python-dateutil pytest
pip install -q feedparser 2>/dev/null && echo "  (feedparser 已装,RSS 探测可用)" \
    || echo "  (feedparser 跳过,不影响主功能)"

echo "== 3/4 跑测试(20 项,含金额红线/发布红线/去重) =="
LLM_PROVIDER=mock python -m pytest -q

echo "== 4/4 初始化 + 离线端到端演示 =="
export LLM_PROVIDER=mock
export DATABASE_URL="sqlite:///${PROJECT_DIR}/data/app.db"
export ARCHIVE_ROOT="${PROJECT_DIR}/data/archive"
python -m app.cli init
python -m app.cli demo

cat <<EOF

=========================================================
本地试用完成 ✓  数据都在 ${PROJECT_DIR}/data/(SQLite + 存档)
下一步(可选):打开管理后台
    source .venv/bin/activate
    LLM_PROVIDER=mock uvicorn app.main:app --port 8000
  浏览器打开 http://127.0.0.1:8000/  (中文管理后台,非接口页)
  登录 admin / ChangeMe!2026,点“一键载入演示数据”即可看到效果  (Ctrl+C 停止)
彻底清除:退出 venv(deactivate)后删除 .venv/ 与 data/ 即可,系统无残留。
=========================================================
EOF
