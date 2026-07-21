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
    ("llm_embed_model", "向量模型", "LLM 大模型", "str", False, "语义去重用,如 text-embedding-v3;留空则禁用语义去重"),

    ("fetch_timeout", "抓取超时(秒)", "采集", "float", False, "单页抓取超时,默认 20"),
    ("crawl_delay_seconds", "抓取间隔(秒)", "采集", "float", False, "请求之间的礼貌延时,默认 2"),
    ("playwright_enabled", "启用浏览器渲染/截图", "采集", "bool", False, "动态页与整页截图需开启(需装 Playwright)"),

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
