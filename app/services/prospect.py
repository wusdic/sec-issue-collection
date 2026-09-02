"""主动找源(D5)+ 候选源 LLM 相关度初评。

此前的源发现是纯被动的:只有"已采到的文章引用过"的渠道才会进候选池——一个从没被
引用过的好渠道永远发现不了,这是覆盖面的天花板。这里补上主动的一路:

1) 用「找源专用检索词」(config/discovery_sec.yaml 的 source_search_queries,加上覆盖度
   空白自动生成的词)去搜索引擎捞结果,把结果域名以 channel="source_search" 登记进
   同一个候选池——复用现成的评分/多通道闸门/黑名单,不另起炉灶;
2) 对候选域名做 LLM 相关度初评:抓其首页/列表页抽样标题,让模型判"是否持续产出国内
   安全事件相关内容",0-1 打分。这个分按 discovery_sec.yaml 的 weight_llm_relevance 计入
   候选总分,让排序看内容而不只看被提及次数。结果按 probe_ttl_days 复用不重评。
"""
import threading
import time as _time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import yaml

from app.config import settings
from app.db import SessionLocal
from app.models import Source, SourceBlacklist, SourceProbe
from app.services import need_ctx, discovery, fetcher, url_tools

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

def _ctx(ctx=None, need_id: str | None = None):
    return ctx or need_ctx.get(None, need_id or need_ctx.default_need_id())


def base_queries(ctx=None) -> list[str]:
    """找源专用检索词:画像配方文件里人工维护的;没有文件 → 关键词模块按 scope 生成。"""
    from app.services import keywords
    return keywords.search_queries_for(_ctx(ctx))


def _recipes(ctx=None) -> dict:
    from app.services import keywords
    return keywords.recipes_for(_ctx(ctx))


def combo_queries(ctx=None) -> list[str]:
    """按配方生成 **2 词** 找源组合:主体×动作、事件×动作、渠道词。

    词堆太多会把召回压死——搜索引擎对多词是 AND 收紧,「网信办 处罚 解读 公众号」几乎搜不到,
    而「网警 处罚」能捞到一批典型执法通报号。所以这里刻意只组两词,用组合数换广度。
    交叉顺序做了打散(不是先把"网警"配完所有动作),保证在 max_combos 截断时主体覆盖面仍均匀。
    """
    r = _recipes(ctx)
    subj = [str(x).strip() for x in r.get("subject_terms") or [] if str(x).strip()]
    act = [str(x).strip() for x in r.get("action_terms") or [] if str(x).strip()]
    ev = [str(x).strip() for x in r.get("event_terms") or [] if str(x).strip()]
    ch = [str(x).strip() for x in r.get("channel_terms") or [] if str(x).strip()]
    out: list[str] = list(ch)
    # 对角轮转:同一轮里每个主体配到的动作各不相同(而不是整轮都配"处罚"),
    # 这样被 max_combos 截断时,主体和动作两个维度的覆盖都是均匀的。
    for pool in (subj, ev):
        if not pool or not act:
            continue
        for k in range(len(act)):
            for i, s in enumerate(pool):
                out.append(f"{s} {act[(i + k) % len(act)]}")
    cap = int(r.get("max_combos", 60) or 60)
    return out[:cap] if cap > 0 else out


def _query_ok(q: str) -> bool:
    """这条词值不值得发出去。

    单独跑的 2 字词(入侵/爬虫/内鬼)在中文里太歧义,搜索引擎回的是百度百科、汉语字典、
    测速网这类东西——实测捞回来的 5 个候选全是这一类。这批词是为了给"限定词增益"
    提供基线才加的,但基线也得是个有区分度的词,所以单词至少要 3 个汉字;
    组成 2 词组合后语境已经收窄,2 字词照旧可用。

    词数上限不在这里管(那是变异环节的规则,见 query_evolution.mutate)——
    人工维护的固定词表里本来就有几条刻意写成 3 词的。
    """
    q = " ".join((q or "").split())
    if not q:
        return False
    n = len(_CJK_ALL.findall(q))
    if len(q.split()) == 1:
        return n >= _MIN_SOLO_CJK or len(q) >= 5     # 英文缩写(APT/CVE)按字符长度放行
    return n >= _MIN_QUERY_CJK or len(q) >= 4


def seed_queries(db, need_id: str) -> list[str]:
    """词表的原料池:固定短词 + 覆盖空白方向词 + 配方生成的 2 词组合。

    这些只是"可以跑什么",跑不跑、按什么顺序跑由 query_evolution 按历史表现决定。
    """
    ctx = need_ctx.get(db, need_id)
    qs = list(base_queries(ctx))
    try:
        from app.services import coverage
        qs += coverage.prospect_queries(db, need_id, ctx=ctx)
    except Exception:  # noqa: BLE001 覆盖度算不出来不该挡住找源
        pass
    qs += combo_queries(ctx)
    # 每个锚点单独也要能跑:增益(加了限定词到底是帮忙还是帮倒忙)全靠这个分母
    r = _recipes(ctx)
    qs += [str(x).strip() for x in (r.get("event_terms") or []) if str(x).strip()]
    seen, out = set(), []
    for q in qs:
        q = " ".join(q.split())
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def build_queries(db, need_id: str) -> list[str]:
    """本轮要跑的找源词 —— 交给关键词进化机制按历史表现排期。

    静态清单的问题是「零售 数据泄露」和「数据泄露」看起来一样重要,而实测前者因为
    搜索引擎按 AND 收紧,召回反而更差。进化机制按四类配额混词:利用高产词、跑锚点基线
    (增益的分母)、探索新词、复活歇着的词,让词表按实际产出自己变好、自己变宽。
    """
    seed = [q for q in seed_queries(db, need_id) if _query_ok(q)]
    cap = int(getattr(settings, "prospect_query_cap", 0) or 0) or len(seed)
    if not getattr(settings, "query_evolution_enabled", True):
        return seed[:cap]
    try:
        from app.services import query_evolution
        out = [q for q in query_evolution.plan(db, need_id, cap, seed=seed) if _query_ok(q)]
        db.commit()
        return out
    except Exception:  # noqa: BLE001 进化机制出问题不该让找源整个停摆
        db.rollback()
        return seed[:cap]


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


def _engines(names: list[str] | None = None):
    from app.services.adapters import _REGISTRY
    if names is None:
        names = str(getattr(settings, "prospect_engines", "") or "").split(",")
    out = []
    for name in names:
        name = name.strip()
        cls = _REGISTRY.get(name)
        if cls:
            out.append((name, cls(_Shim())))
    return out


_SKIP_HOSTS = {"baidu.com", "bing.com", "sogou.com", "google.com", "so.com", "zhihu.com",
               "weibo.com", "douyin.com", "bilibili.com", "csdn.net", "jianshu.com",
               "163.com", "qq.com", "sina.com.cn", "sina.cn", "sohu.com", "toutiao.com",
               "baijiahao.baidu.com", "duckduckgo.com",
               # 百科/导航/工具站:引擎在查询退化成宽泛词时最爱返回这一类,
               # 它们永远不会是"持续产出安全事件报道的渠道"
               "baike.baidu.com", "baike.com", "wikipedia.org", "zhidao.baidu.com",
               "hao123.com", "2345.com", "speedtest.cn", "zdic.net", "guoxuedashi.net",
               "dict.cn", "youdao.com", "iciba.com", "so.360.cn", "quark.cn"}

# 搜索引擎自家域名:结果停在这里只说明跳转链没还原,和"落在通用大平台"是两回事
_ENGINE_HOSTS = {"baidu.com", "bing.com", "sogou.com", "so.com", "google.com",
                 "duckduckgo.com", "sm.cn", "quark.cn"}

_MIN_QUERY_CJK = 2   # 少于两个汉字的词等于"什么都没问",引擎只会回百科/导航站
_MIN_SOLO_CJK = 3    # 单独跑的词还要更严:2 字词太歧义(「入侵」「爬虫」搜回来的是百科

# 页脚模板链:ICP 备案、公安网备、违法有害信息举报——几乎每个中文站底部都有一串。
# 它们不是搜索结果,一旦被通用抽链兜底扫进来,就会在统计里堆成"结果最多的域名",
# 把真正的召回情况整个盖住。
# 逐个域名列举是打地鼠:列了 beian.gov.cn,必应换成 beian.mps.gov.cn 就又漏 300 条。
# 备案/举报类站点一律是 beian.* / jubao.* 前缀,按前缀认。
_BOILERPLATE_HOSTS = ("dxzhgl.miit.gov.cn", "12377.cn", "12321.cn", "12318.gov.cn",
                      "miitbeian.gov.cn")
_BOILERPLATE_PREFIX = ("beian.", "jubao.", "icp.")
_BOILERPLATE_PATHS = ("/icp/", "/beian", "/portal/registersysteminfo")


def _is_boilerplate(url: str) -> bool:
    u = (url or "").lower()
    host = (urlparse(u).netloc or "").removeprefix("www.")
    if host.startswith(_BOILERPLATE_PREFIX):
        return True
    if any(host == h or host.endswith("." + h) for h in _BOILERPLATE_HOSTS):
        return True
    return any(p in u for p in _BOILERPLATE_PATHS)


def _resolve_redirect(url: str) -> str:
    """还原搜索引擎跳转链(C3)。

    百度结果链接是 www.baidu.com/link?url=…、必应是 bing.com/ck/a?…,直接取域名只会得到
    搜索引擎自己——这正是"搜索结果 0 条"的头号原因。跟一次跳转拿到真实站点。
    """
    try:
        # render="auto":搜狗/360 的跳转页是一段 JS(把真实地址拼出来再 location.href),
        # 纯 httpx 只会停在 sogou.com/so.com —— 实测自检里搜狗那批网警通报全折在这一步。
        # 未开浏览器渲染时 auto 自动降级为 httpx,零额外开销。
        fr = fetcher.fetch(url, timeout=min(10.0, float(settings.fetch_timeout)), render="auto")
        if fr.final_url and not url_tools.is_search_redirect(fr.final_url):
            return fr.final_url
        # 没跳走但页面里带着真实永久链接(搜狗的公众号跳转页就是这样)
        perm = url_tools.extract_wechat_permalink(fr.html or "")
        return perm or fr.final_url or url
    except Exception:  # noqa: BLE001
        return url


import re as _re

_RAW_BIZ_ID = _re.compile(r"^gh_[0-9a-zA-Z]{8,}$")


def _wechat_account(url: str) -> str | None:
    """公众号文章 → 它属于哪个号(抓一次文章解析)。失败返回 None。"""
    try:
        from app.services import wechat
        info = wechat.resolve_account(url)
        return info.get("account") if info.get("ok") else None
    except Exception:  # noqa: BLE001
        return None


_CJK = _re.compile(r"[一-鿿]")
_CJK_ALL = _re.compile(r"[一-鿿]")     # 数汉字个数用(_query_ok)


def _looks_domestic(url: str, title: str | None, ctx=None) -> bool:
    """这条结果符不符合画像的地域/语言范围(need.regions / languages → sources.region_policy)。

    我们喂的全是中文找源词,搜索引擎却会跨语言给出 Thesaurus.com 这类纯英文站。
    判定放得很松——域名是本地后缀,或标题里有一个本地文字,就算数——只挡明显的语言噪声;
    但画像拒绝的境外国家域名一律不收,因为日文/韩文标题同样含汉字,躲得过语言判定。
    画像不限地域(region_policy 全空)时一律放行。
    """
    if url_tools.platform_account(url):
        return True          # 公众号/百家号/微博号:身份是"号"不是站,标题短且常是英文,不参与语言判定
    pol = _ctx(ctx).region_policy
    host = (urlparse(url).netloc or "").lower()
    if any(host.endswith(t) for t in pol.get("reject_tlds") or []):
        return False
    if any(host.endswith(t) for t in pol.get("domestic_tlds") or []):
        return True
    if (pol.get("require_script") or "none") == "cjk":
        return bool(_CJK.search(title or ""))
    return True


def _candidate(url: str, budget: dict, ctx=None) -> tuple[str | None, str, dict]:
    """搜索结果 URL → (候选源标识键, 丢弃原因, 额外信息)。

    关键点:公众号文章 / 百家号作者页 / 微博用户页要识别成"某个号",不能按注册域退化成
    qq.com / baidu.com / weibo.com —— 那样会被"通用大平台"规则整批丢掉,而这恰恰是
    最该收的一类源(用户要的执法通报大多发在公众号里)。
    """
    if not url or not url.startswith("http"):
        return None, "bad_url", {}
    plat = url_tools.platform_account(url)
    if plat:
        if not plat["needs_resolve"]:
            return plat["key"], "", {"kind": plat["kind"], "entry_url": plat["entry_url"]}
        cap = int(getattr(settings, "prospect_wechat_resolve_max", 30) or 0)
        if cap and budget.get("wechat", 0) >= cap:
            return None, "wechat_over_budget", {}
        budget["wechat"] = budget.get("wechat", 0) + 1
        acct = _wechat_account(url)
        if not acct or _RAW_BIZ_ID.match(acct):
            return None, "wechat_unresolved", {}   # 解析不出真名的号采不了,不入池
        return f"mp:{acct}", "", {"kind": "wechat", "account": acct}
    key = url_tools.identity_key_for(url)
    if not key:
        return None, "bad_url", {}
    if key in _ENGINE_HOSTS and url_tools.is_search_redirect(url):
        # 还是一条没还原成功的跳转链,不是"这条结果落在通用大平台"。混成一个原因会
        # 得出完全错误的结论:自检曾把搜狗判成"不可用",而它其实正正好好搜回了一批
        # 网警通报,只是卡在跳转页拿不到最终链接。
        # (注意要看 URL 本身是不是跳转链——baike.baidu.com 归一化后也叫 baidu.com,
        #  但它是真结果、只是没价值,那属于"大平台"而不是"还原失败"。)
        return None, "redirect_unresolved", {}
    if key in _SKIP_HOSTS or (ctx is not None and key in ctx.skip_hosts_extra):
        return None, "platform", {}      # 知乎/CSDN/门户等通用大平台(+画像追加的),不作为专业源
    return key, "", {}


def _pace(ename: str, streak: int):
    """请求之间留间隔,连错时再退避——搜狗/百度上一轮各只跑了 1 页和 3 页就被拦死。

    不做退避的话,熔断只是让它"更快地死",召回一样是 0;慢一点反而能多跑几十页。
    """
    base = float(getattr(settings, "prospect_delay_seconds", 1.5) or 0)
    if base <= 0:
        return
    _time.sleep(min(base * (2 ** min(streak, 4)), 30.0) if streak else base)


def run_once(db, need_id: str, on_progress=None) -> dict:
    """跑一轮主动找源。

    返回里带完整统计与人读结论:"0 条"必须说得出为什么(引擎被反爬 / 全是跳转链还原不了 /
    全是大平台 / 全是已有源),否则页面上一排 0 等于什么都没说。
    """
    engines = _engines()
    ctx = need_ctx.get(db, need_id)
    queries = build_queries(db, need_id)
    pages = max(1, int(getattr(settings, "prospect_pages_per_query", 1) or 1))
    resolve_cap = int(getattr(settings, "prospect_resolve_max", 60) or 0)
    st = {"pages": 0, "fetch_fail": 0, "raw_items": 0, "redirect": 0, "resolved": 0,
          "platform": 0, "bad_url": 0, "empty_pages": 0, "blocked_pages": 0,
          # 丢弃原因细分:"已是现有源"和"被拉黑"是两回事,混成一个数字等于没说
          "already_source": 0, "blacklisted": 0, "engine_self": 0, "invalid_name": 0,
          "wechat_unresolved": 0, "wechat_over_budget": 0, "redirect_over_budget": 0,
          "redirect_cached": 0, "non_chinese": 0, "boilerplate": 0, "excluded_sites": 0,
          "redirect_unresolved": 0,
          "new_wechat": 0, "new_platform_account": 0, "column_hints": 0}
    dropped: dict[str, int] = {}      # 被丢弃的域名 → 次数(给人看"到底丢了什么")
    budget: dict[str, int] = {}
    col_hints: dict[str, set] = {}    # 已有源站点上搜到的相关页面 → 反推该站漏采的栏目
    edetail: dict[str, dict] = {e[0]: {"engine": e[0], "pages": 0, "items": 0, "errors": 0}
                                for e in engines}
    # 引擎在本轮内被反爬打死就别再喂词了:实测百度连错 148 次仍每个词都试一遍,
    # 纯耗时且拖慢其它引擎。连错到阈值就本轮停用,下一轮(或引擎自检)再给机会。
    fail_streak: dict[str, int] = {name: 0 for name, _ in engines}
    streak_cap = int(getattr(settings, "prospect_engine_fail_streak", 8) or 0)
    redirect_cache: dict[str, str] = {}   # 同一条跳转链只还原一次
    # 某引擎老把结果落在同几个"我们早就有的站"上时,后续的词直接用 -site: 把它们排掉。
    # 实测 941 条结果里 905 条是现有源、其中 904 条只来自 miit/mps 两个域——不排掉等于整轮空转。
    hog: dict[str, dict[str, int]] = {name: {} for name, _ in engines}
    excl: dict[str, list[str]] = {name: [] for name, _ in engines}
    topic_min = float(getattr(settings, "engine_on_topic_min", 0.3))
    topic_min_n = int(getattr(settings, "engine_on_topic_min_items", 20))
    hog_at = int(getattr(settings, "prospect_exclude_after_hits", 25) or 0)
    hog_max = int(getattr(settings, "prospect_exclude_max_sites", 6) or 0)
    new_keys, resolved_used = set(), 0
    # 每条词的产出单独记账,词表才谈得上按表现进化(而不是每周原样重跑)
    per_q: dict[str, dict] = {q: {"results": 0, "usable": 0, "new": set()} for q in queries}
    total = max(1, len(queries) * max(1, len(engines)))
    done = 0
    for q in queries:
        acc = per_q[q]
        for ename, eng in engines:
            if _cancel.is_set():
                break
            if streak_cap and fail_streak.get(ename, 0) >= streak_cap:
                edetail[ename].setdefault("stopped", f"连续失败 {streak_cap} 次,本轮停用")
                done += 1
                continue
            ed0 = edetail[ename]
            if (ed0.get("topic_total", 0) >= topic_min_n
                    and ed0["topic_hit"] / ed0["topic_total"] < topic_min):
                ed0.setdefault("stopped",
                               f"结果与查询词无关(命中率 {ed0['topic_hit']}/{ed0['topic_total']}),本轮停用")
                done += 1
                continue
            eq = q + "".join(f" -site:{h}" for h in excl[ename])
            try:
                for page in range(pages):
                    _pace(ename, fail_streak.get(ename, 0))
                    items = eng.search_page(eq, page)
                    if items is None:
                        st["fetch_fail"] += 1
                        edetail[ename]["errors"] += 1
                        if page == 0:
                            # 只有第一页就抓不到才算"这个引擎不行"。翻页翻到头也返回 None,
                            # 把它算进连败会把正常引擎误熔断
                            fail_streak[ename] = fail_streak.get(ename, 0) + 1
                        break               # 抓不到(403/反爬/网络)
                    fail_streak[ename] = 0
                    st["pages"] += 1
                    edetail[ename]["pages"] += 1
                    if not items:
                        # 抓到页面却一条没解析出来:分清"验证页/反爬"与"页面正常但没结果",
                        # 并留一段可见文本样本,便于定位到底返回了什么
                        st["empty_pages"] += 1
                        _diagnose_empty(eng, q, page, edetail[ename], st)
                        break
                    edetail[ename]["items"] += len(items)
                    # 引擎在不在回答我们的问题:整页标题都不带查询词 = 它搜的不是这个词。
                    # 必应 RSS 实测就是这样(搜「网警 处罚」回日本 IT 咨询公司),而这种
                    # 结果域名都合法,不看相关度是拦不住的。
                    ed = edetail[ename]
                    ed["topic_hit"] = ed.get("topic_hit", 0) + sum(
                        1 for i in items if _on_topic_rate(q, [i.title]) > 0)
                    ed["topic_total"] = ed.get("topic_total", 0) + len(items)
                    # 留几条真实结果样本:上一轮"必应 900 条全落在两个已有域"这种情况,
                    # 光看统计数字判不出是真结果还是页脚模板链,有样本就一眼看穿
                    for s in items[:2]:
                        if len(edetail[ename].setdefault("url_samples", [])) < 6:
                            edetail[ename]["url_samples"].append(f"{(s.title or '')[:40]} → {s.url[:110]}")
                    for it in items:
                        st["raw_items"] += 1
                        acc["results"] += 1
                        url = it.url
                        # 搜狗微信的结果页自带公众号名:直接拿来用,不必逐条还原临时链再抓文章
                        # (此前 790 条搜狗结果全被当成跳转链,受配额限制只还原了几十条,绝大多数白丢)
                        acct = (getattr(it, "wechat_account", None) or "").strip()
                        if acct and _RAW_BIZ_ID.match(acct):
                            # 搜狗有时给的是原始号 ID(gh_81a05c27673f)而不是号名。这种 ID
                            # 既不可读、也没法用"按号名检索"去采,必须解析成真名才有用。
                            acct = ""       # 走下面的跳转链还原 + 文章解析拿真实号名
                        if acct:
                            key = f"mp:{acct}"
                            why_skip = _why_known(db, key)
                            if why_skip:
                                st[why_skip] += 1
                                dropped[key] = dropped.get(key, 0) + 1
                                continue
                            acc["usable"] += 1
                            k = discovery.record_evidence(db, None, "source_search",
                                                          display_name=acct, wechat_account=acct,
                                                          found_by_query=q)
                            if k:
                                if k not in new_keys:
                                    acc["new"].add(k)
                                new_keys.add(k)
                                st["new_wechat"] += 1
                            else:
                                st["invalid_name"] += 1
                            continue
                        if url_tools.is_search_redirect(url):
                            st["redirect"] += 1
                            cached = redirect_cache.get(url)
                            if cached is not None:
                                st["redirect_cached"] += 1
                                url = cached
                            elif resolve_cap and resolved_used >= resolve_cap:
                                st["redirect_over_budget"] += 1
                                continue     # 还原有配额,超了就跳过(否则一轮几百次请求)
                            else:
                                resolved_used += 1
                                url = _resolve_redirect(url)
                                redirect_cache[it.url] = url
                            if url_tools.is_search_redirect(url):
                                continue     # 还原失败(JS 跳转/需登录)
                            st["resolved"] += 1
                        if _is_boilerplate(url):
                            # 备案/举报中心这类页脚模板链:任何中文站底部都有,永远不是搜索结果
                            st["boilerplate"] += 1
                            continue
                        if not _looks_domestic(url, it.title, ctx):
                            # 中文词搜出来的纯英文外站(Thesaurus.com 之类)是引擎的跨语言噪声,
                            # 不该占候选池名额,也不值得再花一次 LLM 初评
                            st["non_chinese"] += 1
                            continue
                        key, why, extra = _candidate(url, budget, ctx)
                        if not key:
                            st[why if why in st else "bad_url"] += 1
                            if why in ("platform", "redirect_unresolved"):
                                d = url_tools.identity_key_for(url)
                                dropped[d] = dropped.get(d, 0) + 1
                            continue
                        why_skip = _why_known(db, key)
                        if why_skip:
                            st[why_skip] += 1
                            dropped[key] = dropped.get(key, 0) + 1
                            if why_skip == "already_source":
                                # 站点已有源 ≠ 这个栏目已在采。搜索命中的正是"相关内容所在的页面",
                                # 拿它反推栏目,补上该站尚未覆盖的栏目——这批结果此前是纯浪费。
                                col_hints.setdefault(key, set()).add(url)
                                # 同一个已有域反复霸榜时,后面的词就把它 -site: 排掉,
                                # 让搜索引擎去翻第二梯队的渠道——那里才有没收录过的源
                                if hog_at and ":" not in key:
                                    hog[ename][key] = hog[ename].get(key, 0) + 1
                                    if (hog[ename][key] >= hog_at and key not in excl[ename]
                                            and len(excl[ename]) < hog_max):
                                        excl[ename].append(key)
                                        st["excluded_sites"] += 1
                                        edetail[ename].setdefault("excluded", []).append(key)
                            continue
                        acc["usable"] += 1
                        # 复用统一入口:黑名单/已注册/主体名校验/去重全都走同一套规则
                        k = discovery.record_evidence(
                            db, None if extra.get("kind") == "wechat" else url, "source_search",
                            display_name=(extra.get("account") or (it.title or ""))[:80],
                            wechat_account=extra.get("account"),
                            platform_key=key if extra.get("kind") in ("baijiahao", "weibo") else None,
                            found_by_query=q)
                        if k:
                            if k not in new_keys:
                                acc["new"].add(k)
                            new_keys.add(k)
                            if extra.get("kind") == "wechat":
                                st["new_wechat"] += 1
                            elif extra.get("kind"):
                                st["new_platform_account"] += 1
                        else:
                            st["invalid_name"] += 1
            except Exception as e:  # noqa: BLE001 单条词失败不影响整轮
                edetail[ename]["errors"] += 1
                edetail[ename].setdefault("last_error", f"{type(e).__name__}: {e}"[:160])
            done += 1
            if on_progress:
                on_progress(done, total, f"{ename} · {q}",
                            {"queries": len(queries), "engines": len(engines),
                             "unit": f"({len(queries)} 词 × {len(engines)} 引擎)"})
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    hinted = _columns_from_hints(db, col_hints)
    st["column_hints"] = len(hinted)
    _record_query_outcomes(db, need_id, per_q)
    out = {"queries": len(queries), "engines": [e[0] for e in engines],
           "query_yield": sorted(
               ({"query": q, "results": a["results"], "usable": a["usable"],
                 "new_channels": len(a["new"])} for q, a in per_q.items()),
               key=lambda x: (-x["new_channels"], -x["usable"]))[:20],
           "hits": st["raw_items"], "new_keys": sorted(new_keys), "new_columns": hinted,
           "stats": st, "engine_detail": list(edetail.values()),
           "dropped_top": sorted(({"key": k, "count": n} for k, n in dropped.items()),
                                 key=lambda x: -x["count"])[:20]}
    out["note"] = explain(out)
    return out


def _record_query_outcomes(db, need_id: str, per_q: dict):
    """把每条词本轮的产出记进台账。记账失败绝不能影响本轮找源的结果。"""
    if not getattr(settings, "query_evolution_enabled", True):
        return
    try:
        from app.services import query_evolution
        for q, a in per_q.items():
            query_evolution.record_run(db, need_id, q, results=a["results"],
                                       usable=a["usable"], new_channels=len(a["new"]))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _columns_from_hints(db, hints: dict[str, set]) -> list[dict]:
    """把"已有源站点上搜到的相关页面"反推成该站尚未采集的栏目并落库。

    站点已有源 ≠ 这个栏目已在采:比如 miit.gov.cn 已经是源,但搜索命中的 162 个页面可能
    分布在好几个我们还没定位到的栏目下。这批结果原来是纯浪费,现在拿来补栏目。
    """
    from app.services import columns
    cap = int(getattr(settings, "prospect_column_hint_max", 8) or 0)
    out: list[dict] = []
    if not hints or cap <= 0:
        return out
    for site, urls in hints.items():
        parent = (db.query(Source).filter_by(site_key=site)
                  .filter(Source.lifecycle != "retired").first())
        if not parent:
            continue
        # 文章 URL → 其所在目录(栏目页),按命中次数排序:命中越多越可能是稳定栏目
        cand: dict[str, int] = {}
        for u in urls:
            pr = urlparse(u)
            segs = [x for x in (pr.path or "").split("/") if x]
            if len(segs) < 2:
                continue
            col = f"{pr.scheme}://{pr.netloc}/" + "/".join(segs[:-1]) + "/"
            cand[col] = cand.get(col, 0) + 1
        ranked = sorted(cand.items(), key=lambda kv: -kv[1])[:cap]
        terms = columns.relevance_terms(db, (parent.serves_needs or [need_ctx.default_need_id()])[0])
        for col_url, n in ranked:
            ik = url_tools.normalize_url(col_url)
            if db.query(Source).filter_by(identity_key=ik).first():
                continue                       # 该栏目已经是源了
            v = columns.validate_column(col_url, (parent.adapter_config or {}).get("render", "auto"),
                                        terms)
            if not v["valid"]:
                continue
            child = Source(
                name=f"{parent.name}·搜索发现栏目", entry_url=col_url, kind="page",
                adapter="generic_list",
                adapter_config={"parent_site_id": parent.id, "render": "auto"},
                credibility=parent.credibility, tier=parent.tier, lifecycle="active",
                serves_needs=list(parent.serves_needs or []),
                identity_key=ik, site_key=parent.site_key, discovered_from="column_auto",
                note=(f"由主动找源命中 {n} 篇反推的栏目(文章{v['article_count']}"
                      f"/一致性{v['consistency']}/相关度{v.get('relevance', 0)})"))
            db.add(child)
            try:
                db.flush()
            except Exception:  # noqa: BLE001 并发/重复不致命
                db.rollback()
                continue
            out.append({"site": site, "url": col_url, "hits": n, "name": child.name})
    if out:
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    return out


def _why_known(db, key: str) -> str | None:
    """这个候选键为什么不用再收:已是现有源 / 已拉黑 / 是搜索引擎自己。返回统计字段名。"""
    if key in ("baidu.com", "bing.com", "sogou.com", "weibo.com", "google.com"):
        return "engine_self"
    if db.get(SourceBlacklist, key):
        return "blacklisted"
    if db.query(Source).filter_by(site_key=key).first():
        return "already_source"
    return None


def _on_topic_rate(query: str, titles: list) -> float:
    """结果标题里有多少比例真的提到了查询词。

    判"这个引擎在不在回答我们的问题"最便宜也最硬的信号。中文检索里,对口结果的标题
    几乎一定会带上查询词本身(搜「网警 处罚」回来的是「某地网警依法查处…」);
    整批都不带,就说明引擎压根没在搜这个词。
    按 2 字滑窗匹配,避免"网警"必须整词出现才算命中。
    """
    terms = [t for t in (query or "").split() if t]
    grams: set[str] = set()
    for t in terms:
        if len(t) <= 2:
            grams.add(t)
        else:
            grams.update(t[i:i + 2] for i in range(len(t) - 1))
    if not grams:
        return 1.0
    ts = [str(x or "") for x in titles]
    if not ts:
        return 0.0
    return sum(1 for x in ts if any(g in x for g in grams)) / len(ts)


def selftest(db, query: str | None = None, on_progress=None, ctx=None) -> dict:
    """找源路径可行性自检:每个引擎只跑一条词,报告这条路到底通不通。

    先验证路径再铺关键词——否则铺了几百个词,发现引擎全被反爬,白跑几十分钟。
    每个引擎给出:抓没抓到、是不是验证页、解析出几条、还原后落到哪些域名、可用与否 + 建议。
    """
    # 测整个候选池,不只是当前在用的那几个:被上一次自检踢掉的引擎(反爬是临时的)
    # 和升级新加的引擎都必须有机会重新证明自己,否则一旦误判就永远回不来。
    c = _ctx(ctx)
    from app.services import keywords
    query = query or keywords.selftest_query_for(c)
    out = {"query": query, "engines": [], "usable": [], "tested": [], "advice": ""}
    pool = all_engine_names()
    for name, eng in _engines(pool):
        row = {"engine": name, "ok": False, "blocked": False, "items": 0,
               "samples": [], "hint": ""}
        try:
            fr = fetcher.fetch(eng.build_url(query, 0, None),
                               render=eng.config.get("render", False))
            if not fr.ok:
                row["hint"] = f"抓不到:{fr.error or fr.status}(网络不通或直接被拒)"
            elif eng.looks_blocked(fr.html):
                row["blocked"] = True
                row["hint"] = "返回验证页/反爬拦截。开设置页的「启用浏览器渲染/截图」多半能解决;否则把它从引擎列表去掉"
            else:
                items = eng.parse(fr.html) or []
                row["items"] = len(items)
                if not items:
                    row["hint"] = ("页面正常但一条结果都没解析出来:结果块多半由 JS 注入"
                                   "(必应网页版就是这样)。开「启用浏览器渲染/截图」,"
                                   "或改用 bing_rss —— 纯 XML 口,不依赖 JS 也不随改版失配")
                else:
                    row["on_topic"] = round(_on_topic_rate(query, [i.title for i in items]), 2)
                    for it in items[:5]:
                        u = it.url
                        acct = (getattr(it, "wechat_account", None) or "").strip()
                        if not acct and url_tools.is_search_redirect(u):
                            u = _resolve_redirect(u)
                        # 走和正式跑一样的过滤链,否则自检的结论预测不了真实行为
                        if not acct and _is_boilerplate(u):
                            key, why = None, "boilerplate"
                        elif not acct and not _looks_domestic(u, it.title, c):
                            key, why = None, "non_chinese"
                        else:
                            key, why, _x = _candidate(u, {"wechat": 10 ** 6}, c)
                        row["samples"].append(
                            {"title": (it.title or "")[:60],
                             "key": (f"mp:{acct}" if acct else (key or "")),
                             "drop": "" if (acct or key) else why})
                    row["ok"] = any(x["key"] for x in row["samples"])
                    drops = [x["drop"] for x in row["samples"] if x["drop"]]
                    if row["on_topic"] < float(getattr(settings, "engine_on_topic_min", 0.3)):
                        # 能解析出域名 ≠ 这个引擎在回答我们的问题。实测必应 RSS 对
                        # 「网警 处罚」返回的是日本 IT 咨询公司、加拿大税务局、知乎 IRR 问答,
                        # 却因为域名合法被判成"可用"——返回无关内容比返回空更糟,
                        # 那些结果看着像真结果,会一路混进候选池。
                        row["ok"] = False
                        row["off_topic"] = True
                        row["hint"] = (f"结果和查询词无关(命中率仅 {row['on_topic']}):"
                                       f"搜「{query}」却返回了别的主题。这种引擎比搜不到更糟——"
                                       "无关结果看着像真结果,会混进候选池。不要用它")
                    elif row["ok"]:
                        row["hint"] = "可用:能解析出结果且能定位到具体渠道"
                    elif drops.count("redirect_unresolved") >= max(1, len(drops) // 2):
                        # 这一类不是"引擎不行":搜回来的内容对得上,只是卡在跳转页拿不到最终链接。
                        # 之前把它和"落在通用大平台"混成一句,自检因此把搜狗判成不可用——
                        # 而搜狗恰恰是唯一能搜到网警通报公众号的引擎。
                        row["blocked_by"] = "redirect"
                        row["hint"] = ("引擎本身好的:搜回来的结果内容对得上,但都卡在它自家的跳转页、"
                                       "还原不出真实链接(搜狗/360 的跳转页要跑 JS)。"
                                       "开设置页的「启用浏览器渲染/截图」即可,不要把这个引擎去掉")
                    elif drops.count("wechat_unresolved"):
                        row["blocked_by"] = "wechat"
                        row["hint"] = ("搜到的是公众号文章,但解析不出所属号名。"
                                       "开「启用浏览器渲染/截图」后公众号页才打得开")
                    else:
                        row["hint"] = "解析出结果了,但都落在通用大平台/百科导航站,不作专业源"
        except Exception as e:  # noqa: BLE001
            row["hint"] = f"{type(e).__name__}: {e}"[:160]
        out["engines"].append(row)
        out["tested"].append(name)
        if row["ok"]:
            out["usable"].append(name)
        if on_progress:
            on_progress(len(out["tested"]), len(pool), name)
    # 卡在跳转/公众号解析的引擎单独列出来:它们是"差一步"而不是"不行",
    # 结论里必须区分开,否则人会把最该留的引擎删掉
    out["render_would_fix"] = [e["engine"] for e in out["engines"]
                               if not e["ok"] and e.get("blocked_by")]
    fix = out["render_would_fix"]
    if fix and not getattr(settings, "playwright_enabled", False):
        out["advice"] = (f"关键结论:{'、'.join(fix)} 搜回来的内容是对的,只差最后一步"
                         "(跳转页/公众号页要跑 JS)。请先到设置页打开「启用浏览器渲染/截图」"
                         "再自检一次——这一步的收益比换引擎大得多,别把这些引擎删掉。"
                         + (f"当前已可直接用的:{'、'.join(out['usable'])}。" if out["usable"] else ""))
    elif out["usable"]:
        out["advice"] = (f"可用引擎:{'、'.join(out['usable'])}。引擎列表由「引擎自检并自动只留可用的」"
                         "每 3 天自动维护,一般不用手改。")
    else:
        out["advice"] = ("所有引擎都不可用,现在铺再多关键词也没用。先解决这一步:"
                         "①开启「启用浏览器渲染/截图」(对验证页和跳转页最有效);"
                         "②或确认这台机器能正常访问搜索引擎。")
    return out


# ---------------- 自检:后台跑 + 结果落库(切页回来还看得到) ----------------

_st_lock = threading.Lock()
_st_state: dict = {"running": False}


def selftest_status(db=None) -> dict:
    """当前自检状态;没在跑就把上一次的结果读回来。

    自检要抓好几个引擎、开渲染时一个引擎就要十几秒,同步等在页面上必然"点完切页就丢"。
    所以改成后台跑 + 结果落 AutoOpsRun:切页/刷新回来都能看到跑到哪、上次结论是什么。
    """
    with _st_lock:
        cur = dict(_st_state)
    if cur.get("running") or db is None:
        return cur
    from app.models import AutoOpsRun
    row = (db.query(AutoOpsRun).filter(AutoOpsRun.task == "engines_selftest")
           .order_by(AutoOpsRun.started_at.desc()).first())
    if not row:
        return {"running": False, "never": True}
    out = dict(row.summary or {})
    out.update({"running": False, "status": row.status,
                "at": row.started_at.isoformat(timespec="seconds"),
                "finished_at": row.finished_at.isoformat(timespec="seconds") if row.finished_at else None})
    if row.status == "failed":
        out["error"] = row.note
    return out


def selftest_start(need_id: str, query: str | None = None, apply: bool = True) -> dict:
    """后台起一次自检(幂等)。测试词缺省取画像 sources.prospect.selftest_query。"""
    from app.services import keywords
    query = query or keywords.selftest_query_for(need_ctx.get(None, need_id))
    with _st_lock:
        if _st_state.get("running"):
            return dict(_st_state)
        _st_state.clear()
        _st_state.update({"running": True, "phase": "准备", "done": 0,
                          "total": len(all_engine_names()), "current": "",
                          "query": query, "engines": [],
                          "started_at": datetime.utcnow().isoformat(timespec="seconds")})
    threading.Thread(target=_selftest_run, args=(need_id, query, apply), daemon=True).start()
    return selftest_status()


def _st_set(**kw):
    with _st_lock:
        _st_state.update(kw)


def _selftest_run(need_id: str, query: str, apply: bool):
    from app.models import AutoOpsRun
    db = SessionLocal()
    row = AutoOpsRun(need_id=need_id, task="engines_selftest", status="running")
    try:
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    try:
        with fetcher.render_session():
            r = selftest(db, query, on_progress=lambda d, t, c: _st_set(done=d, total=t, current=c),
                         ctx=need_ctx.get(db, need_id))
        if apply and getattr(settings, "prospect_autotune", True):
            r["applied"] = apply_selftest(db, r)
        row.status, row.summary = "done", r
        row.finished_at = datetime.utcnow()
        db.commit()
        _st_set(**r)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        from app.services.errors import error_headline
        msg = error_headline(e)
        try:
            row.status, row.note, row.finished_at = "failed", msg, datetime.utcnow()
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        _st_set(error=msg)
    finally:
        _st_set(running=False, phase="完成", current="",
                finished_at=datetime.utcnow().isoformat(timespec="seconds"))
        db.close()


def apply_selftest(db, r: dict) -> dict:
    """把一次自检的结论落到在用引擎列表上。

    自检已经把"哪个能用"算出来了,还要人去设置页照抄一遍没有意义。
    保底同 autotune:一个都不可用时保持原样不动,宁可空跑也不要把找源彻底关掉。
    """
    from app.services import settings_service
    usable = list(r.get("usable") or [])
    prev = [x.strip() for x in str(getattr(settings, "prospect_engines", "")).split(",") if x.strip()]
    tested = list(r.get("tested") or [e["engine"] for e in r.get("engines") or []])
    if tested:
        settings_service.save(db, {"prospect_engines_tuned": ",".join(sorted(set(tested)))})
    if not usable:
        return {"changed": False, "note": "一个都不可用,保持原配置不动"}
    if sorted(usable) == sorted(prev):
        return {"changed": False, "note": f"引擎无变化:{'、'.join(usable)}"}
    settings_service.save(db, {"prospect_engines": ",".join(usable)})
    added, removed = sorted(set(usable) - set(prev)), sorted(set(prev) - set(usable))
    note = ("按自检结果调整引擎:"
            + ("加回 " + "、".join(added) + ";" if added else "")
            + ("停用 " + "、".join(removed) if removed else "")).rstrip(";")
    from app.services import actions
    actions.record(db, "source.engines_tuned", note,
                   detail={"usable": usable, "before": prev, "detail": r.get("engines")})
    return {"changed": True, "note": note, "added": added, "removed": removed}


def all_engine_names() -> list[str]:
    """候选引擎池:自动调优时会把池子里每个都测一遍(掉线的踢掉、恢复的加回来)。

    池子取"配置值 ∪ 本版本内置的默认池":用户库里存过一次 prospect_engines_all 之后,
    升级新加的引擎(如 bing_rss)就再也进不了池子、永远测不到——那等于新引擎白加。
    """
    from app import config
    raw = str(getattr(settings, "prospect_engines_all", "") or "")
    names = [x.strip() for x in raw.split(",") if x.strip()]
    if not names:
        names = [x.strip() for x in str(getattr(settings, "prospect_engines", "")).split(",")
                 if x.strip()]
    shipped = [x.strip() for x in config.SHIPPED_ENGINES.split(",") if x.strip()]
    return names + [n for n in shipped if n not in names]


def sync_new_engines(db) -> dict:
    """把"本版本新加、且从没被自检评价过"的引擎补进在用列表。

    自动调优只在既有列表上做加减,升级新加的引擎不会自己进来;而直接把整池都加进去
    又会把之前测出来不可用、已被踢掉的引擎重新塞回去。所以按"是否评价过"来分:
    评价过的听自检的,没评价过的先给一次机会——不行的话本轮熔断 + 下次自检自然会踢掉。
    """
    from app.services import settings_service
    pool = all_engine_names()
    tuned = {x.strip() for x in str(getattr(settings, "prospect_engines_tuned", "") or "").split(",")
             if x.strip()}
    cur = [x.strip() for x in str(getattr(settings, "prospect_engines", "")).split(",") if x.strip()]
    fresh = [n for n in pool if n not in tuned and n not in cur]
    if not fresh:
        return {"added": [], "engines": cur}
    settings_service.save(db, {"prospect_engines": ",".join(cur + fresh)})
    return {"added": fresh, "engines": cur + fresh}


def autotune_engines(db, query: str | None = None, need_id: str | None = None) -> dict:
    """自动调优搜索引擎:测一遍候选池,把 prospect_engines 设为当前可用的那些。

    人不该每次都先点自检、再去设置页手改引擎列表。这里每轮自己测:
    - 掉线/被反爬的引擎自动踢出,不再白耗几十次请求;
    - 之前踢掉的下轮仍会重测,恢复了自动加回来(反爬是临时的,不能一棒子打死)。
    一个都不可用时保持原样不动——宁可空跑也不要把找源彻底关掉。
    """
    from app.services import settings_service
    pool = all_engine_names()
    prev = [x.strip() for x in str(getattr(settings, "prospect_engines", "")).split(",") if x.strip()]
    c = need_ctx.get(db, need_id or need_ctx.default_need_id())
    from app.services import keywords
    r = selftest(db, query or keywords.selftest_query_for(c), ctx=c)      # selftest 本身就测整池,不必再临时改配置
    usable = r["usable"]
    detail = {e["engine"]: ("可用" if e["ok"] else ("验证页/反爬" if e["blocked"] else e["hint"][:60]))
              for e in r["engines"]}
    out = {"tested": pool, "usable": usable, "before": prev, "detail": detail, "changed": False}
    # 记下"评价过哪些引擎":升级新加的引擎据此判定为"从没测过",才敢先给它一次机会,
    # 而不会把测出来不可用、已被踢掉的老引擎重新塞回去
    settings_service.save(db, {"prospect_engines_tuned": ",".join(sorted(pool))})
    if not usable:
        out["note"] = "所有引擎都不可用,保持原配置不动(下轮再测);建议开启浏览器渲染"
        return out
    if sorted(usable) != sorted(prev):
        settings_service.save(db, {"prospect_engines": ",".join(usable)})
        out["changed"] = True
        added, removed = sorted(set(usable) - set(prev)), sorted(set(prev) - set(usable))
        out["note"] = ("引擎列表已自动调整:"
                       + ("加回 " + "、".join(added) + ";" if added else "")
                       + ("停用 " + "、".join(removed) if removed else "")).rstrip(";")
        from app.services import actions
        actions.record(db, "source.engines_tuned", out["note"],
                       detail={"usable": usable, "before": prev, "detail": detail})
    else:
        out["note"] = f"引擎无变化,当前可用:{'、'.join(usable)}"
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
        # 页面正常、但一条外链都抽不出来 = 结果块是 JS 注入的(必应网页版就是这样)。
        # 这一类既不是验证页也不是"结构变了",提示得说清楚,否则人只会反复去改选择器。
        from bs4 import BeautifulSoup as _BS
        soup = _BS(fr.html or "", "lxml")
        externals = [a["href"] for a in soup.find_all("a", href=True)
                     if a["href"].startswith("http")
                     and not getattr(eng, "_is_footer_link", lambda _u: False)(a["href"])]
        if not externals:
            ed.setdefault("hint", "页面正常但没有任何结果链接:结果块多半由 JS 注入,"
                                  "需开启浏览器渲染;必应可改用 bing_rss(纯 XML,不依赖 JS)")
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
        return "本轮没有找源词:检查画像 discovery_file(缺省 config/discovery_sec.yaml)的 source_search_queries 是否为空"
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
        if st.get("redirect_over_budget"):
            parts.append(f"{st['redirect_over_budget']} 条跳转链因还原配额被跳过"
                         "(设置页调大『跳转链还原上限』)")
        if st.get("already_source"):
            parts.append(f"{st['already_source']} 条来自已有的源"
                         + (f",已从中补到 {st['column_hints']} 个漏采栏目" if st.get("column_hints")
                            else ",说明这些词搜到的还是老渠道,该换更细的找源词或换引擎")
                         + (f";已自动 -site: 排除霸榜的 {st['excluded_sites']} 个域"
                            if st.get("excluded_sites") else ""))
        if st.get("boilerplate"):
            parts.append(f"{st['boilerplate']} 条是备案/举报页脚模板链")
        if st.get("redirect_unresolved"):
            parts.append(f"{st['redirect_unresolved']} 条卡在搜索引擎的跳转页没还原出真实站点"
                         "(搜狗/360 的跳转页要跑 JS,开设置页的『启用浏览器渲染/截图』才拿得到)")
        if st.get("blacklisted"):
            parts.append(f"{st['blacklisted']} 条已拉黑")
        if st.get("engine_self"):
            parts.append(f"{st['engine_self']} 条指向搜索引擎自身")
        if st.get("wechat_unresolved"):
            parts.append(f"{st['wechat_unresolved']} 条公众号文章解析不出所属号(公众号页需浏览器渲染)")
        if st.get("non_chinese"):
            parts.append(f"{st['non_chinese']} 条是纯英文外站(跨语言噪声)")
        why = ";".join(parts) or "全部被去重/校验规则挡下"
        return f"抓到 {st.get('raw_items', 0)} 条结果,但没有新渠道:{why}"
    extra = []
    if st.get("redirect_over_budget"):
        extra.append(f"另有 {st['redirect_over_budget']} 条跳转链因还原配额被跳过"
                     "(设置页可调大『跳转链还原上限』)")
    if st.get("column_hints"):
        extra.append(f"顺带从已有源站点补到 {st['column_hints']} 个漏采栏目")
    if st.get("new_wechat"):
        extra.append(f"其中公众号 {st['new_wechat']} 个")
    if st.get("new_platform_account"):
        extra.append(f"平台号(百家号/微博){st['new_platform_account']} 个")
    return (f"抓到 {st.get('raw_items', 0)} 条结果,还原跳转链 {st.get('resolved', 0)} 条,"
            f"新增候选渠道 {len(r['new_keys'])} 个" + ("(" + "、".join(extra) + ")" if extra else ""))


# ---------------- 候选源 LLM 相关度初评 ----------------

_SITE_TITLES: dict[str, str] = {}     # 初评时顺手记下的站点标题,用作候选的展示名

# 候选源初评提示词来自画像(sources.prospect.probe_prompt,缺省按需求名与粗筛目标生成),见 NeedContext.probe_prompt


def _wechat_titles(account: str, limit: int) -> list[str]:
    """公众号没有可抓的首页 → 用搜狗按号检索取该号最近文章标题作为初评样本。

    不做这一步,公众号候选就永远初评不了、永远拿不到相关度分,也就永远过不了单通道闸门
    ——而公众号恰恰是执法通报最集中的一类渠道。
    """
    try:
        from app.services.adapters import SogouWechatAdapter

        class _S:
            adapter_config = {"account": account, "render": "auto"}
            name, entry_url, kind = "probe", None, "query"

        items = SogouWechatAdapter(_S()).search_page("", 0) or []
        return [i.title for i in items if i.title][:limit]
    except Exception:  # noqa: BLE001
        return []


def _sample_titles(key: str, limit: int) -> list[str]:
    """抓候选站首页,取正文链接文字作为最近文章标题样本。"""
    if key.startswith("mp:"):
        return _wechat_titles(key[3:], limit)
    plat = url_tools.platform_entry_url(key)
    fr = fetcher.fetch(plat or f"https://{key}/", render="auto")
    if not fr.ok and not plat:
        fr = fetcher.fetch(f"http://{key}/")
    if not fr.ok:
        return []
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(fr.html or "", "lxml")
    t = soup.find("title")
    if t:
        _SITE_TITLES[key] = " ".join(t.get_text(" ", strip=True).split())[:80]
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


def probe_one(db, key: str, force: bool = False, ctx=None) -> SourceProbe | None:
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
                _ctx(ctx).probe_prompt, f"站点:{key}\n最近文章标题:\n" + "\n".join(f"- {t}" for t in titles))
            rel = max(0.0, min(1.0, float(out.get("relevance") or 0)))
            reason = str(out.get("reason") or "")[:300]
        except Exception as e:  # noqa: BLE001 评不了不该阻断,记为未初评
            ok, reason = False, f"初评失败:{type(e).__name__}"
    if row is None:
        row = SourceProbe(identity_key=key)
        db.add(row)
    row.relevance, row.reason, row.ok = rel, reason, ok
    row.sample_titles, row.probed_at = titles[:12], datetime.utcnow()
    if _SITE_TITLES.get(key):
        row.site_title = _SITE_TITLES.pop(key)      # 渠道名,给候选池当展示名
    db.flush()
    return row


def _noop_progress(*_a, **_k):
    pass


def probe_pending(db, need_id: str, on_progress=None) -> dict:
    """给还没初评(或初评过期)的候选域名补上 LLM 相关度分。"""
    ctx = need_ctx.get(db, need_id)
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
        probe_one(db, k, ctx=ctx)
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
                       "current": "", "unit": "", "need_id": need_id, "hits": 0, "new_candidates": 0,
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
                         on_progress=lambda d, t, c, m=None: _set(done=d, total=t, current=c,
                                                                 **(m or {})))
            _set(hits=r["hits"], new_candidates=len(r["new_keys"]), engines=r["engines"],
                 note=r.get("note", ""), stats=r.get("stats", {}),
                 query_yield=r.get("query_yield", []),
                 engine_detail=r.get("engine_detail", []), queries=r.get("queries", 0),
                 dropped_top=r.get("dropped_top", []), new_columns=r.get("new_columns", []),
                 new_keys=r.get("new_keys", []))
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
                # 注意:这里不能把 queries/engines 清零——那是本轮找源的成绩单,
                # 清零会让结束后的汇总永远显示"找源词 0 条"。进度分母改用 unit 单独控制。
                p = probe_pending(db, need_id,
                                  on_progress=lambda d, t, c: _set(done=d, total=t, current=c,
                                                                   unit=""))
                scores = llm_scores(db)
                _set(probed=p.get("probed", 0),
                     new_candidates_detail=[
                         {"key": k, "name": discovery.candidate_name(db, k),
                          "kind": ("公众号" if k.startswith("mp:") else
                                   "百家号" if k.startswith("bjh:") else
                                   "微博号" if k.startswith("wb:") else "网站"),
                          "llm_relevance": scores.get(k)}
                         for k in r["new_keys"]])

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
