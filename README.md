# 通用数据采集平台 · 首个实例:国内行业安全事件库(sec-issue-collection)

一套**需求画像驱动**的数据采集平台:采集/找源/去重/粗筛/抽取/三态守卫/复核发布/回访/线索/报表/日报/界面
全部是通用引擎,行业个性只存在于**任务**里:每新建一个任务(`config/tasks/<id>.yaml`)就规定这个任务要干的事——
采集什么信息、节奏多快、怎么判、怎么存;任务里共性的参数提炼成**参数库**(`config/library/`)供其它任务直接复用;
任务编译成画像(引擎契约)后引擎零改动。首个实例是**国内行业安全事件库**(每日采集国内网络/信息安全事件,
形成可用于商业决策的结构化事件库,`config/need_sec_events.yaml` 是完整画像样板);政策监管动态库、医疗政策、
招标中标、企业动态、省市文件五个任务(`config/tasks/`)证明"零改引擎、只换任务"。

平台主线:**针对一个信息需求,找得快、找得全、确认真实、整理成好用可用的记录、按分类存在本地**;
通知(邮件/飞书/webhook)与导出(本地资料库/多维表格)是可插拔组件。

平台设计与验证见 `design/platform/`(从 [00 总纲](design/platform/00-总纲.md) 开始):
[01 架构设计与实施方案](design/platform/01-实施方案.md)(模块式分层,每层的需求与验收)·
[02 业务逻辑与参数化维度](design/platform/02-业务逻辑与参数化维度.md) ·
[03 功能说明书](design/platform/03-功能清单.md)(每个功能:作用/给谁用/入口/用法要点)·
[04 多视角验证](design/platform/04-多视角验证.md) ·
[05 落地与验证记录](design/platform/05-落地验证.md)(含"新增一个需求"操作清单)·
[06 场景适用性与能力模组](design/platform/06-场景适用性与能力模组.md)(四个场景画像,能力模组目录)·
[07 需求泛化模型与决策表](design/platform/07-需求泛化模型与决策表.md)(六根轴 + 需求特征→画像键 决策表,生成式验证)·
[08 模块边界与升级规范](design/platform/08-模块边界与升级规范.md)(分层、契约、扩展点、升级操作规范)·
[09 同类项目借鉴评估](design/platform/09-同类项目借鉴评估.md)(data-collector / caijifagui / data-compliance-platform 的吸收、排期与不采用)。
画像模板:[`config/need_profile.template.yaml`](config/need_profile.template.yaml)(v1.2 全维度,9 个必填键)。

快速开始:`python -m app.cli init`(装载默认需求;`--all` 装载全部画像)→ `uvicorn app.main:app` → 登录后头部可切换需求。
新建任务(v1.5):复制 `config/tasks/task.template.yaml` → `use` 参数库条目 + 写本任务特有部分 → `python -m app.cli task-setup <id>`;详见 [10 任务模式与参数库](design/platform/10-任务模式与参数库.md)。

## 仓库结构

> 表中带「实例」的文件属于首个实例(安全事件库)或其历史设计;平台通用设计只看 `design/platform/`(索引见 [design/README.md](design/README.md))。

| 路径 | 内容 |
|---|---|
| [`docs/需求与解决方案.md`](docs/需求与解决方案.md) | 实例 · 首个实例的需求方案(历史基线) |
| [`schema/sec_event.schema.json`](schema/sec_event.schema.json) | 事件记录字段规范(JSON Schema,机器可读) |
| [`schema/sec_dictionaries.yaml`](schema/sec_dictionaries.yaml) | 标准词表:行业、攻击类型、入口、数据类型、采购品类、来源可信度分级 |
| [`design/详细设计.md`](design/详细设计.md) | 实例 · 平台化前的详细设计(历史) |
| [`design/搜索行为逻辑与能力规范.md`](design/搜索行为逻辑与能力规范.md) | 八类搜索行为(B1–B8)× 十项通用能力(C1–C10)统一规范,含监控名单机制与待编写清单 |
| [`design/通用信息搜索框架.md`](design/通用信息搜索框架.md) | 领域无关的泛化框架:信息需求六要素、通用流水线、G1–G8 泛化搜索行为、多需求并行架构 |
| [`config/need_profile.template.yaml`](config/need_profile.template.yaml) | 信息需求画像模板(新需求实例化配置) |
| [`design/schema.sql`](design/schema.sql) | 实例 · PostgreSQL DDL 设计稿(历史;运行表结构以 app/models.py 为准) |
| [`config/seed_sources_sec.yaml`](config/seed_sources_sec.yaml) | 种子源清单(32 个,含适配器/频率/可信度/反爬备注) |
| [`config/keyword_matrix_sec.yaml`](config/keyword_matrix_sec.yaml) | 每日定题检索关键词矩阵初版 |
| [`config/discovery_sec.yaml`](config/discovery_sec.yaml) | 源发现引擎配置:导航/聚合站清单、找源检索词、候选评分与自动试运行阈值 |

## 核心设计要点

- **决策字段优先**:不止记录"发生过攻击",强制采集损失、罚款、责任、事故后采购等商业字段;
- **损失六分类(L1–L6)**:直接资金 / 收入产量 / 响应处置 / 罚款诉讼 / 商业机会 / 公共物理影响,分开统计;
- **金额三态**:声称(claimed)/ 第三方估算(estimated)/ 已确认(confirmed)三通道互不覆盖,
  赎金"要求"绝不等于"损失",只有企业公告、法院、监管明确确认的金额才进"已确认";
- **来源可信度 S1–S4 分级**,字段级来源绑定 + 网页快照存档;
- **事件生命周期**:罚款、判决、整改采购滞后数月,T+30/90/180/365 自动回访回填,
  重点联动招标采购网站验证事故后实际采购(最直接的商机数据);
- **合规红线**:只采集公开信息,严禁接触泄露数据本体;
- **查全/查新/查重闭环**:每日定题搜索+引用挖掘持续发现新源(候选→试运行→转正的源生命周期),
  月度基准清单对标度量漏报率;分级抓取频率+热搜突发触发保时效(披露→入库 ≤24h);
  URL/文档(SimHash 同稿簇)/事件(指纹+语义召回)三层去重。

## 下一步

按方案第 13 节《采集软件功能需求清单》(模块 M1–M9)进行软件设计。
