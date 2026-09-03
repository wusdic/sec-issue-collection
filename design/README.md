# 设计文档索引

| 路径 | 性质 | 读法 |
|---|---|---|
| `design/platform/README.md` → `00-总纲.md` … `08-演进与借鉴.md` | **平台通用设计**(任务模式 + 参数库、模块式分层) | 从 [platform/README.md](platform/README.md) 或 00 开始;新建任务看 01 与 05 决策表;查参数看 02;改代码看 03/07 |
| `config/tasks/task.template.yaml` | **任务模板**:任务 = 参数库引用 + 覆盖 + 节奏/状态 | 复制为 `config/tasks/<id>.yaml`;`task-compile` 看结果,`task-setup` 装载 |
| `config/tasks/*.yaml` | 五个任务样板(招标中标、安全法规、医疗政策、企业动态、南京文件) | 对照 platform/05 场景矩阵 |
| `config/library/<kind>/<id>.yaml` | **参数库**:可复用的画像片段(地域/行业/文种/记录形态/可信度/节奏/输出…) | `library-list` 查看,`library-extract` 提炼 |
| `config/need_profile.template.yaml` | 画像模板(全维度;任务编译的目标形态) | 手写画像时复制为 `config/need_<id>.yaml`;查键的写法 |
| `config/need_sec_events.yaml` | 首个实例:国内行业安全事件库(最完整的画像样板) | 参考写法,不要把其中取值当平台约束 |
| `docs/需求与解决方案.md`、`design/详细设计.md`、`design/搜索行为逻辑与能力规范.md`、`design/通用信息搜索框架.md`、`design/schema.sql` | 首个实例的**原始**需求与设计(平台化之前,历史) | 只作背景;与 platform 冲突时以 platform 为准 |
| `CODE.md` | 代码导读 | — |
