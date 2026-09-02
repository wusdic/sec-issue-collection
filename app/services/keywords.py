"""关键词能力模块(通用):从画像的范围限定(scope)与静态词组自动生成检索词矩阵,并按配方组合成查询。

三种来源合成词组(group → terms):
- scope 派生:regions→region_terms、industries→industry_terms、topics→topic_terms、entities→entity_terms(含别名)、
  doc_types→doctype_terms;
- 画像 keywords.groups 静态词组(如 action_terms/event_terms);
- 库里的监控名单 WatchTarget(org→entity_terms、topic→topic_terms、region→region_terms、product→entity_terms);
- 可选:模型扩展同义/相关说法(keywords.expand_with_llm)。
组合配方 keywords.compose 缺省时用平台缺省配方(主体×主题、地域×主题、行业×主题、主题单独…)。
产物是与旧版 keyword_matrix_sec.yaml 同构的 KeywordSet.content,日常检索(expand_queries)、找源配方
(discovery_recipes)、栏目相关词都能直接用。也可单独调用:python -m app.cli keywords-generate --need X。
"""
from __future__ import annotations

from datetime import datetime

from app.services import need_ctx
from app.services.need_ctx import SCOPE_GROUP, SCOPE_KINDS

_WATCH_GROUP = {"org": "entity_terms", "product": "entity_terms", "topic": "topic_terms",
                "region": "region_terms", "attacker_group": "entity_terms", "industry": "industry_terms"}
# 组合时不单独跑的词组(地域/主体单独搜太泛)
_NO_SOLO = {"region_terms", "entity_terms", "industry_terms"}
# 缺省配方里"主体/地域/行业"要配的对象词组,按优先级
_OBJECT_GROUPS = ("topic_terms", "event_terms", "action_terms", "doctype_terms", "industry_terms")


def _uniq(xs) -> list[str]:
    seen, out = set(), []
    for x in xs:
        x = " ".join(str(x or "").split())
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def scope_groups(ctx, db=None) -> dict[str, list[str]]:
    """scope 五维 + 监控名单 → 词组。"""
    groups: dict[str, list[str]] = {}
    for kind in SCOPE_KINDS:
        terms = ctx.scope_terms(kind)
        if terms:
            groups[SCOPE_GROUP[kind]] = _uniq(terms)
    if db is not None:
        try:
            from app.models import WatchTarget
            for w in db.query(WatchTarget).filter_by(need_id=ctx.id, active=True).all():
                g = _WATCH_GROUP.get(w.kind, "entity_terms")
                groups[g] = _uniq(list(groups.get(g) or []) + [w.value] + list(w.aliases or []))
        except Exception:  # noqa: BLE001 名单读不到不影响生成
            pass
    return groups


def static_groups(ctx) -> dict[str, list[str]]:
    out = {}
    for g, terms in (ctx.keywords_cfg.get("groups") or {}).items():
        if isinstance(terms, list) and terms:
            out[str(g)] = _uniq(terms)
    return out


def term_groups(ctx, db=None, expand: bool | None = None) -> dict[str, list[str]]:
    """合成词组;expand=True(或画像 expand_with_llm)时让模型补同义/相关说法。"""
    groups = scope_groups(ctx, db)
    for g, terms in static_groups(ctx).items():
        groups[g] = _uniq(list(groups.get(g) or []) + terms)
    if expand is None:
        expand = bool(ctx.keywords_cfg.get("expand_with_llm"))
    if expand:
        for g in list(groups):
            groups[g] = _uniq(groups[g] + expand_terms(ctx, g, groups[g]))
    return groups


def expand_terms(ctx, group: str, terms: list[str]) -> list[str]:
    """模型扩展:给每个词补 N 个同义/相关检索说法(2-10 字)。模型不可用/离线 → 空。"""
    if not terms:
        return []
    per = int(ctx.keywords_cfg.get("expand_per_term") or 3)
    try:
        from app.services.llm import get_llm
        from app.services.prompts import expand_terms_prompts
        system, user = expand_terms_prompts(ctx, group, terms[:60], per)
        r = get_llm().complete_json(system, user) or {}
        out = []
        for w in r.get("terms") or []:
            w = " ".join(str(w).split())
            if 2 <= len(w) <= 12:
                out.append(w)
        return out[: per * len(terms)]
    except Exception:  # noqa: BLE001
        return []


def default_recipes(groups: dict[str, list[str]]) -> list[dict]:
    """平台缺省配方(与领域无关):
    ① 有锚点词组(主体/地域/行业)时,锚点 × 对象词组(主题/事件/动作/文种)优先——锚点单独搜太泛;
    ② 没有锚点时,对象词组单独跑;
    ③ 既无对象词组,锚点之间两两组合(至少给出两词组合)。"""
    recipes: list[dict] = []
    objs = [g for g in _OBJECT_GROUPS if groups.get(g)]
    anchors = [g for g in ("entity_terms", "region_terms", "industry_terms") if groups.get(g)]
    for anchor in anchors:
        for o in objs:
            if o != anchor:
                recipes.append({"groups": [anchor, o], "template": "{0} {1}"})
    if not anchors:
        for g in groups:
            if g not in _NO_SOLO and groups.get(g):
                recipes.append({"groups": [g]})
    if not objs:
        for i in range(len(anchors)):
            for j in range(len(anchors)):
                if i != j:
                    recipes.append({"groups": [anchors[i], anchors[j]], "template": "{0} {1}"})
    if not recipes:                      # 只有一个词组:只能单独跑
        recipes = [{"groups": [g]} for g in groups if groups.get(g)]
    return recipes


def compose(groups: dict[str, list[str]], recipes: list[dict] | None, budget: int = 0) -> list[str]:
    """按配方把词组组合成查询;去重保序;budget>0 截断。"""
    recipes = recipes or default_recipes(groups)
    out: list[str] = []
    for r in recipes:
        gs = [str(g) for g in (r.get("groups") or []) if groups.get(str(g))]
        if not gs or len(gs) != len(r.get("groups") or []):
            continue
        limits = list(r.get("limits") or [])
        lists = [groups[g][: int(limits[i])] if i < len(limits) and limits[i] else groups[g]
                 for i, g in enumerate(gs)]
        tpl = str(r.get("template") or " ".join("{%d}" % i for i in range(len(gs))))
        if len(gs) == 1:
            out += [tpl.format(t) for t in lists[0]]
        elif len(gs) == 2:
            # 对角轮转:截断时两个维度覆盖都均匀
            a, b = lists
            for k in range(len(b)):
                for i, x in enumerate(a):
                    out.append(tpl.format(x, b[(i + k) % len(b)]))
        else:
            import itertools
            for combo in itertools.product(*lists):
                out.append(tpl.format(*combo))
    out = _uniq(out)
    return out[:budget] if budget > 0 else out


def _legacy_expand(content: dict) -> list[str]:
    """旧版矩阵(event/industry/consequence/org 四组 + cross_* 深度)的展开,保持原行为。"""
    events = content.get("event_terms") or []
    industries = content.get("industry_terms") or []
    consequences = content.get("consequence_terms") or []
    orgs = content.get("org_terms") or []
    ce = int(content.get("cross_event_terms", 12))
    ci = int(content.get("cross_industry_terms", 20))
    cc = int(content.get("cross_consequence_terms", 12))
    co = int(content.get("cross_org_terms", 5))
    queries = list(events)
    queries += [f"{i} {e}" for e in events[:ce] for i in industries[:ci]]
    queries += [f"{o} {c}" for c in consequences[:cc] for o in orgs[:co]]
    return _uniq(queries)


def content_groups(content: dict) -> dict[str, list[str]]:
    return {k: _uniq(v) for k, v in (content or {}).items()
            if k.endswith("_terms") and isinstance(v, list) and v and k not in ("negative_terms", "english_terms", "procurement_terms")}


def expand_queries(content: dict, ctx=None) -> list[str]:
    """矩阵 → 查询列表(B1 日常定题)。有 compose 配方按配方;否则旧版四组交叉;再否则缺省配方。"""
    content = content or {}
    budget = int(content.get("query_budget_per_source_daily", 200) or 0)
    recipes = content.get("compose")
    groups = content_groups(content)
    if recipes:
        queries = compose(groups, recipes, 0)
    elif any(k in content for k in ("cross_event_terms", "consequence_terms", "org_terms")) or \
            (content.get("event_terms") and content.get("industry_terms") and not recipes):
        queries = _legacy_expand(content)
    else:
        queries = compose(groups, None, 0)
    return queries[:budget] if budget > 0 else queries


def build_matrix(db, ctx, expand: bool | None = None) -> dict:
    """生成与 keyword_matrix_sec.yaml 同构的矩阵内容。"""
    kc = ctx.keywords_cfg
    groups = term_groups(ctx, db, expand)
    recipes = list(kc.get("compose") or []) or default_recipes(groups)
    content = {
        "version": "auto-" + datetime.utcnow().strftime("%Y%m%d%H%M%S"),
        "generated_by": "keywords.generate", "need_id": ctx.id,
        "time_filters": list(kc.get("time_filters") or []),
        "query_budget_per_source_daily": int(kc.get("query_budget_per_source_daily") or 200),
        "max_pages_per_query": int(kc.get("max_pages_per_query") or 3),
        "negative_terms": _uniq(kc.get("negative_terms") or []),
        "compose": recipes,
    }
    content.update(groups)
    content["preview"] = compose(groups, recipes, 30)
    return content


def generate(db, ctx, expand: bool | None = None, persist: bool = True):
    """生成矩阵并(可选)存为生效的 KeywordSet。返回 (content, KeywordSet|None)。"""
    content = build_matrix(db, ctx, expand)
    ks = None
    if persist and db is not None:
        from app.models import KeywordSet
        db.query(KeywordSet).filter_by(need_id=ctx.id).update({"is_active": False})
        ks = KeywordSet(need_id=ctx.id, version=content["version"], content=content, is_active=True)
        ks.published_at = datetime.utcnow()
        db.add(ks)
        db.flush()
    return content, ks


def discovery_recipes(ctx) -> dict:
    """画像没给找源配方文件时,按 scope/keywords 生成:找源词 = 组合查询前 N 条;配方词组按角色归位。"""
    groups = term_groups(ctx, None, False)
    objs = [g for g in ("topic_terms", "event_terms", "doctype_terms") if groups.get(g)]
    subject = _uniq(sum((groups.get(g) or [] for g in ("entity_terms", "industry_terms", "region_terms")), []))
    event = _uniq(sum((groups.get(g) or [] for g in objs), []))
    action = _uniq(groups.get("action_terms") or [])
    return {
        "source_search_queries": compose(groups, None, 30),
        "query_recipes": {"subject_terms": subject[:30], "action_terms": action[:20],
                          "event_terms": event[:30], "channel_terms": [], "max_combos": 60},
    }


def search_queries_for(ctx) -> list[str]:
    """找源词:画像配方文件里的 source_search_queries;没有文件 → 按 scope/keywords 生成。"""
    qs = ctx.source_search_queries
    return qs if qs else discovery_recipes(ctx)["source_search_queries"]


def recipes_for(ctx) -> dict:
    """找源组合配方:画像配方文件里的 query_recipes;没有文件 → 按 scope/keywords 生成。"""
    r = ctx.query_recipes
    return r if r else discovery_recipes(ctx)["query_recipes"]


def selftest_query_for(ctx) -> str:
    """引擎自检词:画像显式声明 > 找源词第一条 > 需求名。"""
    explicit = (ctx.sources_cfg.get("prospect") or {}).get("selftest_query")
    if explicit:
        return str(explicit)
    qs = search_queries_for(ctx)
    return qs[0] if qs else ctx.name
