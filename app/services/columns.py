"""站点栏目自动发现:根域页面型源不去抓首页要闻,而是自动找出与需求相关的栏目并抓栏目。

政务/机构站栏目多且会变动,人工补 URL 不现实也不准。这里从站点导航/首页链接里,按相关词
自动识别"执法处罚/网络安全通报/数据安全/漏洞预警"等栏目,交给通用列表适配器分别采集。
动态站每次采集重新识别,栏目变了也能感知。纯词法打分,不依赖 LLM,快且稳。
"""
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.config import settings
from app.services import fetcher, url_tools

# 栏目相关词(命中越多越可能是目标栏目);与关键词矩阵的事件/后果词叠加
COLUMN_HINT_WORDS = [
    "执法", "处罚", "通报", "曝光", "案例", "网络安全", "数据安全", "信息安全", "个人信息",
    "漏洞", "预警", "情况通报", "违法违规", "查处", "打击", "净网", "监管", "处置", "事件",
    "安全", "泄露", "举报", "整治", "行政处罚", "监督管理", "风险提示", "安全通告", "公告",
]
# 明显无关的栏目词(直接排除)
COLUMN_STOP_WORDS = [
    "招聘", "关于我们", "联系", "网站地图", "版权", "登录", "注册", "English", "简介",
    "机构设置", "领导", "党建", "会议", "视频", "图片", "专题", "首页", "邮箱", "服务",
]


def is_root_only(url: str | None) -> bool:
    """入口链接是否只是站点根目录(无具体栏目路径)。"""
    if not url or not url.startswith("http"):
        return False
    path = (urlparse(url).path or "/").strip("/")
    return path == ""


def _score(anchor: str, href_path: str, extra_terms: list[str]) -> int:
    blob = anchor
    if any(w in blob for w in COLUMN_STOP_WORDS):
        return -1
    score = sum(1 for w in COLUMN_HINT_WORDS if w in blob)
    score += sum(1 for t in extra_terms if t and t in blob)
    # 路径里带 zhifa/chufa/tongbao/aqbao 等拼音/栏目段也加分(弱信号)
    if any(seg in href_path for seg in ("zhifa", "chufa", "tongbao", "aqfa", "wangan", "anquan")):
        score += 1
    return score


def find_columns(html: str, base_url: str, extra_terms: list[str] | None = None,
                 limit: int | None = None) -> list[dict]:
    """从页面 HTML 找同域相关栏目链接。返回 [{url, anchor, score}],按分降序,已按栏目URL去重。"""
    extra_terms = extra_terms or []
    limit = limit or settings.auto_column_max
    soup = BeautifulSoup(html or "", "lxml")
    base_dom = url_tools.registered_domain(urlparse(base_url).netloc)
    seen: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = url_tools.normalize_url(_abs(base_url, href))
        if not full.startswith("http"):
            continue
        if url_tools.registered_domain(urlparse(full).netloc) != base_dom:
            continue  # 只要本站栏目
        if is_root_only(full):
            continue  # 跳过根链接自身
        anchor = a.get_text(" ", strip=True)[:40]
        if not anchor or len(anchor) < 2:
            continue
        sc = _score(anchor, urlparse(full).path.lower(), extra_terms)
        if sc <= 0:
            continue
        prev = seen.get(full)
        if not prev or sc > prev["score"]:
            seen[full] = {"url": full, "anchor": anchor, "score": sc}
    ranked = sorted(seen.values(), key=lambda x: -x["score"])
    return ranked[:limit]


def _abs(base: str, href: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, href)


def discover_columns(source, extra_terms: list[str] | None = None) -> list[dict]:
    """抓根页 → 找相关栏目。返回栏目列表(可能为空)。"""
    if not source.entry_url:
        return []
    fr = fetcher.fetch(source.entry_url, render=(source.adapter_config or {}).get("render", "auto"))
    if not fr.ok:
        return []
    return find_columns(fr.html, fr.final_url or source.entry_url, extra_terms)


# ---------------- 栏目验证:文章一致性 ----------------

_NUM_SEG = re.compile(r"\d+")


def _article_links(html: str, base_url: str) -> list[dict]:
    """栏目页里比栏目更深一层的同域文章链接(归一化去重)。返回 [{url, title}]。"""
    soup = BeautifulSoup(html or "", "lxml")
    base_dom = url_tools.registered_domain(urlparse(base_url).netloc)
    base_depth = len([p for p in urlparse(base_url).path.split("/") if p])
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = url_tools.normalize_url(_abs(base_url, href))
        if not full.startswith("http") or full in seen:
            continue
        if url_tools.registered_domain(urlparse(full).netloc) != base_dom:
            continue
        depth = len([p for p in urlparse(full).path.split("/") if p])
        if depth <= base_depth:          # 只算比栏目更深的文章页
            continue
        seen.add(full)
        out.append({"url": full, "title": a.get_text(" ", strip=True)[:80]})
    return out


def _signature(url: str) -> str:
    """文章 URL 结构签名:目录前缀 + 数字段掩码。同栏目文章签名高度一致。"""
    p = urlparse(url)
    parts = [seg for seg in p.path.split("/") if seg][:-1]      # 去掉文件名本身
    return "/".join(_NUM_SEG.sub("#", seg) for seg in parts)


def _consistency(urls: list[str]) -> float:
    """文章一致性 = 最主流结构签名的占比(0-1)。越高越像"同一个栏目的文章列表"。"""
    if not urls:
        return 0.0
    from collections import Counter
    c = Counter(_signature(u) for u in urls)
    return c.most_common(1)[0][1] / len(urls)


# ---------------- 栏目验证:内容相关度 ----------------

_TERMS_CACHE: dict[str, list[str]] = {}


def relevance_terms(db=None, need_id: str = "sec_events") -> list[str]:
    """判定"栏目内容是否相关"用的词表:栏目相关词 + 关键词矩阵里的事件/后果词(拆到单词粒度)。

    优先读库里生效的关键词矩阵(设置页改了立即生效),读不到再回退 config/keyword_matrix.yaml。
    结果按 need 缓存,避免每验证一个栏目就查一次库。
    """
    # 库里的词表优先:没带 db 时若已缓存过库版本,直接复用,不要退回文件版
    if need_id in _TERMS_CACHE:
        return _TERMS_CACHE[need_id]
    if db is None and f"file:{need_id}" in _TERMS_CACHE:
        return _TERMS_CACHE[f"file:{need_id}"]
    raw: list[str] = []
    from_db = False
    try:
        if db is not None:
            from app.models import KeywordSet
            ks = db.query(KeywordSet).filter_by(need_id=need_id, is_active=True).first()
            if ks:
                for f in ("event_terms", "consequence_terms"):
                    raw += [str(t) for t in (ks.content or {}).get(f) or []]
                from_db = bool(raw)
    except Exception:  # noqa: BLE001 取词表失败不该影响栏目验证
        raw, from_db = [], False
    if not raw:
        try:
            import yaml
            data = yaml.safe_load((settings.config_dir / "keyword_matrix.yaml").read_text(encoding="utf-8"))
            for f in ("event_terms", "consequence_terms"):
                raw += [str(t) for t in (data or {}).get(f) or []]
        except Exception:  # noqa: BLE001
            pass
    terms = set(COLUMN_HINT_WORDS)
    for t in raw:
        # "网络安全法 处罚" 这类组合词按空格拆开,标题里通常只出现其中一段
        for part in str(t).split():
            part = part.strip()
            if len(part) >= 2 and not re.fullmatch(r"[A-Za-z ]+", part):
                terms.add(part)
    out = sorted(terms, key=len, reverse=True)
    # 只有取到库里的词表才占用正式缓存键;文件兜底单独缓存,后续带 db 调用仍会重新查库
    _TERMS_CACHE[need_id if from_db else f"file:{need_id}"] = out
    return out


def reset_terms_cache():
    """关键词矩阵变更后调用:下次栏目验证重新取词表。"""
    _TERMS_CACHE.clear()


def _relevance(titles: list[str], terms: list[str]) -> tuple[float, int]:
    """栏目内容相关度 = 命中安全相关词的标题占比。返回 (比例, 有效标题数)。"""
    named = [t for t in titles if t and len(t.strip()) >= 4]
    if not named:
        return 0.0, 0
    hit = sum(1 for t in named if any(w in t for w in terms))
    return hit / len(named), len(named)


def validate_column(url: str, render_pref="auto", terms: list[str] | None = None) -> dict:
    """验证候选栏目:抓该页,看它是否列出一批"高度一致"且"内容相关"的文章。

    有效标准三条同时满足:
    1) 文章数 ≥ column_min_articles —— 是个列表页而非单篇/空页;
    2) URL 结构一致性 ≥ column_consistency_min —— 是"同一个栏目"而非导航/杂链页;
    3) 标题相关度 ≥ column_relevance_min —— 内容确实是安全相关的,而不是"要闻/领导活动"。
       标题样本不足(全是图片链接等)时只按 1)2) 判,不误杀。
    第 3 条是"精准定位到相关内容"的关键:结构再规整,内容不相关的栏目也不该入库。
    """
    fr = fetcher.fetch(url, render=render_pref)
    if not fr.ok:
        return {"url": url, "valid": False, "article_count": 0, "consistency": 0.0,
                "relevance": 0.0, "reason": "抓取失败"}
    arts = _article_links(fr.html, fr.final_url or url)
    urls = [a["url"] for a in arts]
    cons = round(_consistency(urls), 2)
    rel, n_titles = _relevance([a["title"] for a in arts], terms or relevance_terms())
    rel = round(rel, 2)
    enough_titles = n_titles >= int(getattr(settings, "column_relevance_min_titles", 3) or 0)
    rel_ok = (not enough_titles) or rel >= settings.column_relevance_min
    valid = (len(arts) >= settings.column_min_articles
             and cons >= settings.column_consistency_min and rel_ok)
    if valid:
        reason = ""
    elif not rel_ok:
        reason = f"内容相关度{rel}(低于{settings.column_relevance_min}),疑似非安全类栏目"
    else:
        reason = f"文章{len(arts)}篇/一致性{cons},未达标"
    return {"url": url, "valid": valid, "article_count": len(arts), "consistency": cons,
            "relevance": rel, "titles_sampled": n_titles, "reason": reason,
            "sample": urls[:5]}


# ---------------- 栏目持久化(记录后不必每次重算) ----------------

def _children_of(db, parent_id: int) -> list:
    from app.models import Source
    # 排除已停用的:用户手动删/停某个自动栏目后,父源不应下次又把它拉回来抓
    return [s for s in db.query(Source).filter_by(discovered_from="column_auto").all()
            if (s.adapter_config or {}).get("parent_site_id") == parent_id
            and s.lifecycle != "retired"]


def discover_and_persist(db, source, extra_terms: list[str] | None = None) -> tuple[list, bool]:
    """发现并持久化站点栏目为子源。TTL 内直接复用已记录的栏目、不重算;过期或首次才重新识别验证。

    返回 (子栏目源列表, 是否本次重新识别)。子源标 parent_site_id,不参与独立调度(经父源采集)。
    """
    from datetime import datetime

    from app.models import Source
    cfg = dict(source.adapter_config or {})
    ts = cfg.get("columns_discovered_at")
    existing = _children_of(db, source.id)
    fresh = False
    if ts:
        try:
            fresh = (datetime.utcnow() - datetime.fromisoformat(ts)).days < settings.auto_column_refresh_days
        except ValueError:
            fresh = False
    if fresh and existing:
        return existing, False   # 记录仍新鲜 → 直接复用,不重算

    render_pref = cfg.get("render", "auto")

    # ① 先把所有网络活儿干完(不碰数据库)。此前是"验证一个→写库一个→再去验证下一个",
    #    第一次写库就拿到 SQLite 全局写锁,之后每次 validate_column 的网络抓取都占着锁不放,
    #    一个根域源最长可占锁上百秒 → 其他并行 worker 与主线程全部卡住,任务看起来"死了"。
    import time as _time
    budget = int(getattr(settings, "column_discovery_budget_seconds", 0) or 0)
    deadline = (_time.time() + budget) if budget > 0 else None
    need_id = (source.serves_needs or ["sec_events"])[0]
    terms = relevance_terms(db, need_id)
    validated = []
    for c in discover_columns(source, extra_terms):
        if deadline and _time.time() > deadline:
            break                                    # 栏目发现自身也要有时间上限
        # 文章高度一致(是个栏目)且内容相关(是"安全"栏目)才确认入库
        v = validate_column(c["url"], render_pref, terms)
        if v["valid"]:
            validated.append((c, v))

    # ② 再一次性写库(短事务),写完立刻提交释放写锁
    result = list(existing)
    known_ids = {c.identity_key for c in existing}
    for c, v in validated:
        ik = url_tools.normalize_url(c["url"])
        if ik in known_ids:
            continue
        child = db.query(Source).filter_by(identity_key=ik).one_or_none()
        if child is None:
            child = Source(
                name=f"{source.name}·{c['anchor']}", entry_url=c["url"], kind="page",
                adapter="generic_list",
                adapter_config={"parent_site_id": source.id, "render": render_pref},
                credibility=source.credibility, tier=source.tier, lifecycle="active",
                serves_needs=list(source.serves_needs or []),
                identity_key=ik, site_key=source.site_key, discovered_from="column_auto",
                note=(f"自动栏目(栏目名相关度{c['score']}/文章{v['article_count']}"
                      f"/结构一致性{v['consistency']}/内容相关度{v.get('relevance', 0)})"))
            db.add(child)
        else:
            ccfg = dict(child.adapter_config or {})   # 注意用独立变量:此前复用 cfg 会把子源配置写回父源
            if ccfg.get("manually_retired"):
                continue   # 用户手动删/停过这个栏目 → 尊重人工判断,不再自动拉回来抓
            # 该栏目已作为源存在(如上轮建过或人工添加):补挂到本站并纳入本轮采集,
            # 否则它既不会被父源抓、自身又可能不在调度里,栏目实际漏采。
            ccfg.setdefault("parent_site_id", source.id)
            child.adapter_config = ccfg
            if child.lifecycle == "retired":
                child.lifecycle = "active"
        known_ids.add(ik)
        result.append(child)
    cfg["columns_discovered_at"] = datetime.utcnow().isoformat()
    source.adapter_config = cfg
    db.flush()
    try:
        db.commit()          # 立即释放写锁,避免后续抓取期间继续占用
    except Exception:  # noqa: BLE001
        db.rollback()
    return result, True


# ---------------- 采集精准度 ----------------

def precision_of(source, db=None) -> dict:
    """这个源是否"精准定位到了相关内容"?返回 {level, precise, label, hint}。

    level 取值:
    - column   页面型且入口就是具体栏目 → 精准
    - resolved 根域源,但已自动定位到 N 个相关栏目(实际按栏目采) → 精准
    - search   检索型:关键词/站内检索圈定的页面集合 → 精准(按词命中,不是整站乱抓)
    - wechat   公众号号内文章集合 → 精准
    - root     根域源且尚未定位到任何栏目 → 不精准,需定位栏目
    """
    ident = source.identity_key or ""
    if ident.startswith("mp:") or source.adapter == "sogou_wechat":
        return {"level": "wechat", "precise": True, "label": "公众号",
                "hint": "按公众号采集其文章集合"}
    if source.kind == "query":
        site = (source.adapter_config or {}).get("site")
        return {"level": "search", "precise": True,
                "label": ("站内检索" if site else "关键词检索"),
                "hint": (f"限定 site:{site} 按关键词定位相关页面" if site
                         else "按关键词检索定位相关页面集合")}
    if not is_root_only(source.entry_url):
        return {"level": "column", "precise": True, "label": "栏目",
                "hint": "入口即具体栏目/RSS"}
    n = 0
    if db is not None:
        try:
            n = len(_children_of(db, source.id))
        except Exception:  # noqa: BLE001 统计失败不影响主流程
            n = 0
    if n:
        return {"level": "resolved", "precise": True, "label": f"已定位{n}个栏目",
                "hint": "根域入口,但采集时按已识别的相关栏目分别抓"}
    return {"level": "root", "precise": False, "label": "根域·未定位栏目",
            "hint": "只填了网站根地址,还没定位到相关栏目;采集时会自动识别,也可点「定位栏目」立即定位"}
