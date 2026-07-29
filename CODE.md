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
| `app/services/discovery.py` | 源发现引擎(被动):采集伴生的证据登记/评分/自动 trial/黑名单 |
| `app/services/prospect.py` | **主动找源(D5)**:找源专用检索词捞新渠道 + 候选源 LLM 相关度初评 |
| `app/services/coverage.py` | **覆盖度盘点**:哪些行业近期零事件 → 自动生成该方向的找源词 |
| `app/services/columns.py` | **采集源精准化**:根域站点自动识别相关栏目 + 三重验证(篇数/结构一致性/内容相关度)+ 落库复用 |
| `app/services/locate.py` | 批量「精准定位栏目」后台任务(可查进度/可取消) |
| `app/services/health.py` | 源体检:后台批量试抓、连续失败自动停用、误判恢复 |
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

## 采集源必须"精准到栏目"

只填网站根地址(如 `https://www.cac.gov.cn/`)的源抓到的是首页要闻(领导活动、政策解读),
与安全事件无关。系统强制把每个源收敛到"具体栏目 / 能精准定位相关内容的页面集合":

1. **栏目自动识别**(`columns.discover_columns`):抓根页,按栏目名相关词打分挑候选;
2. **三重验证**(`columns.validate_column`):文章数 ≥ `column_min_articles`、URL 结构一致性 ≥
   `column_consistency_min`(是不是一个栏目)、**标题内容相关度 ≥ `column_relevance_min`**
   (是不是"安全"栏目——这条专门挡掉结构规整但全是要闻的栏目);
3. **落库复用**:通过的栏目建成子源挂在站点下,`auto_column_refresh_days` 内不重算;
   人工删除过的栏目不会被自动拉回(`manually_retired`);
4. **兜底不抓首页**:识别不到相关栏目时按 `root_no_column_fallback` 处理,默认转
   `site:域名` + 需求关键词的站内检索(仍是精准页面集合),而不是退回抓根页;
5. **可见可操作**:`GET /sources` 返回每个源的 `precision`(column/resolved/search/wechat/root),
   页面上标红"还没精准到栏目"的源,可单个「定位栏目」或批量「🎯 精准定位栏目」。

`pytest tests/test_precise_sources.py` 覆盖上述全部行为。

## 源库怎么做到"越来越全、越来越准"

**越来越全 —— 被动 + 主动两路进候选池**

1. 被动(伴生):每篇入库文档顺手登记 3 类线索——搜索结果域名 `event_search`、正文
   「来源/转载自」`citation`、署名公众号 `wechat_reference`(`pipeline.ingest_item`);
2. 主动(D5):每周用「找源专用检索词」去搜索引擎捞渠道(`prospect.run_once`)。词表 =
   `config/discovery.yaml` 人工维护的基础词 **+ 覆盖空白自动生成的方向词**;
3. 覆盖度闭环:`coverage.industry_coverage` 按词表行业统计近 90 天事件数,低于下限判为
   空白 → `coverage.prospect_queries` 翻译成该行业的找源词喂回第 2 步,"缺哪块找哪块";
4. 一站裂变多栏目(`columns.discover_and_persist`)、直连抓不到转站内检索兜底。

**越来越准 —— 四道闸门,但都留了冗余**

1. 多通道硬闸门:单通道孤证不自动入库,需 ≥2 个发现通道或曾为同稿首发;
2. **LLM 相关度初评**(`prospect.probe_one`):抓候选站首页抽样标题,让模型判"是否持续
   产出国内安全事件内容"(0-1),结果落 `source_probe` 表并按 TTL 复用,计入候选评分
   ——此前这一项权重最高却从没人算过、恒为 0,排序只看被提及次数;
3. 栏目三重验证:篇数 + URL 结构一致性 + 标题内容相关度(`columns.validate_column`);
4. 人工定级:候选一律 S4 试运行,`discovery.promote` 是唯一转正入口,不自动升级。

**冗余度:不轻易判死一个源**(`health.register_failure`)

没有哪个站天天出稿,"连续几轮没产出"不等于源坏了。自动停用要**同时**满足:
连续失败 ≥ `source_auto_retire_fail_streak` **且** 距上次成功产出 >
`source_quiet_tolerance_days`(默认 30 天);未达标的只标『观察中』,照常参与采集。
`auto_retire_protect_credibility`(默认 S1,S2)里的官方权威源**永不**自动停用。
自动停用的源隔 `retired_recheck_days`(默认 14 天)在体检时自动复检,能出数据就自动恢复
——误杀可自愈。栏目篇数门槛也从 5 降到 3,免得一年发几条的执法通报栏目被挡在门外。

`pytest tests/test_prospect_coverage.py` 覆盖上述全部行为(15 例)。

## 框架泛化的落地证据

`config/need_policy_watch.yaml`(政策库,记录原型=**文档型**)与 sec_events(**事件型**)
共用同一套 models/pipeline/adapters/dedup/discovery/archive,仅画像+Schema+词表不同——
`pytest tests/test_leads_and_pipeline.py::test_need_isolation` 验证两实例数据隔离、引擎零改动。

## 与设计文档的对应

代码是 `design/详细设计.md`(M1–M11、§5 API、§8 源发现)与
`design/搜索行为逻辑与能力规范.md`(B1–B8/C1–C10,已实现 B1 事件发现、B4 回访、
B6 同款预警骨架、C1/C3/C4/C8/C10 能力)的可运行落地。未覆盖项(Playwright 截图需
`PLAYWRIGHT_ENABLED=1`、前端页面、CRM 对接)见 `design/搜索行为逻辑与能力规范.md` 第 5 节待编写清单。
