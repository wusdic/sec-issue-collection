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
| `app/services/grading.py` | **试运行源自动定级/淘汰**:规则自动转正,红线等级只给建议 |
| `app/services/autopilot.py` | **源库自动运维**:按周期自己做整理/定位/体检/找源/定级,每步留痕 |
| `app/services/actions.py` | **动作分类分级**:系统每个有后果的动作按模块归类、按影响定级,高级别优先提示 |
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
   `config/discovery_sec.yaml` 人工维护的基础词 **+ 覆盖空白自动生成的方向词**;
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

## 源库自动运维:人只管拍板,其余交给系统

数据源模块原来有五件事全靠人点按钮,不点就不做。`services/autopilot.py` 把它们变成
按周期自动执行(`daily._autopilot` 每天 `autopilot_hour` 检查一次到期任务):

| 任务 | 默认周期 | 做什么 |
|---|---|---|
| dedup | 7 天 | 校正源键、同采集目标的重复源自动并一 |
| locate | 7 天 | 给还没精准到栏目的根域源定位栏目(每轮限 `autopilot_locate_max` 个) |
| health | 3 天 | 体检(优先最久没成功的)+ 到期停用源复检恢复(每轮限 `autopilot_health_max` 个) |
| prospect | 7 天 | 主动找源 + 候选 LLM 初评 + 评分自动入库 |
| grade | 1 天 | 试运行源自动定级/淘汰 |

每步落一行 `AutoOpsRun`(状态 + 结果摘要 + 失败原因),自动化但不黑箱;一步失败不影响其余步。

**自动定级怎么守住红线**(`grading.decide`,规则在 `config/discovery_sec.yaml` 的 `grading:`):

- **S1 自动给**——但只给"域名本身能证明官方身份"的:`.gov.cn`/`.mil.cn` 政务域名,
  或 `official_domains` 名录里的官方技术机构/法定披露平台(CNCERT、交易所、巨潮、裁判文书网…)。
  这是客观事实不是判断,自动给零风险;
- **S3 自动给**——试运行满 `trial_days` 且产出 ≥ `promote_min_docs`、相关率 ≥
  `promote_min_relevant_ratio`。S3 支撑不了"已确认"金额,自动化不会突破发布红线;
- **S2 只给建议**——企业自披露能支撑已确认金额,且"是不是该企业自己的官网"机器判不可靠,
  故写进 `suggest_credibility` 等人一键确认(`POST /sources/{id}/grade`);
- **自动淘汰**——产出 ≥ `retire_min_docs` 且相关率 ≤ `retire_max_relevant_ratio` 判为噪声源;
- **不下结论**——样本不足/未满试运行期 → 延长观察;人工添加的源只升不降,绝不自动淘汰。

## 「系统动作」模块:高级别动作一眼可见

自动化程度越高,越需要"系统到底动了什么"能一眼看到。`services/actions.py` 给每个有后果的
动作定 **模块 + 基础级别 + 影响面升级规则**,前端有独立的「系统动作」导航页(带未确认角标)。

分级按 **是否碰红线 × 影响面 × 可逆性**:

| 级别 | 判据 | 例子 |
|---|---|---|
| 4 紧急 | 碰发布红线,或大面积生效 | 自动定级 S1、生成含"已确认"金额的事件、采集整批失败、一次停用 ≥5 个源 |
| 3 重要 | 改变了源库构成 | 自动转正 S3、自动淘汰噪声源、新源自动入库、重复源自动合并 |
| 2 关注 | 有变化但低风险 | 自动定位栏目、停用源复检恢复、转"观察中" |
| 1 一般 | 例行动作 | 采集完成、自动生成草稿事件 |

**影响面自动升级**:同一动作一次作用于 `escalate_at` 条以上时级别 +1 —— 自动停用 1 个源是
"重要",一次停用 8 个就是"紧急"(那多半是网络/系统出了问题,不是这 8 个源同时坏了)。

只有 **级别 ≥ 3 且未确认** 的才优先提示:「系统动作」页顶部钉住 + 导航角标 + 对应模块
(数据源/采集/复核台)页顶横幅 + 仪表盘全局横幅。每条都带 `reversible`(一句话说明怎么撤销)
和"知道了"按钮;确认后不再顶,日志完整保留可按模块/级别/时间筛选回查。

`pytest tests/test_action_log.py` 覆盖分级、影响面升级、筛选排序、确认、以及自动停用/
自动定级/新源入库确实写入台账(16 例)。

`autopilot.human_todo` 汇总"机器判不了、真需要人拍板"的极少数,在数据源页顶部直接列出并
可一键处理;`GET /autopilot/grading-preview` 可先试算自动定级会怎么判,确认规则符合预期
再放手。`pytest tests/test_autopilot_grading.py` 覆盖上述全部规则与调度行为(17 例)。

## 框架泛化的落地证据

`config/need_policy_watch.yaml`(政策库,记录原型=**文档型**)与 sec_events(**事件型**)
共用同一套 models/pipeline/adapters/dedup/discovery/archive,仅画像+Schema+词表不同——
`pytest tests/test_leads_and_pipeline.py::test_need_isolation` 验证两实例数据隔离、引擎零改动。

## 与设计文档的对应

代码是 `design/详细设计.md`(M1–M11、§5 API、§8 源发现)与
`design/搜索行为逻辑与能力规范.md`(B1–B8/C1–C10,已实现 B1 事件发现、B4 回访、
B6 同款预警骨架、C1/C3/C4/C8/C10 能力)的可运行落地。未覆盖项(Playwright 截图需
`PLAYWRIGHT_ENABLED=1`、前端页面、CRM 对接)见 `design/搜索行为逻辑与能力规范.md` 第 5 节待编写清单。
