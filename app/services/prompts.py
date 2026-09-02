"""提示词库:粗筛 / 结构化抽取 / 源相关度 / 列表模板生成。

与需求画像绑定(通用平台):粗筛的判定目标、算相关/必须判否清单,抽取的领域规则、
record_type 取值,全部来自 NeedContext;引擎只保留与领域无关的"平台通用硬规则"
(不臆造、未披露用 status、附原文片段、日期精度、来源链接真实)。
提示词里嵌 NEED_ID/SCHEMA_FILE 标记,供离线 Mock 模型按画像规则产出。
"""
import json

from app.config import settings
from app.services import need_ctx


def _ctx(profile_cfg: dict | None, ctx=None):
    return ctx or need_ctx.from_config(profile_cfg or {})


def screen_prompts(profile_cfg: dict, title: str, text: str, ctx=None) -> tuple[str, str]:
    c = _ctx(profile_cfg, ctx)
    sc = c.screen
    goal = sc.get("goal") or f"判断这篇内容是否属于『{c.name}』要收集的目标记录"
    inc = [str(r) for r in (sc.get("include_rules") or []) if str(r).strip()]
    exc = [str(r) for r in (sc.get("exclude_rules") or []) if str(r).strip()]
    reminder = str(sc.get("reminder") or "").strip()
    system = (
        f"TASK=screen\nNEED_ID={c.id}\n"
        f"你是『{c.name}』的粗筛分类器。仅输出 JSON:"
        '{"is_candidate": bool, "relevance": 0-1, "confidence": 0-1, "reason": "一句话"}\n'
        f"字段含义(务必区分):relevance=这篇与『{c.name}』的相关程度(1=高度相关,0=完全无关);"
        "confidence=你对本次判断的把握程度。判为不相关时 relevance 必须给低分(而不是把把握程度写进去)。\n"
        f"判定标准:{goal}。\n"
    )
    if inc:
        system += "【算相关 is_candidate=true】\n" + "\n".join(f"· {r}" for r in inc) + "\n"
    if exc:
        system += "【必须判 false,不得放行】\n" + "\n".join(f"· {r}" for r in exc) + "\n"
    if reminder:
        system += f"关键:{reminder}\n"
    scope_lines = c.scope_summary()
    if scope_lines:
        system += "【范围限定】只收下列范围内的内容:\n" + "\n".join(f"· {x}" for x in scope_lines) + "\n"
        rm = c.require_mention
        if rm:
            from app.services.need_ctx import SCOPE_LABEL
            system += "标题或正文未提及上述『" + "/".join(SCOPE_LABEL[k] for k in rm) + "』中任何一个的,一律判 false。\n"
    user = f"标题:{title}\n正文:\n{text[:6000]}"
    return system, user


# 平台通用硬规则:与领域无关,任何需求都必须遵守;画像只能追加,不能删除
_PLATFORM_EXTRACT_RULES = [
    "未披露的字段用 status='未披露' 或枚举『未披露/未知/不明』,不留空、不猜测",
    "每个关键抽取值在 _source_spans 里附原文片段(字段名→原文引句)",
    "日期类字段若 Schema 定义为 {\"date\":\"YYYY-MM-DD\",\"precision\":\"日|月|季|年|未知\"} 结构,按该结构填;"
    "只知年月的用 precision 标注,不要另造字段;Schema 为纯日期字符串的直接填 YYYY-MM-DD",
    "不得编造可核验字段:sources 的 url 必须用文中给出的真实链接(不确定就留空,禁止占位符如 XXXXX);"
    "机构/主体名称按原文",
]


def extract_prompts(profile_cfg: dict, dictionaries: dict, record_schema: dict,
                    title: str, text: str, ctx=None) -> tuple[str, str]:
    c = _ctx(profile_cfg, ctx)
    dict_brief = {k: v for k, v in (dictionaries or {}).items() if k != "version"}
    rt = c.record_types
    rules = list(_PLATFORM_EXTRACT_RULES)
    rules.append(
        f"另输出 record_type,取值只能是 {rt.get('values')};"
        f"若本文不属于『{c.name}』的收集范畴,填『{rt.get('out_of_scope')}』(此时其余字段可留空、不要强填)")
    rules += c.extract_rules
    scope_lines = c.scope_summary()
    if scope_lines:
        rules.append("范围限定(主体/地域/分类取值优先对齐这些名单及其别名,别名统一写成正式名):" + ";".join(scope_lines))
    numbered = "\n".join(f"{i + 1}) {r}" for i, r in enumerate(rules))
    system = (
        f"TASK=extract\nNEED_ID={c.id}\nSCHEMA_FILE={c.schema_file or 'light'}\n"
        f"你是『{c.name}』的结构化抽取器。把文章内容按给定 JSON Schema 抽取为一条记录,仅输出 JSON。\n"
        f"硬规则(违反即废):\n{numbered}\n"
        # Schema 必须完整给出:$defs 里的结构(三态/日期精度等)截掉模型就看不到怎么写
        f"词表(枚举值必须取自词表):\n{json.dumps(dict_brief, ensure_ascii=False)[:settings.prompt_dict_chars]}\n"
        f"JSON Schema(务必完整遵守,含 $defs 引用的结构):\n"
        f"{json.dumps(record_schema, ensure_ascii=False)[:settings.prompt_schema_chars]}"
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


def expand_terms_prompts(ctx, group: str, terms: list[str], per: int) -> tuple[str, str]:
    """关键词扩展:给一组词补同义/相关检索说法(keywords 能力模块用)。"""
    system = (
        "TASK=expand_terms\n"
        f"你是『{ctx.name}』的检索词工程师。给你一组『{group}』类的词,请为每个词补 {per} 个"
        "中文搜索引擎里常见的同义/近义/口语/缩写说法(2-10 字,完整说法,不要半截词、不要重复原词)。"
        '只输出 JSON:{"terms": ["词1", "词2"]}'
    )
    return system, "词组:\n" + "\n".join(f"- {t}" for t in terms)
