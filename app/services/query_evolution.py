"""找源关键词的进化机制。

问题:找源词一直是一份静态清单,同一批词每周重复跑,谁也不知道哪条词真的带回过渠道。
「零售 数据泄露」这种加了限定反而把召回压死的词(搜索引擎对多词按 AND 收紧),和
「数据泄露」这种有效词,在系统里长得完全一样。词表既不会自己变好,也不会自己变宽。

机制分四步,每一步都只用可观测的事实,不靠拍脑袋:

1) 记账 —— 每条词每轮的产出落 SearchQueryStat:原始结果、去掉页脚/大平台/已有源后
   仍有效的结果、带回的新渠道、其中最终进了源库的。价值按"越靠近目标权重越高"折算。

2) 增益 —— 2 词组合拆成 anchor(主锚点)+ modifier(限定词)。锚点单独也会跑(baseline
   配额),于是能算 lift = 组合每轮产出 / 锚点单独每轮产出。lift < 1 就是"加了这个限定词
   反而更差"。同一个限定词在多条组合上的 lift 取中位数,低于阈值就把这个词整个降级为
   weak——不再参与组合,但仍允许单独跑。这正是「零售」该走的路。

3) 变异 —— 从表现好的词派生新词:去掉限定词(drop)、换一个动作词(swap);再从已采到并
   被判为相关的文章标题里挖高频新词(harvest),让词表跟着真实语料长,而不是停在人写死的
   那几十个词上。可选再让 LLM 提一批(llm)。

4) 排期 —— 每轮的词表按四类配额混合:利用(高产词)、基线(锚点单独跑,增益的分母)、
   探索(没跑过的新词)、复活(歇着的词定期复测)。复活配额是刻意留的冗余:没有哪个网站
   天天都出一堆报道,一条词连着几轮空手不代表它没用,不能一棒子打死。
"""
import re
import statistics
from datetime import datetime, timedelta

from app.config import settings
from app.models import RawDocument, SearchQueryStat, TermStat

# 价值折算:越靠近"真的多了一个能用的源"权重越高。
# 原始结果条数权重压到很低——必应页脚那 900 条已经证明原始条数最容易被灌水。
W_ADMITTED, W_NEW_CHANNEL, W_USABLE = 5.0, 1.0, 0.02


def _cfg(key: str, default):
    return getattr(settings, key, default)


def value_of(s: SearchQueryStat) -> float:
    """这条词每轮的平均产出价值。"""
    if not s.runs:
        return 0.0
    return (W_ADMITTED * s.admitted + W_NEW_CHANNEL * s.new_channels
            + W_USABLE * s.usable) / s.runs


# ---------------- 记账 ----------------

def split_query(query: str) -> tuple[str | None, str | None, list[str]]:
    """把找源词拆成 (anchor, modifier, terms)。

    anchor 取更"本体"的那个词:事件/动作类词是检索锚点,主体类词是限定。拿不准时按
    出现顺序取第一个作 anchor —— 只要全局一致,增益比较就成立。
    """
    terms = [t for t in re.split(r"\s+", (query or "").strip()) if t]
    if not terms:
        return None, None, []
    if len(terms) == 1:
        return terms[0], None, terms
    return terms[0], terms[1], terms


def get_or_create(db, need_id: str, query: str, origin: str = "combo",
                  parent: str | None = None) -> SearchQueryStat:
    q = " ".join((query or "").split())
    row = db.query(SearchQueryStat).filter_by(need_id=need_id, query=q).one_or_none()
    if row:
        return row
    anchor, modifier, terms = split_query(q)
    row = SearchQueryStat(need_id=need_id, query=q, terms=terms, anchor=anchor,
                          modifier=modifier, origin=origin, parent=parent)
    db.add(row)
    db.flush()
    return row


def record_run(db, need_id: str, query: str, *, results: int = 0, usable: int = 0,
               new_channels: int = 0, origin: str = "combo") -> SearchQueryStat:
    """记一条词本轮的产出。admitted 是滞后信号,由 attribute_admitted 事后补。"""
    row = get_or_create(db, need_id, query, origin=origin)
    row.runs += 1
    row.results += results
    row.usable += usable
    row.new_channels += new_channels
    row.last_run_at = datetime.utcnow()
    row.barren_streak = 0 if new_channels else row.barren_streak + 1
    return row


def attribute_admitted(db, need_id: str, query: str, n: int = 1):
    """某条词带回的候选后来真的进了源库——最强的正反馈,单独回填。"""
    row = db.query(SearchQueryStat).filter_by(need_id=need_id, query=query).one_or_none()
    if row:
        row.admitted += n


# ---------------- 增益:这个限定词到底是帮忙还是帮倒忙 ----------------

def _solo_value(db, need_id: str, term: str) -> float | None:
    """某个词单独跑时的每轮产出(增益的分母)。没单独跑过就返回 None。"""
    row = (db.query(SearchQueryStat)
           .filter_by(need_id=need_id, query=term).one_or_none())
    if not row or row.runs < int(_cfg("query_min_runs", 2)):
        return None
    return value_of(row)


def compute_term_stats(db, need_id: str) -> dict:
    """算每个限定词的增益中位数,并把普遍拖后腿的词降级为 weak。

    只看单条组合容易被偶然性带偏(某周恰好没新闻),所以按词汇总、取中位数,
    且要求样本数达标才下结论——这是刻意留的冗余,宁可晚一轮判也不误杀。
    """
    min_runs = int(_cfg("query_min_runs", 2))
    min_samples = int(_cfg("term_min_samples", 3))
    weak_at = float(_cfg("term_weak_lift", 0.8))
    lifts: dict[str, list[float]] = {}
    for s in db.query(SearchQueryStat).filter_by(need_id=need_id).all():
        if not s.modifier or s.runs < min_runs:
            continue
        base = _solo_value(db, need_id, s.anchor or "")
        if base is None or base <= 0:
            continue          # 锚点还没基线,或锚点自己也没产出,算不出增益
        lifts.setdefault(s.modifier, []).append(value_of(s) / base)
    out = {"evaluated": 0, "weak": [], "recovered": []}
    for term, xs in lifts.items():
        row = (db.query(TermStat)
               .filter_by(need_id=need_id, term=term, role="modifier").one_or_none())
        if not row:
            # state 必须显式给:列默认值要 flush 才写进对象,新建的行在这里读出来是 None,
            # 下面 state == "active" 的判断就永远不成立,弱词一个也标不出来
            row = TermStat(need_id=need_id, term=term, role="modifier", state="active")
            db.add(row)
        row.lift = round(statistics.median(xs), 3)
        row.samples = len(xs)
        row.solo_value = _solo_value(db, need_id, term) or 0.0
        row.updated_at = datetime.utcnow()
        out["evaluated"] += 1
        if row.samples < min_samples:
            continue          # 样本不够就不下结论
        if row.lift < weak_at and row.state == "active":
            row.state = "weak"
            row.note = (f"作限定词的增益中位数 {row.lift}(<{weak_at}),"
                        f"{row.samples} 条组合上都不如锚点单独搜;退出组合池,仍可单独跑")
            out["weak"].append({"term": term, "lift": row.lift, "samples": row.samples})
        elif row.lift >= 1.0 and row.state == "weak":
            row.state = "active"
            row.note = f"增益回升到 {row.lift},重新参与组合"
            out["recovered"].append({"term": term, "lift": row.lift})
    return out


def weak_terms(db, need_id: str) -> set[str]:
    return {r.term for r in db.query(TermStat)
            .filter_by(need_id=need_id, role="modifier", state="weak").all()}


# ---------------- 变异:从表现好的词派生新词 ----------------

_STOP = {"the", "and", "有限公司", "股份", "记者", "编辑", "来源", "责任编辑", "点击",
         "阅读", "原标题", "转载", "微信", "公众号", "扫码", "关注", "网站", "首页",
         "本文", "我们", "他们", "什么", "如何", "为什么", "怎么", "一个", "这个",
         "以及", "但是", "因此", "目前", "近日", "昨日", "今日", "上午", "下午"}
_CJK_RUN = re.compile(r"[一-鿿]{2,12}")
_CJK = re.compile(r"[一-鿿]+")


_HARVEST_SYS = (
    "你是中文安全资讯的检索词工程师。给你一批最近被判定为『相关』的文章标题,"
    "请挑出最适合拿去搜索引擎**找新渠道**的关键词。要求:"
    "①每个词 2-8 个汉字,是完整的说法(法规名/专项行动名/攻击手法名/监管动作名),"
    "不要输出半截词或滑窗碎片;②要有检索区分度,不要『通报』『公司』『近日』这类泛词;"
    "③不要人名、地名单独成词;④只输出 JSON:{\"terms\": [\"词1\", \"词2\"]}"
)


def _harvest_llm(titles: list[str], top_n: int) -> list[str]:
    """让模型从标题里抽检索词。没有分词库时这是最靠谱的一条路——n-gram 只会切出
    『某公司因』『公司因违』这种滑窗碎片,拿去搜索毫无意义。"""
    from app.services.llm import get_llm
    sample = titles[:120]
    r = get_llm().complete_json(
        _HARVEST_SYS,
        f"最多给我 {top_n} 个词。标题如下:\n" + "\n".join(f"- {t[:80]}" for t in sample))
    out = []
    for w in (r or {}).get("terms") or []:
        w = " ".join(str(w).split())
        if 2 <= len(w) <= 8 and _CJK.fullmatch(w):
            out.append(w)
    return out[:top_n]


def _harvest_ngram(titles: list[str], known: set[str], top_n: int) -> list[str]:
    """兜底:极大重复子串。只保留"再加一个字频次就下降"的那个长度,
    否则同一句话会切出一串互相嵌套的碎片。"""
    freq: dict[str, int] = {}
    for t in titles:
        seen = set()
        for run in _CJK_RUN.findall(t):
            for n in range(2, 9):
                for i in range(len(run) - n + 1):
                    w = run[i:i + n]
                    if w in seen or w in _STOP:
                        continue
                    seen.add(w)
                    freq[w] = freq.get(w, 0) + 1
    lo = max(3, len(titles) // 20)
    hi = len(titles) * 0.9
    # 极大性:存在同频的更长子串包含它 → 它只是碎片,丢掉
    maximal = {w for w, n in freq.items()
               if not any(len(w2) > len(w) and freq.get(w2) == n and w in w2 for w2 in freq)}
    cands = [(w, n) for w, n in freq.items()
             if w in maximal and lo <= n <= hi and 3 <= len(w) <= 8 and w not in known]
    cands.sort(key=lambda x: (-x[1], -len(x[0])))
    picked: list[str] = []
    for w, _n in cands:
        if any(w in p or p in w for p in picked):
            continue
        picked.append(w)
        if len(picked) >= top_n:
            break
    return picked


def harvest_terms(db, need_id: str, top_n: int = 8, days: int = 90) -> list[str]:
    """从已采到、且被判为相关的文章标题里挖新检索词。

    这是词表唯一真正"跟着环境长"的入口:人写死的那几十个词覆盖不到新出现的说法
    (新法规名、新专项行动名、新攻击手法名),而这些词恰恰是找到新渠道的钥匙。
    优先让模型抽(没有分词库,n-gram 只会切出滑窗碎片),模型不可用时退回极大子串统计。
    """
    since = datetime.utcnow() - timedelta(days=max(1, days))
    titles = [t for (t,) in db.query(RawDocument.title)
              .filter(RawDocument.need_id == need_id,
                      RawDocument.screen_status == "screened_in",
                      RawDocument.fetched_at >= since)
              .limit(3000).all() if t]
    if len(titles) < int(_cfg("harvest_min_titles", 30)):
        return []            # 语料太少,挖出来的都是噪声
    known = set(_all_pool_terms())
    words: list[str] = []
    try:
        words = _harvest_llm(titles, top_n)
    except Exception:  # noqa: BLE001 模型不可用就退回统计法,不该让整轮进化失败
        words = []
    if not words:
        words = _harvest_ngram(titles, known, top_n)
    return [w for w in words if w not in known][:top_n]


def _all_pool_terms() -> list[str]:
    from app.services import prospect
    r = prospect._recipes()
    out = []
    for k in ("subject_terms", "action_terms", "event_terms", "channel_terms"):
        out += [str(x).strip() for x in (r.get(k) or []) if str(x).strip()]
    return out


def mutate(db, need_id: str, per_kind: int = 6) -> dict:
    """从高产词派生候选新词:去掉限定词、换动作词、挖来的新词配锚点。"""
    from app.services import prospect
    min_runs = int(_cfg("query_min_runs", 2))
    rows = [s for s in db.query(SearchQueryStat).filter_by(need_id=need_id, state="active").all()
            if s.runs >= min_runs]
    rows.sort(key=value_of, reverse=True)
    good = [s for s in rows if value_of(s) > 0][:per_kind * 2]
    acts = [str(x).strip() for x in (prospect._recipes().get("action_terms") or [])
            if str(x).strip()]
    weak = weak_terms(db, need_id)
    proposals: list[tuple[str, str, str]] = []      # (query, origin, parent)
    # ① 去掉限定词:直接检验"这条组合是不是还不如锚点单独搜"
    for s in good:
        if s.modifier and s.anchor:
            proposals.append((s.anchor, "drop", s.query))
    # ② 换动作词:锚点保留,换一个同类动作,看能不能捞到另一批渠道
    for s in good[:per_kind]:
        if not s.anchor:
            continue
        for a in acts:
            if a != s.modifier and a not in weak:
                proposals.append((f"{s.anchor} {a}", "swap", s.query))
                break
    # ③ 语料里挖来的新词:先单独跑(拿基线),再与最高产的锚点组一条
    fresh = harvest_terms(db, need_id, top_n=per_kind)
    top_anchor = good[0].anchor if good else None
    for w in fresh:
        proposals.append((w, "harvest", None))
        if top_anchor and w != top_anchor:
            proposals.append((f"{top_anchor} {w}", "harvest", top_anchor))
    added = []
    for q, origin, parent in proposals:
        q = " ".join(q.split())
        if not q or len(q.split()) > 2:
            continue
        if db.query(SearchQueryStat).filter_by(need_id=need_id, query=q).first():
            continue
        get_or_create(db, need_id, q, origin=origin, parent=parent)
        added.append({"query": q, "origin": origin, "parent": parent})
    return {"added": added, "harvested": fresh}


# ---------------- 淘汰与休整 ----------------

def prune(db, need_id: str) -> dict:
    """把词分成三类:正常轮换 / 暂时歇着 / 确认无效。

    「歇着」不是判死:没有哪个网站天天都出一堆报道,一条词连着几轮空手很正常,
    所以只降到低频复测。真正退休的门槛很高——跑够多轮且一条有效结果都没出过。
    """
    rest_at = int(_cfg("query_rest_barren", 4))
    retire_runs = int(_cfg("query_retire_runs", 6))
    out = {"rested": [], "retired": [], "woke": []}
    for s in db.query(SearchQueryStat).filter_by(need_id=need_id).all():
        if s.state == "retired":
            continue
        if s.runs >= retire_runs and s.usable == 0 and s.new_channels == 0:
            s.state = "retired"
            s.note = f"跑了 {s.runs} 轮,一条有效结果都没出过"
            out["retired"].append(s.query)
        elif s.barren_streak >= rest_at and s.state == "active":
            s.state = "resting"
            s.note = f"连续 {s.barren_streak} 轮没带回新渠道,转低频复测(不是淘汰)"
            out["rested"].append(s.query)
        elif s.state == "resting" and s.barren_streak == 0:
            s.state = "active"
            s.note = "复测又带回了新渠道,恢复正常轮换"
            out["woke"].append(s.query)
    return out


# ---------------- 排期:本轮到底跑哪些词 ----------------

def plan(db, need_id: str, cap: int, seed: list[str] | None = None) -> list[str]:
    """按四类配额混出本轮词表。

    比例是刻意的:利用要占大头(已知有效的词别浪费),但必须留出基线和探索——
    没有基线就算不出增益,没有探索词表就永远长不出新东西。
    """
    cap = max(1, int(cap))
    r_base = float(_cfg("query_share_baseline", 0.2))
    r_expl = float(_cfg("query_share_explore", 0.25))
    r_rev = float(_cfg("query_share_revive", 0.1))
    n_base, n_expl = int(cap * r_base), int(cap * r_expl)
    n_rev = int(cap * r_rev)

    seed = seed or []
    for q in seed:
        get_or_create(db, need_id, q, origin="base")
    db.flush()
    # 原料池是按优先级给过来的(固定短词 → 覆盖空白方向词 → 通用组合),没跑过的词之间
    # 就按这个顺序定先后,别让"缺哪块补哪块"的方向词掉到字典序后面去
    order = {q: i for i, q in enumerate(seed)}

    def rank(s):
        return order.get(s.query, len(order) + 1)

    rows = db.query(SearchQueryStat).filter_by(need_id=need_id).all()
    by_state: dict[str, list] = {}
    for s in rows:
        by_state.setdefault(s.state, []).append(s)
    active = by_state.get("active", [])
    weak = weak_terms(db, need_id)

    def usable(s):     # 降级的限定词不再参与组合
        return not (s.modifier and s.modifier in weak)

    picked: list[str] = []

    def take(items, n):
        for s in items:
            if n <= 0:
                break
            if s.query not in picked:
                picked.append(s.query)
                n -= 1

    # ① 基线:被用作锚点的单词必须单独跑,否则增益没有分母,整套机制就瞎了
    anchors = {s.anchor for s in active if s.modifier and s.anchor}
    take(sorted([s for s in active if not s.modifier and s.anchor in anchors],
                key=lambda s: (s.runs, rank(s))), n_base)
    # ② 探索:没跑过的新词优先(变异/挖出来的都在这里)
    take(sorted([s for s in active if s.runs == 0 and usable(s)], key=rank), n_expl)
    # ③ 复活:歇着的词按"最久没跑"轮换复测——留的就是这份冗余
    take(sorted(by_state.get("resting", []),
                key=lambda s: (s.last_run_at or datetime.min, rank(s))), n_rev)
    # ④ 利用:剩下的名额全给已知高产的词
    take(sorted([s for s in active if usable(s)],
                key=lambda s: (-value_of(s), s.runs, rank(s))), cap - len(picked))
    # ⑤ 名额还没填满(库还空)——拿传进来的种子词兜底。
    # 兜底同样要守住淘汰结果:退休的词、含弱限定词的组合,不能从这里溜回去
    by_q = {s.query: s for s in rows}
    for q in seed:
        if len(picked) >= cap:
            break
        if q in picked:
            continue
        s = by_q.get(q)
        if s and (s.state == "retired" or not usable(s)):
            continue
        picked.append(q)
    return picked[:cap]


def evolve(db, need_id: str) -> dict:
    """一轮完整进化:算增益 → 淘汰/休整 → 派生新词。由自动运维按周期调用。"""
    terms = compute_term_stats(db, need_id)
    pruned = prune(db, need_id)
    grown = mutate(db, need_id)
    return {"terms": terms, "prune": pruned, "mutate": grown}


def report(db, need_id: str, top: int = 12) -> dict:
    """给页面看的:哪些词在干活、哪些词在拖后腿、词表正在怎么长。"""
    rows = db.query(SearchQueryStat).filter_by(need_id=need_id).all()
    ran = [s for s in rows if s.runs]
    ran.sort(key=value_of, reverse=True)

    def row(s):
        return {"query": s.query, "origin": s.origin, "state": s.state, "runs": s.runs,
                "results": s.results, "usable": s.usable, "new_channels": s.new_channels,
                "admitted": s.admitted, "value": round(value_of(s), 3),
                "barren_streak": s.barren_streak, "note": s.note}

    ts = db.query(TermStat).filter_by(need_id=need_id, role="modifier").all()
    ts.sort(key=lambda t: t.lift)
    return {
        "total": len(rows), "ran": len(ran),
        "untried": sum(1 for s in rows if not s.runs),
        "by_state": {k: sum(1 for s in rows if s.state == k)
                     for k in ("active", "resting", "retired")},
        "top": [row(s) for s in ran[:top]],
        "bottom": [row(s) for s in ran[-top:] if value_of(s) <= 0][:top],
        "weak_terms": [{"term": t.term, "lift": t.lift, "samples": t.samples,
                        "solo_value": round(t.solo_value, 3), "note": t.note}
                       for t in ts if t.state == "weak"],
        "term_lifts": [{"term": t.term, "lift": t.lift, "samples": t.samples}
                       for t in ts[:top]],
    }
