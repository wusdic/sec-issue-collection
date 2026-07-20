"""粗筛 + 结构化抽取(M3):LLM 产出 → schema 校验 → 金额三态守卫。"""
import json
from pathlib import Path

import jsonschema

from app.config import settings
from app.services.llm import get_llm
from app.services.money_guard import apply_guard
from app.services.prompts import extract_prompts, screen_prompts

_SCHEMA_CACHE: dict[str, dict] = {}


def load_record_schema(path: str | Path) -> dict:
    key = str(path)
    if key not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[key] = json.loads(Path(path).read_text(encoding="utf-8"))
    return _SCHEMA_CACHE[key]


def screen_document(profile_cfg: dict, title: str, text: str) -> dict:
    """粗筛:{'is_candidate': bool, 'confidence': float, 'reason': str}"""
    system, user = screen_prompts(profile_cfg, title or "", text or "")
    out = get_llm().complete_json(system, user)
    return {
        "is_candidate": bool(out.get("is_candidate")),
        "confidence": float(out.get("confidence") or 0),
        "reason": str(out.get("reason") or ""),
    }


# 系统信封字段:抽取阶段不由 LLM 产出,发布时由 events.full_record 注入
_ENVELOPE_FIELDS = {"event_id", "status", "review", "confidence_overall",
                    "completeness_score", "change_log", "sources"}


def validate_payload(payload: dict, record_schema: dict, strict: bool = False) -> list[str]:
    """schema 校验:strict=False(草稿)忽略系统信封字段,只报内容问题;strict=True(发布)全量。"""
    validator = jsonschema.Draft202012Validator(record_schema)
    errors = []
    clean = {k: v for k, v in payload.items() if not k.startswith("_")}
    for err in validator.iter_errors(clean):
        top = err.absolute_path[0] if err.absolute_path else None
        if not strict and top is None and err.validator == "required":
            missing = err.message.split("'")[1] if "'" in err.message else ""
            if missing in _ENVELOPE_FIELDS:
                continue  # 草稿阶段不报系统信封字段缺失
        errors.append(f"{'/'.join(str(p) for p in err.absolute_path) or '(root)'}: {err.message[:160]}")
    return errors


def extract_record(profile_cfg: dict, dictionaries: dict, record_schema: dict,
                   title: str, text: str) -> dict:
    """抽取 + 守卫。返回 {'payload', 'violations', 'schema_errors', 'guard_demoted'}"""
    system, user = extract_prompts(profile_cfg, dictionaries, record_schema, title or "", text or "")
    payload = get_llm().complete_json(system, user)
    guard = apply_guard(payload, full_text=text or "")
    schema_errors = validate_payload(guard.payload, record_schema, strict=False)
    return {
        "payload": guard.payload,
        "violations": guard.violations,
        "guard_demoted": guard.demoted_fields,
        "schema_errors": schema_errors,
    }


def completeness_score(payload: dict, min_fields: list[str] | None = None) -> float:
    """字段完备度:决策字段加权(损失/整改/责任权重高)。"""
    weights = {
        "loss_L1": 8, "loss_L2": 8, "loss_L3": 6, "loss_L4": 8, "loss_L5": 5, "loss_L6": 3,
        "ransom": 5, "downtime_hours": 5, "affected_users": 4, "affected_records": 4,
        "remediation_actions": 8, "accountability": 5, "regulatory_actions": 5,
        "root_cause": 5, "security_controls": 6, "entry_vector": 4,
        "org_name": 4, "industry": 3, "region": 2, "severity": 3, "sellable_mapping": 4,
    }
    total = sum(weights.values())
    got = 0.0
    for field, w in weights.items():
        v = payload.get(field)
        if v in (None, "", [], {}):
            continue
        if isinstance(v, dict):
            status = v.get("status") or v.get("level") or v.get("category")
            filled = any(val not in (None, "", [], {}) for k, val in v.items()
                         if k not in ("status",)) or (status and status not in ("未披露", "未知", "不明", "未定级"))
            got += w if filled else w * 0.3  # 明确"未披露"也有过程分
        elif isinstance(v, list):
            informative = [x for x in v if str(x) not in ("未披露", "未知", "不明")]
            got += w if informative else w * 0.3
        else:
            got += w
    return round(got / total * 100, 1)
