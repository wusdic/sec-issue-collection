"""公众号数据源处理 —— 通用能力 reputation 的一个实例(sec_events 专用便捷封装)。

核心逻辑已泛化到 app/services/reputation.py(任何需求可复用:政策库按发布机关定级、
招标库按采购平台定级…)。本模块保留公众号语义的便捷入口与向后兼容 API。
默认策略:公众号一律保留并按主体定级,黑名单仅用于真正的垃圾号(默认空)。
"""
from app.config import settings
from app.services import reputation

_WECHAT_REGISTRY = settings.config_dir / "wechat_accounts.yaml"


def _reg() -> dict:
    return reputation.load_registry(_WECHAT_REGISTRY)


def reload_accounts():
    reputation.reload()


def normalize_account(name):
    return reputation.normalize_subject(name)


def is_blacklisted(account):
    return reputation.is_blacklisted(_reg(), account)


def account_credibility(account, channel_default="S4"):
    return reputation.subject_credibility(_reg(), account, channel_default)


def detect_repost(text):
    r = reputation.detect_repost(text)
    # 兼容旧字段名 original_account
    r["original_account"] = r.get("original_subject")
    return r


def is_wechat_source(source, doc_publisher=None):
    if source is not None and getattr(source, "adapter", "") in ("sogou_wechat",):
        return True
    return bool(doc_publisher) and normalize_account(doc_publisher) in _reg()["subjects"]
