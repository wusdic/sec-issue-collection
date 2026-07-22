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
     "增量采集:列表按时间倒序,连续遇到这么多条『已采过』就判定新内容抓全、停止翻页,不用跑完整站。默认 15"),
    ("discovery_auto_trial_threshold", "新源自动入库阈值", "采集", "float", False,
     "采集中出现的新域名累积证据评分≥此值就自动建为 trial 试运行源(仍 S4 待人工定级)。"
     "调低→自动入库更激进、新源更多但更杂;调高→更保守。默认 4.0"),

    ("simhash_hamming_max", "同稿去重阈值", "去重", "int", False, "SimHash 海明距离≤此值判为转载;越大越激进,默认 3"),
    ("semantic_recall_threshold", "语义去重阈值", "去重", "float", False, "事件摘要余弦相似度≥此值判疑似同事件,0-1,默认 0.88"),
    ("fingerprint_window_days", "事件去重时间窗(天)", "去重", "int", False, "同单位同类型事件在此天数内视为同一事件,默认 14"),

    ("archive_max_assets", "单页最多存图/附件数", "存档", "int", False, "完整存档单页下载图片与附件上限,默认 50"),
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
