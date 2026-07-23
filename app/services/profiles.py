"""需求画像(need profile)加载与校验:框架的实例化入口。

上线校验(通用信息搜索框架 第 7 节):六要素齐备 + 覆盖基准声明,缺项拒绝激活。
"""
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DictionaryRelease, KeywordSet, NeedProfile, Source
from app.services import url_tools

REQUIRED_TOP_KEYS = ["need", "record_schemas", "dictionaries", "sources", "update", "quality", "outputs", "benchmark", "compliance"]
VALID_ARCHETYPES = {"事件型", "文档型", "对象型", "观测型"}
VALID_QUALITY_MODELS = {"事实核实型", "影响力评估型", "观点聚合型"}


class ProfileError(ValueError):
    pass


def validate_profile(cfg: dict) -> list[str]:
    errors = []
    for k in REQUIRED_TOP_KEYS:
        if k not in cfg or cfg[k] in (None, {}, []):
            errors.append(f"缺少必填要素: {k}")
    need = cfg.get("need", {})
    if not need.get("id"):
        errors.append("need.id 必填")
    for rs in cfg.get("record_schemas", []) or []:
        if rs.get("archetype") not in VALID_ARCHETYPES:
            errors.append(f"记录原型非法: {rs.get('archetype')}(须为 {VALID_ARCHETYPES})")
    q = cfg.get("quality", {})
    if q and q.get("model") not in VALID_QUALITY_MODELS:
        errors.append(f"质量模型非法: {q.get('model')}")
    bm = cfg.get("benchmark", {})
    if not (bm and bm.get("baselines")):
        errors.append("覆盖基准未声明(benchmark.baselines)——无基准不允许上线")
    comp = cfg.get("compliance", {})
    if not comp.get("collection_boundary"):
        errors.append("合规画像缺少采集边界(compliance.collection_boundary)")
    return errors


def load_profile_file(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def register_need(db: Session, cfg: dict, activate: bool = True) -> NeedProfile:
    errors = validate_profile(cfg)
    if errors and activate:
        raise ProfileError("画像校验失败: " + "; ".join(errors))
    need_id = cfg["need"]["id"]
    np = db.get(NeedProfile, need_id)
    if np is None:
        np = NeedProfile(id=need_id, name=cfg["need"].get("name", need_id), config=cfg, active=activate)
        db.add(np)
    else:
        np.name = cfg["need"].get("name", np.name)
        np.config = cfg
        np.active = activate
    db.flush()
    return np


def load_dictionaries(db: Session, need_id: str, path: str | Path) -> DictionaryRelease:
    with open(path, encoding="utf-8") as f:
        content = yaml.safe_load(f)
    version = str(content.get("version", "0"))
    existing = (
        db.query(DictionaryRelease)
        .filter_by(need_id=need_id, version=version)
        .one_or_none()
    )
    if existing:
        existing.content = content
        db.flush()
        return existing
    rel = DictionaryRelease(need_id=need_id, version=version, content=content)
    db.add(rel)
    db.flush()
    return rel


def load_keyword_set(db: Session, need_id: str, path: str | Path) -> KeywordSet:
    with open(path, encoding="utf-8") as f:
        content = yaml.safe_load(f)
    version = str(content.get("version", "0"))
    ks = db.query(KeywordSet).filter_by(need_id=need_id, version=version).one_or_none()
    if ks:
        ks.content = content
        ks.is_active = True
        db.flush()
        return ks
    db.query(KeywordSet).filter_by(need_id=need_id).update({"is_active": False})
    ks = KeywordSet(need_id=need_id, version=version, content=content, is_active=True)
    db.add(ks)
    db.flush()
    return ks


def load_seed_sources(db: Session, need_id: str, path: str | Path) -> int:
    """种子源导入:按 (adapter, entry_url) 幂等 upsert,serves_needs 合并。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    count = 0
    for s in data.get("sources", []):
        entry = s.get("entry_url")
        existing = (
            db.query(Source)
            .filter_by(adapter=s["adapter"], entry_url=entry)
            .one_or_none()
        )
        if existing:
            needs = set(existing.serves_needs or [])
            needs.add(need_id)
            existing.serves_needs = sorted(needs)
            if not existing.site_key:  # 旧数据补键
                sk, ik = url_tools.source_keys(existing.kind, existing.entry_url,
                                               existing.adapter_config)
                existing.site_key = sk
                if ik and not existing.identity_key and \
                        not db.query(Source).filter_by(identity_key=ik).first():
                    existing.identity_key = ik
            continue
        cfg = s.get("adapter_config", {})
        site_key, ident = url_tools.source_keys(s["kind"], entry, cfg)
        # identity_key 唯一:若目标键已被占用(极少),留空避免冲突,不影响该源采集
        if ident and db.query(Source).filter_by(identity_key=ident).first():
            ident = None
        db.add(Source(
            name=s["name"], entry_url=entry, kind=s["kind"], adapter=s["adapter"],
            adapter_config=cfg,
            credibility=s["credibility"], tier=s.get("tier", "B"),
            lifecycle="active", serves_needs=[need_id],
            identity_key=ident, site_key=site_key,
            manual_assist=bool(s.get("manual_assist", False)),
            note=s.get("note"), discovered_from="seed",
        ))
        count += 1
    db.flush()
    return count


def get_active_profile(db: Session, need_id: str) -> NeedProfile:
    np = db.get(NeedProfile, need_id)
    if np is None or not np.active:
        raise ProfileError(f"需求 {need_id} 不存在或未激活")
    return np


def get_active_dictionaries(db: Session, need_id: str) -> dict:
    rel = (
        db.query(DictionaryRelease)
        .filter_by(need_id=need_id)
        .order_by(DictionaryRelease.released_at.desc())
        .first()
    )
    return rel.content if rel else {}


def default_sec_events_paths() -> dict:
    c = settings.config_dir
    return {
        "profile": c / "need_sec_events.yaml",
        "dictionaries": settings.schema_dir / "dictionaries.yaml",
        "keywords": c / "keyword_matrix.yaml",
        "sources": c / "seed_sources.yaml",
        "product_mapping": c / "product_mapping.yaml",
    }
