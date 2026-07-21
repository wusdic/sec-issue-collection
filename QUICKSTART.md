# 本地试用指南(不影响系统与其它程序)

**核心原理**:程序默认用 **Python 虚拟环境(venv)隔离依赖 + SQLite 单文件数据库 + 离线 MockLLM**,
不依赖任何需要"开启/停止"的后台服务(PostgreSQL/Redis/MinIO 都不需要)。
所有产物都在项目目录内(`.venv/` 和 `data/`),删掉即彻底清除,系统无残留。

## 一、最简试用(Linux / macOS)

```bash
git clone <本仓库地址> && cd sec-issue-collection
bash scripts/run_local.sh
```

脚本自动完成:建 venv → 装依赖(隔离)→ 跑 20 项测试 → 初始化 → 离线端到端演示。
演示会打印:一篇"某医院遭勒索、要求200万赎金"样例文章的处理结果——
抽取出赎金但 `loss_L1=未披露`(要求≠损失红线生效)→ 复核发布 → 回访任务 → 销售线索。

## 二、手动分步(想看清每一步 / Windows)

```bash
# 1. 建独立虚拟环境(与系统 Python、其它项目完全隔离)
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 2. 装依赖(全部进 .venv,不碰系统)
pip install fastapi uvicorn "sqlalchemy>=2.0" "pydantic>=2.6" httpx PyYAML \
    jsonschema typer beautifulsoup4 lxml python-dateutil pytest

# 3. 离线跑通(不联网、用 MockLLM)
export LLM_PROVIDER=mock             # Windows: set LLM_PROVIDER=mock
python -m app.cli init               # 建库 + 加载 32 个种子源 + 建账号
python -m app.cli demo               # 端到端演示
python -m pytest -q                  # 20 项测试
```

## 三、打开管理后台(真正给人用的界面)

```bash
source .venv/bin/activate
LLM_PROVIDER=mock uvicorn app.main:app --port 8000
```

浏览器打开 **http://127.0.0.1:8000/** —— 这是**中文管理后台**(仪表盘 / 事件 / 复核台 /
销售线索 / 数据源 / 采集),不是那个满屏 GET/POST 的接口页。

- 用默认账号 **admin / ChangeMe!2026** 登录;
- 首次是空库,点仪表盘上的「**一键载入演示数据**」,立刻能看到事件、线索、复核任务的效果;
- 想看真实采集,去「采集」页点「开始采集」(会真实访问网站,失败源会在错误报告里列出,属正常)。

（`http://127.0.0.1:8000/docs` 仍保留,是给开发者调试接口用的,现已支持右上角 Authorize 按钮。）
端口被占用就换一个:`--port 8123`。 `Ctrl+C` 停止(临时进程,不是系统服务)。

## 四、为什么不影响别的程序(逐条)

| 顾虑 | 实际情况 |
|---|---|
| 会不会污染系统 Python 包? | 不会。依赖全在 `.venv/`,`deactivate` 后与系统无关 |
| 要不要装数据库服务? | 不用。默认 SQLite = `data/app.db` 一个文件,非服务、不常驻、不占端口 |
| 会不会开机自启 / 后台常驻? | 不会。没有任何 systemd/服务注册,uvicorn 是你手动起、手动停的前台进程 |
| 会不会联网偷跑? | 不会。`LLM_PROVIDER=mock` 全程离线;只有你执行 `run-daily` 才会去抓真实网站 |
| 占端口吗? | 仅当你起 uvicorn 时占一个(默认 8000,可改),停掉即释放 |

## 五、彻底清除

```bash
deactivate            # 退出虚拟环境
rm -rf .venv data     # 删除环境与全部数据,系统无残留
```

## 六、进阶:接真实 LLM / 真实采集(可选,非试用必需)

试用满意后想让它真跑起来:复制 `.env.example` 为 `.env`,填国产 LLM 的 OpenAI 兼容接口
(如 Qwen / DeepSeek 的 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`),然后:

```bash
LLM_PROVIDER=openai_compat python -m app.cli run-daily --limit-sources 3
```

生产级(PostgreSQL + Redis + MinIO + 定时任务)用 `docker compose up`,与本地试用互不影响。
