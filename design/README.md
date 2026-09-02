# 设计文档索引

| 路径 | 性质 | 读法 |
|---|---|---|
| `design/platform/00-总纲.md` … `09-同类项目借鉴评估.md` | **平台通用设计**(需求画像驱动、模块式分层) | 从 00 开始;新增需求看 05 §3 与 07 决策表;改代码看 08 |
| `config/need_profile.template.yaml` | 画像模板(v1.2+,全维度、9 个必填键) | 复制为 `config/need_<id>.yaml` |
| `config/need_sec_events.yaml` | 首个实例:国内行业安全事件库(最完整的画像样板) | 参考写法,不要把其中取值当平台约束 |
| `config/need_policy_watch.yaml` 等 | 其它实例/场景画像(政策、医疗政策、招标、企业动态、省市文件) | 对照 06 |
| `docs/需求与解决方案.md`、`design/详细设计.md`、`design/搜索行为逻辑与能力规范.md`、`design/通用信息搜索框架.md`、`design/schema.sql` | 首个实例的**原始**需求与设计(平台化之前,历史) | 只作背景;与 platform 冲突时以 platform 为准 |
| `CODE.md` | 代码导读 | — |
