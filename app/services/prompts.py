"""提示词库:粗筛 / 结构化抽取 / 源相关度 / 列表模板生成。

与需求画像绑定:screen_prompt 要点与词表由画像注入,prompt 版本随词表版本走。
"""
import json


def screen_prompts(profile_cfg: dict, title: str, text: str) -> tuple[str, str]:
    goal = (profile_cfg.get("quality") or {}).get("screen_prompt") or "判断是否为国内发生的网络/信息安全事件报道"
    system = (
        "TASK=screen\n"
        "你是网络安全事件库的粗筛分类器。仅输出 JSON:"
        '{"is_candidate": bool, "confidence": 0-1, "reason": "一句话"}\n'
        f"判定标准:{goal}。\n"
        "【算相关 is_candidate=true】仅限网络/数据/信息安全:安全事件(攻击/入侵/泄露/勒索/篡改/宕机/"
        "供应链投毒)、安全监管处罚决定、威胁情报(木马/僵尸网络/黑产/钓鱼/漏洞利用)、漏洞公告、"
        "安全风险提示预警、网络安全态势/统计通报。\n"
        "【必须判 false,不得放行】以下不属于网络安全事件库:\n"
        "· 网络内容治理/意识形态治理:清朗专项、集中整治、未成年人网络保护、团播直播乱象、AI内容乱象、"
        "短视频/账号名称/不良信息 整治处置;\n"
        "· 备案/评估/遴选名单:算法备案公告、服务安全评估『通过名单』、支撑单位遴选结果、认证机构名单;\n"
        "· 政策宣贯/解读/报告:专家解读、政策阐释、白皮书或报告发布;\n"
        "· 栏目/目录/列表导航页(无单篇正文)、会议/赛事/报名通知、招聘、营销、产品宣传。\n"
        "关键:『网络空间内容治理』≠『网络安全』,网信办发布≠安全事件;缺明确安全要素时一律判 false。"
    )
    user = f"标题:{title}\n正文:\n{text[:6000]}"
    return system, user


def extract_prompts(profile_cfg: dict, dictionaries: dict, record_schema: dict,
                    title: str, text: str) -> tuple[str, str]:
    dict_brief = {k: v for k, v in dictionaries.items() if k != "version"}
    system = (
        "TASK=extract\n"
        "你是结构化抽取器。把文章内容按给定 JSON Schema 抽取为一条记录,仅输出 JSON。\n"
        "硬规则(违反即废):\n"
        "1) 金额三态:文中出现『要求/索赔/勒索/拟处罚/预计/或将/传闻/主张』语境的金额,"
        "只能写入 claimed 通道;『判决/裁定/处罚决定书/公告确认/年报披露』语境的金额写入 confirmed,"
        "并在 note 注明依据语句。任何情况下不得臆造金额。\n"
        "2) 赎金『要求金额』写入 ransom.demanded_amount,绝不写入任何 loss_* 字段;"
        "只有明确『已支付』才填 paid_amount。\n"
        "3) 未披露的字段用 status='未披露' 或枚举『未披露/未知/不明』,不留空、不猜测。\n"
        "4) 每个关键抽取值在 _source_spans 里附原文片段(字段名→原文引句)。\n"
        "5) 另输出 record_type:有明确单一受害方/单起安全事件→『单一事件』;安全威胁情报、漏洞、"
        "风险提示、安全态势统计、明确安全处罚决定(无单一受害方)→『通报情报』;"
        "若本文属内容治理/意识形态治理(清朗/整治/未成年人保护等)、备案或评估『名单』、遴选结果、"
        "政策解读/报告发布、会议名单等非网络安全范畴→『不该入库』(此时其余字段可留空、不要强填)。\n"
        "6) occurred_date/disclosed_date 用 {\"date\":\"YYYY-MM-DD\",\"precision\":\"日|月|季|年|未知\"};"
        "只知年月填月末不知的用 precision 标注,不要另造 value 等字段;发布日只填 disclosed_date。\n"
        "7) consequences 只记『已确认发生』的后果;原文用『容易造成/可能/风险/预计/或将/不得』等表述的属"
        "潜在风险或预防警示,不得计入 consequences。约谈/预警类未确认发生的,不得填 被攻陷系统/确认攻击方/业务中断。\n"
        "8) 不得编造可核验字段:sources 的 url 必须用文中给出的真实链接(不确定就留空,禁止占位符如 XXXXX);"
        "法规依据只能引用正文出现过的法规名;机构缩写按原文(如 CNCERT)。sellable_mapping 仅当原文确有相关"
        "安全需求时才填,非安全内容留空。\n"
        f"词表(枚举值必须取自词表):\n{json.dumps(dict_brief, ensure_ascii=False)[:4000]}\n"
        f"JSON Schema:\n{json.dumps(record_schema, ensure_ascii=False)[:6000]}"
    )
    user = f"标题:{title}\n正文:\n{text[:12000]}"
    return system, user


def relevance_prompts(need_name: str, sample_text: str) -> tuple[str, str]:
    system = (
        "TASK=relevance\n"
        f"评估一个信息渠道对需求『{need_name}』的价值,仅输出 JSON:"
        '{"score": 0-1, "reason": "一句话"}。'
        "看:是否持续产出相关的原创/一手内容。"
    )
    return system, f"该渠道最近内容样本:\n{sample_text[:6000]}"


def list_template_prompts(html: str) -> tuple[str, str]:
    system = (
        "TASK=list_template\n"
        "分析列表页 HTML,给出文章条目的解析模板,仅输出 JSON:"
        '{"item_selector": "CSS选择器", "title_from": "text|attr:x", "url_from": "href", "confidence": 0-1}'
    )
    return system, html[:15000]
