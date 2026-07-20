# 代码说明(P1 MVP 实现)

按《通用信息搜索框架》实现:**全表带 `need_id`、画像(need profile)驱动**。
安全事件库(`sec_events`)是第一个实例把框架跑实;政策库(`policy_watch`)是第二个实例验证泛化性。

## 快速开始(离线可跑,无需网络/外部服务)

```bash
pip install -r requirements.txt          # feedparser 编译失败可忽略(RSS 探测降级)
python -m app.cli init                   # 建库 + 加载 sec_events 画像/词表/32 种子源 + 建账号
python -m app.cli demo                   # 离线端到端:采集→抽取→红线校验→复核发布→回访→线索
pytest -q                                # 20 项测试(含金额红线/发布红线/双签/去重)
uvicorn app.main:app                     # 起 API,默认 SQLite + MockLLM
```

真实运行:配置 `.env`(`LLM_PROVIDER=openai_compat` + PostgreSQL),`python -m app.cli run-daily`。

## 目录

| 路径 | 内容 |
|---|---|
| `app/models.py` | 全部数据表(带 need_id 维度);对应 `design/schema.sql` |
| `app/services/profiles.py` | 需求画像加载与校验(六要素+基准,框架实例化入口) |
| `app/services/pipeline.py` | 主流水线:采集→存档→去重→粗筛→抽取→记录去重→草稿 |
| `app/services/adapters.py` | 适配器框架:页面型/查询型 + generic_rss/generic_list 零适配器接入 |
| `app/services/archive.py` | 原文存档降级链 L-A→L-B→L-C→L-D(方案 7.3) |
| `app/services/dedup.py` | 三层去重:URL / SimHash 同稿簇 / 指纹+语义召回 |
| `app/services/extraction.py` | 粗筛 + LLM 结构化抽取 + schema 校验 + 完备度评分 |
| `app/services/money_guard.py` | **金额三态红线**:要求≠损失、赎金隔离、confirmed 降级 |
| `app/services/events.py` | 事件 CRUD/合并/**发布红线校验**(confirmed 需 S1/S2) |
| `app/services/review.py` | 复核状态机 + 金额双签 |
| `app/services/followup.py` | 生命周期回访 T+N + 一键检索包 |
| `app/services/leads.py` | 四维产品映射 + 线索评分 + 采购窗口三阶段 |
| `app/services/discovery.py` | 源发现引擎:证据登记/评分/自动 trial/黑名单 |
| `app/services/scheduler.py` | 分级调度 + SLA 反向驱动 + 每日主任务 |
| `app/services/kpi.py` | 看板/损失口径/控制缺失/白区/可追溯硬约束 |
| `app/services/llm.py` | LLM 抽象层(OpenAI 兼容 + MockLLM 离线) |
| `app/api/routes.py` | REST API `/api/v1`(详细设计 §5) |
| `app/cli.py` | 运维 CLI:init / run-daily / demo / verify-archives |
| `tasks/celery_app.py` | Celery beat 调度装配 |
| `config/need_*.yaml` | 需求画像:sec_events(事件型)/ policy_watch(文档型) |

## 红线在代码中的三层强制

1. **抽取层** `money_guard.apply_guard`:声称语境金额自动降 claimed;赎金金额禁入损失通道。
2. **复核层** `review.approve`:含 confirmed 金额强制双人签,一审二审不同人;二审才清 `pending_human`。
3. **发布层** `events.validate_publish`:confirmed 金额无 S1/S2 来源→拒绝发布;报表层 `kpi.traceability_check` 再兜一层,违规则拒绝出数。

三层对应 `design/schema.sql` 中的 PG 触发器(生产库级兜底)。

## 框架泛化的落地证据

`config/need_policy_watch.yaml`(政策库,记录原型=**文档型**)与 sec_events(**事件型**)
共用同一套 models/pipeline/adapters/dedup/discovery/archive,仅画像+Schema+词表不同——
`pytest tests/test_leads_and_pipeline.py::test_need_isolation` 验证两实例数据隔离、引擎零改动。

## 与设计文档的对应

代码是 `design/详细设计.md`(M1–M11、§5 API、§8 源发现)与
`design/搜索行为逻辑与能力规范.md`(B1–B8/C1–C10,已实现 B1 事件发现、B4 回访、
B6 同款预警骨架、C1/C3/C4/C8/C10 能力)的可运行落地。未覆盖项(Playwright 截图需
`PLAYWRIGHT_ENABLED=1`、前端页面、CRM 对接)见 `design/搜索行为逻辑与能力规范.md` 第 5 节待编写清单。
