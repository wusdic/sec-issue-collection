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


def count_seed_sources(path: str | Path) -> int:
    """内置种子源清单里一共写了多少个源(不看库里有没有)。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError:
        return 0
    return len(data.get("sources") or [])


def load_seed_sources(db: Session, need_id: str, path: str | Path) -> int:
    """种子源导入:按 (adapter, entry_url) 幂等 upsert,serves_needs 合并。返回新增条数。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    count = 0
    for s in data.get("sources", []):
        entry = s.get("entry_url")
        cfg0 = s.get("adapter_config", {}) or {}
        _sk0, ident0 = url_tools.source_keys(s["kind"], entry, cfg0)
        # 幂等键优先用采集目标键:公众号(mp:号名)/站内检索(site:域名)都没有 entry_url,
        # 只按 (adapter, entry_url) 去重会让同适配器的一批源互相覆盖——19 个公众号源只进得来 1 个。
        existing = None
        if ident0:
            existing = db.query(Source).filter_by(identity_key=ident0).one_or_none()
        if existing is None and entry:
            existing = (db.query(Source)
                        .filter_by(adapter=s["adapter"], entry_url=entry).one_or_none())
        if existing is None and not ident0 and not entry:
            # 既无目标键也无入口(如"多站汇总/定题检索"这类占位源):按 (适配器,名称) 幂等,
            # 否则每次载入种子都会再插一条重复的
            existing = (db.query(Source)
                        .filter_by(adapter=s["adapter"], name=s["name"]).one_or_none())
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


def need_paths(need_id: str | None = None) -> dict:
    """某需求的画像/词表/关键词/种子源/产品映射文件路径(全部由画像声明;缺省需求 = settings.default_need_id)。"""
    from app.services import need_ctx
    need_id = need_id or need_ctx.default_need_id()
    ctx = need_ctx.get(None, need_id)
    profile = None
    for f in need_ctx.profile_files():
        try:
            if ((yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("need") or {}).get("id") == need_id:
                profile = f
                break
        except (OSError, yaml.YAMLError):
            continue
    return {
        "profile": profile or (settings.config_dir / f"need_{need_id}.yaml"),
        "dictionaries": ctx.dictionaries_file,
        "keywords": ctx.discovery_terms_file,
        "sources": ctx.seed_file,
        "product_mapping": ctx.path(ctx.leads.get("mapping_file")),
    }


def all_profile_files() -> list[Path]:
    """config/need_*.yaml 里全部画像文件(不含模板)。"""
    from app.services import need_ctx
    return need_ctx.profile_files()


def setup_need(db: Session, need_id: str | None = None, activate: bool = True) -> dict:
    """按画像把一个需求装起来:注册画像 + 载词表 + 载关键词 + 载种子源(全部幂等;文件缺的跳过)。"""
    from app.services import need_ctx
    paths = need_paths(need_id)
    if not paths["profile"] or not Path(paths["profile"]).exists():
        raise ProfileError(f"找不到画像文件:{paths['profile']}")
    cfg = load_profile_file(paths["profile"])
    np = register_need(db, cfg, activate=activate)
    need_ctx.reset_cache()
    out = {"need_id": np.id, "name": np.name, "dictionaries": False, "keywords": False, "seed_sources": 0}
    if paths["dictionaries"] and Path(paths["dictionaries"]).exists():
        load_dictionaries(db, np.id, paths["dictionaries"])
        out["dictionaries"] = True
    if paths["keywords"] and Path(paths["keywords"]).exists():
        load_keyword_set(db, np.id, paths["keywords"])
        out["keywords"] = True
    else:
        # 没给关键词文件:按画像 scope/keywords 自动生成矩阵(auto_generate 可关)
        ctx = need_ctx.get(db, np.id)
        if ctx.keywords_cfg.get("auto_generate", True):
            from app.services import keywords
            content, _ks = keywords.generate(db, ctx, persist=True)
            out["keywords"] = bool(content.get("preview"))
            out["keywords_generated"] = len(content.get("preview") or [])
    if paths["sources"] and Path(paths["sources"]).exists():
        out["seed_sources"] = load_seed_sources(db, np.id, paths["sources"])
    db.flush()
    return out


def active_need_ids(db: Session) -> list[str]:
    return [n.id for n in db.query(NeedProfile).filter_by(active=True).all()]
