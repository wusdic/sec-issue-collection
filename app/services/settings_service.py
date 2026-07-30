"""运行时配置读写:页面「设置」编辑的字段白名单 + 持久化 + 即时生效。

只暴露可安全在运行时修改的项;数据库/密钥等结构性配置不经页面改(需重启)。
所有业务模块运行时读 settings 实例属性,setattr 即生效;LLM 客户端有缓存,改后重置。
"""
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AppSetting

# (key, 中文标签, 分组, 类型, 是否密钥, 说明)
EDITABLE = [
    ("llm_provider", "LLM 模式", "LLM 大模型", "enum:mock,openai_compat", False,
     "mock=离线测试(不调真实大模型);openai_compat=接真实 LLM(Qwen/DeepSeek 等)"),
    ("llm_base_url", "LLM 接口地址", "LLM 大模型", "str", False,
     "OpenAI 兼容接口,如 https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("llm_api_key", "LLM 密钥", "LLM 大模型", "str", True, "API Key,留空表示不修改已存值"),
    ("llm_model", "抽取模型", "LLM 大模型", "str", False, "结构化抽取用,如 qwen-plus / deepseek-chat"),
    ("llm_screen_model", "粗筛模型", "LLM 大模型", "str", False, "粗筛用小模型省钱,如 qwen-turbo;留空同抽取模型"),
    ("llm_max_tokens", "单次输出上限(token)", "LLM 大模型", "int", False,
     "抽取记录较大,推理模型(MiniMax-M3 等)还会先输出思维链,太小会截断 JSON 致抽取失败。默认 8192"),
    ("llm_timeout", "单次请求超时(秒)", "LLM 大模型", "float", False,
     "单次大模型调用超时即放弃该篇(不重试),避免个别慢篇卡住整批。推理模型偏慢可适当调大。默认 90"),
    ("llm_embed_base_url", "向量接口地址", "LLM 大模型", "str", False, "Embedding 接口,留空回退 LLM 接口"),
    ("llm_embed_api_key", "向量接口密钥", "LLM 大模型", "str", True, "留空表示不修改"),
    ("llm_embed_model", "向量模型", "LLM 大模型", "str", False,
     "语义去重用的 embedding 专用模型(不是聊天模型名!):MiniMax=embo-01、通义=text-embedding-v3、"
     "智谱=embedding-3;留空则禁用语义去重(不影响文章抽取)"),

    ("fetch_timeout", "抓取超时(秒)", "采集", "float", False, "单页抓取超时,默认 20"),
    ("crawl_delay_seconds", "抓取间隔(秒)", "采集", "float", False, "请求之间的礼貌延时,默认 2"),
    ("playwright_enabled", "启用浏览器渲染/截图", "采集", "bool", False, "动态页与整页截图需开启(需装 Playwright)"),
    ("screen_keep_threshold", "粗筛入选阈值", "采集", "float", False,
     "文章相关度≥此值才判为相关并抽取,0-1。调高=更严、少收不相干内容;默认 0.6"),
    ("screen_manual_threshold", "粗筛待定阈值", "采集", "float", False,
     "相关度在『待定阈值~入选阈值』之间的进人工待定,低于此值直接判为不相干过滤;默认 0.4"),
    ("crawl_stop_consecutive_seen", "翻页早停:连续已采过条数", "采集", "int", False,
     "增量采集:列表按时间倒序,连续遇到这么多条『已采过』或『太旧』就判定抓全、停止翻页。默认 15"),
    ("collect_recency_days", "时效窗口(天)", "采集", "int", False,
     "只采集发布时间在近这么多天内的内容,超出视为历史不入库。默认 1825=近5年。设 0=不限时效"),
    ("search_source_query_cap", "搜索型源关键词上限", "采集", "int", False,
     "搜索型源每次最多用多少个关键词,防 400 词硬打慢站空跑几十分钟。默认 30;页面型不受此限"),
    ("source_time_budget_seconds", "单源采集时长上限(秒)", "采集", "int", False,
     "单个源一次采集超过此秒数就停止剩余查询/翻页,避免个别慢源拖垮整批。默认 180;设 0=不限"),
    ("crawl_concurrency", "抓取并发数(多源同时)", "采集", "int", False,
     "同时抓取多少个源。默认 5;调大更快但更吃网络/内存(开渲染时每并发一个浏览器)。1=串行"),
    ("process_concurrency", "抽取并发数(多文档同时)", "采集", "int", False,
     "同时处理多少篇文档(粗筛+抽取的 LLM 调用并发)。默认 6;受大模型并发额度限制,报错就调小。1=串行"),
    ("render_max_concurrency", "渲染时抓取并发上限", "采集", "int", False,
     "开启浏览器渲染后,每个并发 worker 各占一个 Chromium(数百 MB),故抓取并发按此值封顶。默认 2"),
    ("auto_column_max", "根域源自动发现栏目数", "采集", "int", False,
     "根域政务站自动识别相关栏目(执法处罚/网络安全通报等)分别采集,每站最多这么多个栏目。默认 8"),
    ("column_min_articles", "栏目验证:最少文章数", "采集", "int", False,
     "候选栏目页至少有这么多篇文章才算有效栏目。默认 5"),
    ("column_consistency_min", "栏目验证:文章一致性下限", "采集", "float", False,
     "候选栏目内文章 URL 结构一致性≥此值才确认为一个栏目(0-1,越高越严)。默认 0.5"),
    ("column_relevance_min", "栏目验证:内容相关度下限", "采集", "float", False,
     "候选栏目里标题命中安全相关词的文章占比≥此值才算『相关栏目』(0-1)。"
     "这条挡掉『要闻/领导活动』这类结构规整但内容无关的栏目。调高=更严更精准,调低=更宽。默认 0.3"),
    ("column_relevance_min_titles", "栏目验证:相关度最少样本数", "采集", "int", False,
     "栏目里可读标题少于这么多条时无法判相关度,只按结构判(避免图片列表页被误杀)。默认 3"),
    ("root_no_column_fallback", "根域源未定位到栏目时", "采集", "str", False,
     "只填了网站根地址、又没识别出相关栏目时怎么办:search=按 site:域名+关键词站内检索精准定位(默认,推荐);"
     "root=退回抓首页(会混进要闻/领导活动等无关内容);skip=本轮跳过不抓"),
    ("column_discovery_budget_seconds", "单源栏目发现时长上限(秒)", "采集", "int", False,
     "一个根域源识别+验证栏目最多花这么久,超时就用已验证通过的。默认 60;设 0=不限"),
    ("auto_column_refresh_days", "栏目重识别周期(天)", "采集", "int", False,
     "栏目发现结果记录后多少天内直接复用不重算,过期才重新识别(应对栏目变动)。默认 7"),
    ("discovery_auto_trial_threshold", "新源自动入库阈值", "采集", "float", False,
     "采集中出现的新域名累积证据评分≥此值就自动建为 trial 试运行源(仍 S4 待人工定级)。"
     "调低→自动入库更激进、新源更多但更杂;调高→更保守。默认 4.0"),
    ("source_auto_retire_fail_streak", "源连续失败自动停用次数", "采集", "int", False,
     "某个源连续失败(采集异常或批量体检抓不到)达到此次数即计入停用条件。默认 3;调大更宽容"),
    ("source_quiet_tolerance_days", "源沉默容忍期(天)", "采集", "int", False,
     "冗余度:没有哪个站天天出稿。自动停用还必须『距上次成功产出超过这么多天』才生效,"
     "未超过的只标『观察中』照常采。默认 30;调大更宽容,调小更激进"),
    ("auto_retire_protect_credibility", "永不自动停用的可信度", "采集", "str", False,
     "这些等级的源无论连续失败多少次都不自动停用(官方权威源低频但不可替代)。默认 S1,S2;留空=不保护"),
    ("retired_recheck_days", "停用源自动复检周期(天)", "采集", "int", False,
     "被自动停用的源隔这么多天在体检时复检一次,能出数据就自动恢复(误杀自愈)。默认 14;0=不复检"),

    ("autopilot_enabled", "启用源库自动运维", "自动运维", "bool", False,
     "开启后系统按周期自己做:整理查重、给根域源定位栏目、体检与停用源复检、主动找源、"
     "试运行源自动定级/淘汰。不用人按按钮;每步都有执行记录可事后核对。默认开"),
    ("autopilot_hour", "自动运维时点(UTC)", "自动运维", "int", False,
     "每天几点检查有哪些维护任务到期。默认 4(≈北京 12 点),建议与采集时点错开"),
    ("autopilot_grade_days", "自动定级周期(天)", "自动运维", "int", False,
     "多久跑一次试运行源自动定级/淘汰。默认 1(每天)"),
    ("autopilot_health_days", "自动体检周期(天)", "自动运维", "int", False, "默认 3"),
    ("autopilot_health_max", "自动体检:每轮最多测几个源", "自动运维", "int", False,
     "分摊到多天跑完,避免一次几十分钟。默认 25(优先测最久没成功过的)"),
    ("autopilot_locate_days", "自动定位栏目周期(天)", "自动运维", "int", False, "默认 7"),
    ("autopilot_locate_max", "自动定位:每轮最多几个站", "自动运维", "int", False, "默认 10"),
    ("autopilot_prospect_days", "自动找源周期(天)", "自动运维", "int", False, "默认 7"),
    ("autopilot_dedup_days", "自动查重整理周期(天)", "自动运维", "int", False, "默认 7"),
    ("autopilot_seeds_days", "内置种子源载入周期(天)", "自动运维", "int", False,
     "多久把配置文件里的内置源清单载入一次(幂等,只补新的)。升级后不用人跑 CLI。默认 7"),
    ("autopilot_engines_days", "找源引擎自检周期(天)", "自动运维", "int", False,
     "多久自检一次搜索引擎并自动只保留可用的(被反爬的踢掉、恢复的加回来)。默认 3"),
    ("prospect_autotune", "找源引擎自动调优", "找源与覆盖", "bool", False,
     "开启后系统按自检结果自动增减「主动找源:用哪些搜索引擎」,不必人工先自检再改配置。"
     "关掉则完全按你手填的列表跑。默认开"),
    ("prospect_engines_all", "找源引擎候选池", "找源与覆盖", "str", False,
     "自动调优每轮把这个池子里每个引擎都测一遍;可用的写进上面的『用哪些搜索引擎』。"
     "实际池子 = 这里填的 ∪ 本版本内置的默认池,升级新加的引擎一定会被测到。"
     "默认 bing_rss,bing_search,sogou_wechat,baidu_search"),
    ("prospect_engines_tuned", "找源引擎:已自检评价过的", "找源与覆盖", "str", False,
     "系统自动维护,不用手填。用来分清某个引擎是「升级新加、从没测过」(补进在用列表给一次机会),"
     "还是「测过不可用被踢掉的」(不再塞回去)"),
    ("engine_on_topic_min", "引擎切题率下限", "找源与覆盖", "float", False,
     "结果标题里带查询词的比例低于此值,判定「这个引擎没在搜我们给的词」。实测必应 RSS 对"
     "「网警 处罚」返回日本 IT 咨询公司、加拿大税务局,域名却都合法——不看切题率拦不住,"
     "而无关结果比没结果更糟(看着像真结果,会混进候选池)。默认 0.3"),
    ("engine_on_topic_min_items", "切题率至少看多少条结果", "找源与覆盖", "int", False,
     "样本太少不下结论,免得偶然误伤。默认 20"),
    ("query_evolution_enabled", "启用找源词进化", "找源与覆盖", "bool", False,
     "开启后找源词按实际产出自我淘汰和自我扩张:算限定词增益、淘汰废词、从已采语料里挖新词。"
     "关掉则回到静态清单(config/discovery.yaml 里写什么就跑什么)。默认开"),
    ("term_weak_lift", "限定词增益低于多少算拖后腿", "找源与覆盖", "float", False,
     "增益 = 组合每轮产出 / 锚点单独跑的每轮产出。「零售 数据泄露」若不如「数据泄露」,增益就 <1。"
     "低于此值且样本达标的限定词退出组合池(仍可单独跑)。默认 0.8;调低更宽容"),
    ("term_min_samples", "限定词要有几条组合才评它", "找源与覆盖", "int", False,
     "单条组合的好坏容易被偶然性带偏,按词汇总取中位数,样本不够就不下结论。默认 3"),
    ("query_min_runs", "一条找源词跑够几轮才下结论", "找源与覆盖", "int", False,
     "冗余度:没有哪个网站天天都出一堆报道,跑一轮空手不代表这条词没用。默认 2"),
    ("query_rest_barren", "连续几轮空手转低频复测", "找源与覆盖", "int", False,
     "转「歇着」不是淘汰,只是降低轮换频率,之后仍会按复活配额定期复测。默认 4"),
    ("query_retire_runs", "跑够几轮且零有效结果才退休", "找源与覆盖", "int", False,
     "真正退休的门槛刻意设得很高:跑满这么多轮、且一条有效结果都没出过。默认 6"),
    ("query_share_baseline", "词表配额:锚点基线", "找源与覆盖", "float", False,
     "锚点单独跑的占比。这是增益的分母,砍到 0 等于关掉限定词评价。默认 0.2"),
    ("query_share_explore", "词表配额:探索新词", "找源与覆盖", "float", False,
     "没跑过的新词(变异/语料里挖来的)占比。调小则词表长得慢。默认 0.25"),
    ("query_share_revive", "词表配额:复活复测", "找源与覆盖", "float", False,
     "歇着的词轮换复测的占比——刻意留的冗余,免得低频好词被永久埋掉。默认 0.1"),
    ("harvest_min_titles", "挖新词至少需要多少篇语料", "找源与覆盖", "int", False,
     "从已采到且判为相关的文章标题里挖高频新词;语料太少挖出来的都是噪声。默认 30"),
    ("autopilot_queries_days", "找源词进化周期(天)", "自动运维", "int", False,
     "多久跑一次「算增益 + 淘汰废词 + 挖新词」。排在主动找源之前,当轮就能用上。默认 7"),
    ("autopilot_candidates_days", "候选池自动处理周期(天)", "自动运维", "int", False,
     "多久跑一次「补初评 + 达标入库 + 清理无关候选」。默认 1(每天),候选池不需要人去点"),
    ("candidate_prune_relevance", "候选清理:相关度低于多少算无关", "找源与覆盖", "float", False,
     "已初评且相关度低于此值、又长期没再出现的候选自动清掉,免得池子无限膨胀。默认 0.2;0=不清理"),
    ("candidate_prune_days", "候选清理:多久没出现算过期", "找源与覆盖", "int", False,
     "候选超过这么多天没有新证据才允许被清理(拿不准的一律留着)。默认 30;0=不清理"),

    ("prospect_enabled", "启用主动找源", "找源与覆盖", "bool", False,
     "开启后每周用『找源专用检索词』去搜索引擎捞新渠道,而不是只等已采文章引用。默认开"),
    ("prospect_weekday", "主动找源:周几跑", "找源与覆盖", "int", False,
     "每日自动化里周几触发一次主动找源。0=周一 … 6=周日。默认 0"),
    ("prospect_engines", "主动找源:用哪些搜索引擎", "找源与覆盖", "str", False,
     "逗号分隔的适配器名。默认 bing_rss,sogou_wechat,baidu_search。bing_rss 是必应的 XML 结果口:"
     "网页版 bing_search 的结果块由 JS 注入,不开渲染时抓回来的页面里根本没有结果链接。"
     "搜狗微信必须留着——执法通报(网警/网信办)大多发在公众号里;百度反爬较狠,可视情况去掉。"
     "这一项由「引擎自检并自动只留可用的」每 3 天自动维护,一般不用手改"),
    ("prospect_wechat_resolve_max", "主动找源:单轮解析几个公众号", "找源与覆盖", "int", False,
     "搜到的公众号文章要抓一次才知道属于哪个号,每条一次网络请求,按此值封顶。默认 30"),
    ("prospect_pages_per_query", "主动找源:每词翻几页", "找源与覆盖", "int", False, "默认 2"),
    ("prospect_resolve_max", "主动找源:跳转链还原上限", "找源与覆盖", "int", False,
     "百度/必应结果是自家跳转链,不还原只能得到 baidu.com;每条还原一次请求,按此值封顶。"
     "调小会让大量结果被跳过(结论里会写明跳过了多少)。同一条链只还原一次,重复的走缓存。默认 400"),
    ("prospect_engine_fail_streak", "主动找源:引擎连错几次就本轮停用", "找源与覆盖", "int", False,
     "某引擎被反爬打死后,继续拿剩下的词去撞只是白耗时间(实测百度连错 148 次)。"
     "连续失败到此值就本轮不再用它,下一轮或引擎自检时自动恢复。默认 8;0=从不停用"),
    ("prospect_delay_seconds", "主动找源:两次搜索之间等几秒", "找源与覆盖", "float", False,
     "不留间隔会被搜狗/百度很快拦死(实测各只跑了 1 页和 3 页)。连错时按 2 倍退避、封顶 30 秒。"
     "默认 1.5;调大更稳但整轮更慢,0=不等"),
    ("prospect_exclude_after_hits", "主动找源:已有域霸榜多少次后排除它", "找源与覆盖", "int", False,
     "一轮 941 条结果里 905 条来自已收录的站(其中 904 条只来自两个域),等于整轮空转。"
     "某个已有域命中到此值,后续找源词自动加 -site: 把它排掉,逼搜索引擎翻出第二梯队渠道。"
     "默认 25;0=不排除"),
    ("prospect_exclude_max_sites", "主动找源:最多排除几个域", "找源与覆盖", "int", False,
     "排除词会占查询长度,排太多反而压召回。默认 6"),
    ("prospect_column_hint_max", "已有源站点:每站最多补几个栏目", "找源与覆盖", "int", False,
     "站点已有源≠该栏目已在采。找源命中的页面会反推出该站漏采的栏目并落库,每站上限。默认 8;0=关闭"),
    ("discovery_probe_pass", "主动找源:初评达标即可入库", "找源与覆盖", "float", False,
     "主动找源是单通道,过不了『需≥2 个发现通道』的闸门,找到的渠道会一直卡在候选池。"
     "LLM 初评相关度≥此值即视为第二重证据放行(0-1)。默认 0.7;设 0=关闭这条通路、维持严格闸门"),
    ("prospect_query_cap", "主动找源:单轮检索词上限", "找源与覆盖", "int", False,
     "固定短词 + 覆盖空白方向词 + 配方组合词,合计最多跑这么多条。默认 150。"
     "注意进度条分母 = 词数 × 引擎数(如 150 词 × 2 引擎 = 300),调大前先做「找源路径自检」"
     "把不可用的引擎去掉,否则只是成倍浪费请求"),
    ("probe_llm_enabled", "候选源 LLM 相关度初评", "找源与覆盖", "bool", False,
     "抓候选站首页抽样标题让模型判『是否持续产出安全事件内容』(0-1)。"
     "候选评分公式里这项权重最高之一,关掉则该项恒为 0、排序只看被提及次数。默认开"),
    ("probe_sample_titles", "初评:抽样标题条数", "找源与覆盖", "int", False, "默认 12"),
    ("probe_max_per_round", "初评:单轮最多评几个候选", "找源与覆盖", "int", False,
     "每个候选一次 LLM 调用,按此值封顶控成本。默认 20"),
    ("probe_ttl_days", "初评结果复用天数", "找源与覆盖", "int", False,
     "初评结果在这么多天内直接复用不重评。默认 30"),
    ("coverage_window_days", "覆盖度统计窗口(天)", "找源与覆盖", "int", False,
     "按行业统计近这么多天的事件数以判断覆盖空白。默认 90"),
    ("coverage_min_events", "覆盖度:低于几条算空白", "找源与覆盖", "int", False,
     "某行业在统计窗口内事件数低于此值即判为『覆盖空白』,自动生成该行业的找源方向词。默认 3"),

    ("simhash_hamming_max", "同稿去重阈值", "去重", "int", False, "SimHash 海明距离≤此值判为转载;越大越激进,默认 3"),
    ("dedup_lookback_days", "同稿去重回溯天数", "去重", "int", False, "只与近这么多天的文档比对同稿,默认 30"),
    ("dedup_len_ratio_min", "同稿正文长度相近比值", "去重", "float", False,
     "缺标题时,两篇正文长度比值≥此值才可能判同稿(0-1),默认 0.6"),
    ("semantic_recall_threshold", "语义去重阈值", "去重", "float", False, "事件摘要余弦相似度≥此值判疑似同事件,0-1,默认 0.88"),
    ("fingerprint_window_days", "事件去重时间窗(天)", "去重", "int", False, "同单位同类型事件在此天数内视为同一事件,默认 14"),

    ("archive_max_assets", "单页最多存图/附件数", "存档", "int", False, "完整存档单页下载图片与附件上限,默认 50"),

    ("daily_auto_enabled", "启用每日自动采集", "每日自动化", "bool", False,
     "开启后进程内到点自动跑一轮采集并出日报,无需人工点。仅应用运行时生效(单机部署适用)"),
    ("daily_auto_hour", "每日自动采集时点(UTC)", "每日自动化", "int", False,
     "每天几点(UTC 24 小时制)自动跑。默认 1 = 北京时间约 9:00。改这里即改触发时间"),
    ("daily_auto_limit_sources", "每日自动采集源数上限", "每日自动化", "int", False,
     "自动跑时抓多少个源。默认 999 = 全量。源多时可调小控制时长"),
    ("digest_email_to", "日报收件邮箱", "每日自动化", "str", False,
     "逗号分隔多个收件人。配了 SMTP 且填了收件人才会推送邮件;否则日报仅页面查看/下载"),
    ("smtp_host", "SMTP 服务器", "每日自动化", "str", False, "如 smtp.exmail.qq.com;留空则不发邮件"),
    ("smtp_port", "SMTP 端口", "每日自动化", "int", False, "465=SSL(默认)、587=STARTTLS"),
    ("smtp_user", "SMTP 账号", "每日自动化", "str", False, "登录邮箱账号"),
    ("smtp_password", "SMTP 密码/授权码", "每日自动化", "str", True, "邮箱授权码;留空表示不修改已存值"),
    ("smtp_from", "发件人地址", "每日自动化", "str", False, "留空则用 SMTP 账号"),
]
_META = {k: (label, group, typ, secret, desc) for k, label, group, typ, secret, desc in EDITABLE}
_KEYS = set(_META)


def _cast(key: str, raw):
    typ = _META[key][2]
    if typ == "bool":
        return str(raw).lower() in ("1", "true", "yes", "on", "是")
    if typ == "int":
        return int(raw)
    if typ == "float":
        return float(raw)
    return str(raw)


def current() -> dict:
    """当前配置(分组;密钥脱敏),供前端渲染表单。"""
    groups: dict[str, list] = {}
    for key, label, group, typ, secret, desc in EDITABLE:
        val = getattr(settings, key, "")
        display = ("***已配置***" if val else "") if secret else val
        if typ == "bool":
            display = bool(val)
        groups.setdefault(group, []).append({
            "key": key, "label": label, "type": typ, "secret": secret,
            "value": display, "desc": desc,
        })
    return {"groups": [{"name": g, "fields": f} for g, f in groups.items()]}


def save(db: Session, updates: dict) -> list[str]:
    """保存改动:写 DB + 即时应用到 settings。返回实际生效的 key 列表。"""
    applied = []
    for key, raw in (updates or {}).items():
        if key not in _KEYS:
            continue
        secret = _META[key][3]
        # 密钥留空 = 不修改;其余空字符串照常写入(允许清空)
        if secret and (raw is None or str(raw).strip() == ""):
            continue
        try:
            val = _cast(key, raw)
        except (ValueError, TypeError):
            continue
        setattr(settings, key, val)
        row = db.get(AppSetting, key)
        if row:
            row.value = str(val)
        else:
            db.add(AppSetting(key=key, value=str(val)))
        applied.append(key)
    db.flush()
    # LLM 相关改动 → 重置客户端缓存,下次调用用新配置
    if any(k.startswith("llm_") for k in applied):
        from app.services import llm
        llm.reset()
    return applied


def load_from_db(db: Session):
    """启动时把 DB 中的持久化配置覆盖到 settings 实例。"""
    for row in db.query(AppSetting).all():
        if row.key in _KEYS:
            try:
                setattr(settings, row.key, _cast(row.key, row.value))
            except (ValueError, TypeError):
                pass


def test_llm(timeout: float = 15) -> dict:
    """用当前已生效配置实测大模型连通:聊天接口 + 向量接口各调一次,快速失败。"""
    from app.services.llm import OpenAICompatLLM

    if settings.llm_provider != "openai_compat" or not settings.llm_base_url:
        return {"provider": "mock", "ok": None,
                "note": "当前为 mock 离线模式,未连真实大模型。要联网抽取请把「LLM 模式」设为 "
                        "openai_compat 并填接口地址/密钥/模型,再测试。"}

    client = OpenAICompatLLM(
        settings.llm_base_url, settings.llm_api_key, settings.llm_model,
        settings.llm_embed_base_url, settings.llm_embed_api_key, settings.llm_embed_model,
        timeout=timeout,
    )
    res = {"provider": "openai_compat", "base_url": settings.llm_base_url,
           "model": settings.llm_model or "(未填模型名)"}
    # 聊天接口(抽取/粗筛用)
    if not settings.llm_model:
        res["chat_ok"], res["chat_detail"] = False, "未填「抽取模型」名称"
    else:
        try:
            out = client.complete_json("你是连通性自检助手,只输出 JSON。",
                                       '返回 {"ok": true}', retries=0)
            res["chat_ok"], res["chat_detail"] = True, f"正常,返回 {str(out)[:80]}"
        except Exception as e:  # noqa: BLE001
            res["chat_ok"], res["chat_detail"] = False, _friendly_err(e)
    # 向量接口(语义去重用,可选)
    if not (settings.llm_embed_model or settings.llm_embed_base_url):
        res["embed_ok"], res["embed_detail"] = None, "未配置向量模型 → 语义去重将禁用(不影响主流程)"
    else:
        try:
            v = client.embed("连通性测试")
            res["embed_ok"], res["embed_detail"] = True, f"正常,向量维度 {len(v)}"
        except Exception as e:  # noqa: BLE001
            res["embed_ok"], res["embed_detail"] = False, _embed_err_hint(e)
    res["ok"] = bool(res.get("chat_ok"))  # 主流程只看聊天接口;向量接口可选
    return res


def _embed_err_hint(e: Exception) -> str:
    return (_friendly_err(e) + " ｜ 向量接口仅用于语义去重(可选,失败不影响文章抽取)。"
            "如要启用,请把「向量模型」填成该厂商的 embedding 专用模型"
            "(MiniMax=embo-01、通义=text-embedding-v3),而非聊天模型名;不需要可留空禁用。")


def _friendly_err(e: Exception) -> str:
    s = str(e)
    if "ConnectError" in type(e).__name__ or "Connection" in s or "getaddrinfo" in s:
        return "连不上接口地址,检查「LLM 接口地址」是否正确、网络是否可达"
    if "timeout" in s.lower() or "Timeout" in type(e).__name__:
        return "接口超时,地址可达但响应太慢或被拦截"
    if "401" in s or "403" in s:
        return "认证失败(401/403),检查「LLM 密钥」是否正确"
    if "404" in s:
        return "接口路径 404,检查地址是否以 /v1 结尾、模型名是否正确"
    if "429" in s:
        return "被限流(429),稍后再试或检查额度"
    if "status_code" in s or "base_resp" in s:  # MiniMax 等 HTTP200 业务错误
        return "接口返回业务错误(密钥/模型名/额度之一有误):" + s[:160]
    if "无法解析向量" in s:
        return "向量接口返回格式不识别,确认「向量模型」名称正确、且该模型是 embedding 模型"
    return s[:200]
