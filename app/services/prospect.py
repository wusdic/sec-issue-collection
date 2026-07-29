"""主动找源(D5)+ 候选源 LLM 相关度初评。

此前的源发现是纯被动的:只有"已采到的文章引用过"的渠道才会进候选池——一个从没被
引用过的好渠道永远发现不了,这是覆盖面的天花板。这里补上主动的一路:

1) 用「找源专用检索词」(config/discovery.yaml 的 source_search_queries,加上覆盖度
   空白自动生成的词)去搜索引擎捞结果,把结果域名以 channel="source_search" 登记进
   同一个候选池——复用现成的评分/多通道闸门/黑名单,不另起炉灶;
2) 对候选域名做 LLM 相关度初评:抓其首页/列表页抽样标题,让模型判"是否持续产出国内
   安全事件相关内容",0-1 打分。这个分按 discovery.yaml 的 weight_llm_relevance 计入
   候选总分,让排序看内容而不只看被提及次数。结果按 probe_ttl_days 复用不重评。
"""
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse

import yaml

from app.config import settings
from app.db import SessionLocal
from app.models import Source, SourceBlacklist, SourceProbe
from app.services import discovery, fetcher, url_tools

_lock = threading.Lock()
_state: dict = {"running": False}
_cancel = threading.Event()


def status() -> dict:
    with _lock:
        return dict(_state)


def cancel():
    _cancel.set()


def _set(**kw):
    with _lock:
        _state.update(kw)


# ---------------- 找源检索词 ----------------

def base_queries() -> list[str]:
    """config/discovery.yaml 里人工维护的找源专用检索词。"""
    try:
        with open(settings.config_dir / "discovery.yaml", encoding="utf-8") as f:
            return [str(q).strip() for q in (yaml.safe_load(f) or {}).get("source_search_queries") or []
                    if str(q).strip()]
    except (OSError, yaml.YAMLError):
        return []


def build_queries(db, need_id: str) -> list[str]:
    """本轮要跑的找源词 = 人工维护的基础词 + 覆盖度空白自动生成的方向词。

    后者让"缺哪块就去找哪块的源"成为闭环,而不是每周重复搜同样的词。
    """
    qs = base_queries()
    try:
        from app.services import coverage
        qs += coverage.prospect_queries(db, need_id)
    except Exception:  # noqa: BLE001 覆盖度算不出来不该挡住找源
        pass
    seen, out = set(), []
    for q in qs:
        if q not in seen:
            seen.add(q)
            out.append(q)
    cap = int(getattr(settings, "prospect_query_cap", 0) or 0)
    return out[:cap] if cap > 0 else out


# ---------------- 搜索引擎(不依赖 Source 行) ----------------

class _Shim:
    """给适配器用的最小 source 替身:找源阶段还没有 Source 行。"""

    def __init__(self):
        # render=auto:搜索引擎对纯 httpx 常 403/返回验证页,开了浏览器渲染能救回来;
        # 没开渲染时 auto 自动降级为 httpx,不产生任何额外开销。
        self.adapter_config = {"render": "auto"}
        self.name = "prospect"
        self.entry_url = None
        self.kind = "query"


def _engines():
    from app.services.adapters import _REGISTRY
    out = []
    for name in str(getattr(settings, "prospect_engines", "") or "").split(","):
        name = name.strip()
        cls = _REGISTRY.get(name)
        if cls:
            out.append((name, cls(_Shim())))
    return out


_SKIP_HOSTS = {"baidu.com", "bing.com", "sogou.com", "google.com", "so.com", "zhihu.com",
               "weibo.com", "douyin.com", "bilibili.com", "csdn.net", "jianshu.com",
               "163.com", "qq.com", "sina.com.cn", "sohu.com", "toutiao.com", "baijiahao.baidu.com"}


def _resolve_redirect(url: str) -> str:
    """还原搜索引擎跳转链(C3)。

    百度结果链接是 www.baidu.com/link?url=…、必应是 bing.com/ck/a?…,直接取域名只会得到
    搜索引擎自己——这正是"搜索结果 0 条"的头号原因。跟一次跳转拿到真实站点。
    """
    try:
        fr = fetcher.fetch(url, timeout=min(10.0, float(settings.fetch_timeout)))
        return fr.final_url or url
    except Exception:  # noqa: BLE001
        return url


def _candidate_key(url: str) -> tuple[str | None, str]:
    """搜索结果 URL → (候选源标识键, 丢弃原因)。键为 None 时原因说明为什么没要它。"""
    if not url or not url.startswith("http"):
        return None, "bad_url"
    key = url_tools.identity_key_for(url)
    if not key:
        return None, "bad_url"
    if key in _SKIP_HOSTS:
        return None, "platform"          # 知乎/CSDN/门户等通用大平台,不作为专业源
    return key, ""


def run_once(db, need_id: str, on_progress=None) -> dict:
    """跑一轮主动找源。

    返回里带完整统计与人读结论:"0 条"必须说得出为什么(引擎被反爬 / 全是跳转链还原不了 /
    全是大平台 / 全是已有源),否则页面上一排 0 等于什么都没说。
    """
    engines = _engines()
    queries = build_queries(db, need_id)
    pages = max(1, int(getattr(settings, "prospect_pages_per_query", 1) or 1))
    resolve_cap = int(getattr(settings, "prospect_resolve_max", 60) or 0)
    st = {"pages": 0, "fetch_fail": 0, "raw_items": 0, "redirect": 0, "resolved": 0,
          "platform": 0, "bad_url": 0, "known_or_blocked": 0,
          "empty_pages": 0, "blocked_pages": 0}
    edetail: dict[str, dict] = {e[0]: {"engine": e[0], "pages": 0, "items": 0, "errors": 0}
                                for e in engines}
    new_keys, resolved_used = set(), 0
    total = max(1, len(queries) * max(1, len(engines)))
    done = 0
    for q in queries:
        for ename, eng in engines:
            if _cancel.is_set():
                break
            try:
                for page in range(pages):
                    items = eng.search_page(q, page)
                    if items is None:
                        st["fetch_fail"] += 1
                        edetail[ename]["errors"] += 1
                        break               # 抓不到(403/反爬/网络)
                    st["pages"] += 1
                    edetail[ename]["pages"] += 1
                    if not items:
                        # 抓到页面却一条没解析出来:分清"验证页/反爬"与"页面正常但没结果",
                        # 并留一段可见文本样本,便于定位到底返回了什么
                        st["empty_pages"] += 1
                        _diagnose_empty(eng, q, page, edetail[ename], st)
                        break
                    edetail[ename]["items"] += len(items)
                    for it in items:
                        st["raw_items"] += 1
                        url = it.url
                        if url_tools.is_search_redirect(url):
                            st["redirect"] += 1
                            if resolve_cap and resolved_used >= resolve_cap:
                                continue     # 还原有配额,超了就跳过(否则一轮几百次请求)
                            resolved_used += 1
                            url = _resolve_redirect(url)
                            if url_tools.is_search_redirect(url):
                                continue     # 还原失败(JS 跳转/需登录)
                            st["resolved"] += 1
                        key, why = _candidate_key(url)
                        if not key:
                            st[why if why in st else "bad_url"] += 1
                            continue
                        # 复用统一入口:黑名单/已注册/主体名校验/去重全都走同一套规则
                        k = discovery.record_evidence(db, url, "source_search",
                                                      display_name=(it.title or "")[:80])
                        if k:
                            new_keys.add(k)
                        else:
                            st["known_or_blocked"] += 1
            except Exception as e:  # noqa: BLE001 单条词失败不影响整轮
                edetail[ename]["errors"] += 1
                edetail[ename].setdefault("last_error", f"{type(e).__name__}: {e}"[:160])
            done += 1
            if on_progress:
                on_progress(done, total, f"{ename} · {q}")
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    out = {"queries": len(queries), "engines": [e[0] for e in engines],
           "hits": st["raw_items"], "new_keys": sorted(new_keys),
           "stats": st, "engine_detail": list(edetail.values())}
    out["note"] = explain(out)
    return out


def _diagnose_empty(eng, query: str, page: int, ed: dict, st: dict):
    """页面抓到了但 0 条结果:重抓一次看它到底返回了什么(验证页?还是结构变了?)。"""
    try:
        fr = fetcher.fetch(eng.build_url(query, page, None),
                           render=eng.config.get("render", False))
        if not fr.ok:
            return
        if hasattr(eng, "looks_blocked") and eng.looks_blocked(fr.html):
            st["blocked_pages"] += 1
            ed["blocked"] = ed.get("blocked", 0) + 1
            ed.setdefault("sample", "疑似验证页/反爬拦截")
            return
        text = fetcher._ANYTAG_RE.sub(" ", fetcher._TAG_RE.sub(" ", fr.html or ""))
        ed.setdefault("sample", " ".join(text.split())[:180] or "(页面无可见文本)")
    except Exception:  # noqa: BLE001 诊断本身失败不影响主流程
        pass


def explain(r: dict) -> str:
    """把统计翻译成一句人读结论——尤其是"为什么是 0"。"""
    st, q = r.get("stats") or {}, r.get("queries", 0)
    if not r.get("engines"):
        return "没有可用的搜索引擎:设置页「主动找源:用哪些搜索引擎」填的名字不在适配器列表里"
    if not q:
        return "本轮没有找源词:检查 config/discovery.yaml 的 source_search_queries 是否为空"
    if st.get("pages", 0) == 0:
        return (f"所有搜索引擎都抓不到内容({st.get('fetch_fail', 0)} 次失败,通常是 403/反爬/"
                "网络不通)。可在设置页换搜索引擎、或开启浏览器渲染后重试")
    if st.get("raw_items", 0) == 0:
        if st.get("blocked_pages"):
            return (f"搜索引擎返回的是验证页/反爬拦截({st['blocked_pages']} 页)。"
                    "开启设置页的「启用浏览器渲染/截图」通常可解决;或换用别的搜索引擎")
        return ("搜索页抓到了但一条结果都没解析出来。展开「各引擎明细」看返回内容样本:"
                "若是验证页请开浏览器渲染;若是正常页面则该引擎结构已变,"
                "可在设置页把「主动找源:用哪些搜索引擎」换成 bing_search 或 sogou_wechat 试试")
    if not r.get("new_keys"):
        parts = []
        if st.get("redirect", 0) and not st.get("resolved", 0):
            parts.append(f"{st['redirect']} 条是搜索引擎跳转链且还原失败")
        if st.get("platform"):
            parts.append(f"{st['platform']} 条落在通用大平台(知乎/CSDN/门户等,不作专业源)")
        if st.get("known_or_blocked"):
            parts.append(f"{st['known_or_blocked']} 条已是现有源或已拉黑")
        why = ";".join(parts) or "全部被去重/校验规则挡下"
        return f"抓到 {st.get('raw_items', 0)} 条结果,但没有新渠道:{why}"
    return (f"抓到 {st.get('raw_items', 0)} 条结果,还原跳转链 {st.get('resolved', 0)} 条,"
            f"新增候选渠道 {len(r['new_keys'])} 个")


# ---------------- 候选源 LLM 相关度初评 ----------------

_PROBE_SYS = (
    "你在评估一个网站/公众号是否值得作为『国内企业网络安全事件』的持续采集源。"
    "依据给出的站点名与最近文章标题样本,判断它是否持续产出与国内安全事件"
    "(数据泄露、勒索攻击、网络入侵、监管处罚通报、漏洞被利用等)相关的内容。\n"
    "注意:综合安全资讯站、监管机构通报栏目、行业安全媒体都算相关;"
    "纯技术教程、招聘、产品推广、境外纯技术研究、与安全无关的门户资讯算不相关。\n"
    '只输出 JSON:{"relevance": 0.0~1.0, "reason": "一句话理由"}'
)


def _sample_titles(key: str, limit: int) -> list[str]:
    """抓候选站首页,取正文链接文字作为最近文章标题样本。"""
    if key.startswith("mp:"):
        return []          # 公众号没有可直接抓的首页,交给搜狗渠道另行判断
    fr = fetcher.fetch(f"https://{key}/", render="auto")
    if not fr.ok:
        fr = fetcher.fetch(f"http://{key}/")
    if not fr.ok:
        return []
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(fr.html or "", "lxml")
    base_dom = url_tools.registered_domain(urlparse(fr.final_url or key).netloc)
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        t = a.get_text(" ", strip=True)
        if not (8 <= len(t) <= 80) or t in seen:
            continue
        href = a["href"]
        if href.startswith("http") and url_tools.registered_domain(urlparse(href).netloc) != base_dom:
            continue                                   # 只看本站内容,外链广告不算
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def probe_one(db, key: str, force: bool = False) -> SourceProbe | None:
    """对一个候选域名做 LLM 相关度初评(TTL 内直接复用已有结果)。"""
    row = db.get(SourceProbe, key)
    ttl = int(getattr(settings, "probe_ttl_days", 0) or 0)
    if row and not force and ttl > 0 and (datetime.utcnow() - row.probed_at).days < ttl:
        return row
    titles = _sample_titles(key, int(getattr(settings, "probe_sample_titles", 12) or 12))
    rel, reason, ok = 0.0, "", True
    if not titles:
        ok, reason = False, "抓不到首页或无可读标题,无法初评"
    else:
        try:
            from app.services.llm import get_screen_llm
            out = get_screen_llm().complete_json(
                _PROBE_SYS, f"站点:{key}\n最近文章标题:\n" + "\n".join(f"- {t}" for t in titles))
            rel = max(0.0, min(1.0, float(out.get("relevance") or 0)))
            reason = str(out.get("reason") or "")[:300]
        except Exception as e:  # noqa: BLE001 评不了不该阻断,记为未初评
            ok, reason = False, f"初评失败:{type(e).__name__}"
    if row is None:
        row = SourceProbe(identity_key=key)
        db.add(row)
    row.relevance, row.reason, row.ok = rel, reason, ok
    row.sample_titles, row.probed_at = titles[:12], datetime.utcnow()
    db.flush()
    return row


def probe_pending(db, need_id: str, on_progress=None) -> dict:
    """给还没初评(或初评过期)的候选域名补上 LLM 相关度分。"""
    if not getattr(settings, "probe_llm_enabled", True):
        return {"probed": 0, "skipped": "已在设置页关闭候选源LLM初评"}
    from app.models import SourceDiscoveryEvidence
    ttl = int(getattr(settings, "probe_ttl_days", 30) or 30)
    stale = datetime.utcnow() - timedelta(days=ttl)
    keys = {r.identity_key for r in db.query(SourceDiscoveryEvidence).all()}
    todo = []
    for k in sorted(keys):
        if db.get(SourceBlacklist, k) or db.query(Source).filter_by(site_key=k).first():
            continue                              # 已入库/已拉黑的不用再评
        row = db.get(SourceProbe, k)
        if row is None or row.probed_at < stale:
            todo.append(k)
    cap = int(getattr(settings, "probe_max_per_round", 0) or 0)
    if cap > 0:
        todo = todo[:cap]
    n = 0
    for i, k in enumerate(todo):
        if _cancel.is_set():
            break
        if on_progress:
            on_progress(i + 1, len(todo), k)
        probe_one(db, k)
        n += 1
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    return {"probed": n, "pending": len(todo)}


def llm_scores(db) -> dict[str, float]:
    """给 discovery.evaluate_candidates 用的 {identity_key: 0-1 相关度}。"""
    return {r.identity_key: float(r.relevance or 0.0)
            for r in db.query(SourceProbe).filter(SourceProbe.ok.is_(True)).all()}


# ---------------- 后台任务(找源 + 初评 + 评分入库) ----------------

def start(need_id: str) -> dict:
    """启动"主动找源"后台任务(幂等)。找源 → LLM 初评 → 重新评分自动入库。"""
    with _lock:
        if _state.get("running"):
            return dict(_state)
        _cancel.clear()
        _state.clear()
        _state.update({"running": True, "phase": "准备", "total": 0, "done": 0,
                       "current": "", "need_id": need_id, "hits": 0, "new_candidates": 0,
                       "probed": 0, "auto_trial": 0, "trial_names": [],
                       "started_at": datetime.utcnow().isoformat(timespec="seconds"),
                       "finished_at": None, "canceled": False})
    threading.Thread(target=_run, args=(need_id,), daemon=True).start()
    return status()


def _run(need_id: str):
    db = SessionLocal()
    try:
        with fetcher.render_session():      # 整轮复用一个浏览器实例
            _set(phase="主动找源(搜索引擎捞新渠道)")
            r = run_once(db, need_id,
                         on_progress=lambda d, t, c: _set(done=d, total=t, current=c))
            _set(hits=r["hits"], new_candidates=len(r["new_keys"]), engines=r["engines"],
                 note=r.get("note", ""), stats=r.get("stats", {}),
                 engine_detail=r.get("engine_detail", []), queries=r.get("queries", 0))
            if not r["new_keys"]:
                # 一无所获必须留痕:否则"主动找源"会长期静默空转,没人知道它其实一直没用
                from app.services import actions
                actions.record(db, "source.prospect_empty",
                               f"主动找源本轮没有发现新渠道:{r.get('note', '')}",
                               need_id=need_id, detail={"stats": r.get("stats"),
                                                        "engines": r.get("engine_detail")})
                try:
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()

            if not _cancel.is_set():
                _set(phase="候选源相关度初评(LLM)", done=0, total=0, current="")
                p = probe_pending(db, need_id,
                                  on_progress=lambda d, t, c: _set(done=d, total=t, current=c))
                _set(probed=p.get("probed", 0))

        _set(phase="评分与自动入库", current="")
        cands = discovery.evaluate_candidates(db, need_id, llm_scores(db))
        auto = [c for c in cands if c.get("auto_trial")]
        db.commit()
        _set(auto_trial=len(auto),
             trial_names=[c.get("name") or c["identity_key"] for c in auto[:20]],
             candidates=len(cands))
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        from app.services.errors import error_headline
        _set(error=error_headline(e))
    finally:
        _set(running=False, phase="完成", current="",
             finished_at=datetime.utcnow().isoformat(timespec="seconds"),
             canceled=_cancel.is_set())
        db.close()
