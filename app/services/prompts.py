"""提示词库:粗筛 / 结构化抽取 / 源相关度 / 列表模板生成。

与需求画像绑定:screen_prompt 要点与词表由画像注入,prompt 版本随词表版本走。
"""
import json


def screen_prompts(profile_cfg: dict, title: str, text: str) -> tuple[str, str]:
    goal = (profile_cfg.get("quality") or {}).get("screen_prompt") or "判断是否为国内发生的网络/信息安全事件报道"
    system = (
        "TASK=screen\n"
        "你是信息采集流水线的粗筛分类器。仅输出 JSON:"
        '{"is_candidate": bool, "confidence": 0-1, "reason": "一句话"}\n'
        f"判定标准:{goal}。"
        "漏洞预警(无受害方)、营销软文、行业趋势文章一律 false。"
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
