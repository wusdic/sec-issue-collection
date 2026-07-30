"""源体检:后台批量试抓 + 可查询进度。

此前体检在浏览器里逐个同步请求,源多时要跑几十分钟、切页就断、也看不到进度。
改为后台线程执行,进度存在进程内(切页/刷新都能查),可取消。
"""
import threading
from datetime import datetime, timedelta

from app.config import settings
from app.db import SessionLocal
from app.models import Source

_lock = threading.Lock()
_state: dict = {"running": False}
_cancel = threading.Event()


# ---------------- 源健康判定(带冗余度,不轻易判死) ----------------

def _protected(src: Source) -> bool:
    """这些可信度等级的源永不自动停用:官方权威源本来就低频,误杀代价远大于留着空跑。"""
    keep = {c.strip() for c in str(getattr(settings, "auto_retire_protect_credibility", "")).split(",")}
    return bool(src.credibility and src.credibility in keep)


def _quiet_days(src: Source) -> int:
    """距上次成功产出多少天(从未成功过则按建源时间算)。"""
    ref = src.last_success_at or src.created_at
    if not ref:
        return 10 ** 6
    return max(0, (datetime.utcnow() - ref).days)


def register_success(db, src: Source):
    """本轮出数据了:清零失败计数并撤销"观察中"。"""
    src.fail_streak = 0
    src.last_success_at = datetime.utcnow()
    cfg = dict(src.adapter_config or {})
    if cfg.pop("watch_since", None) is not None or cfg.pop("auto_retired_at", None) is not None:
        src.adapter_config = cfg
    db.flush()


def convert_to_site_search(db, src: Source, retire_original: bool = True) -> dict:
    """把直连抓不到的页面型源改造成「站内检索」——借搜索引擎按 site:域名 抓它。

    搜索引擎的爬虫能渲染 JS、绕过部分反爬,故常能救回政务站这类直连抓不到的源。
    以前这一步只有人点按钮才会做:体检把源判死、然后就没人管了,一个本来能救回来的
    权威源就这么躺在停用列表里。现在自动停用时顺手做掉。
    返回 {ok, id, created, site, note};不适用时 ok=False。
    """
    from app.services import url_tools
    if src.kind != "page" or not (src.entry_url or "").startswith("http"):
        return {"ok": False, "note": "仅页面型且有入口链接的源可转站内检索"}
    domain = url_tools.identity_key_for(src.entry_url)
    if not domain or domain.startswith("mp:"):
        return {"ok": False, "note": "入口链接解析不出站点域名"}
    ident = f"site:{domain}"
    existing = db.query(Source).filter_by(identity_key=ident).one_or_none()
    if existing:
        if existing.lifecycle == "retired":
            existing.lifecycle = "active"
        # 该检索源可能是"根域源没定位到栏目"时自动建的挂靠源(经父源采集,不独立排期);
        # 父源要停掉改走它,必须解除挂靠,否则父源一停它就再也不会被采。
        ecfg = dict(existing.adapter_config or {})
        if retire_original and ecfg.pop("parent_site_id", None) is not None:
            existing.adapter_config = ecfg
        out = {"ok": True, "id": existing.id, "created": False, "site": domain}
    else:
        retry = Source(
            name=f"{src.name}·站内检索", entry_url=None, kind="query",
            adapter="baidu_search", adapter_config={"site": domain, "list_order": "relevance"},
            credibility=src.credibility, tier=src.tier, lifecycle="active",
            serves_needs=list(src.serves_needs or []), identity_key=ident, site_key=domain,
            discovered_from="search_retry",
            note=f"由页面型源「{src.name}」直连抓不到,改站内检索兜底")
        db.add(retry)
        db.flush()
        out = {"ok": True, "id": retry.id, "created": True, "site": domain}
    if retire_original:
        src.lifecycle = "retired"
    db.flush()
    out["note"] = f"已改走站内检索 site:{domain}"
    return out


def register_failure(db, src: Source, reason: str = "") -> dict:
    """本轮没出数据:累加失败计数,但**不轻易停用**。

    冗余度:没有哪个站天天出稿,"连续 N 次没产出"不等于源坏了。只有同时满足
    ① 连续失败达 source_auto_retire_fail_streak,且
    ② 距上次成功产出已超过 source_quiet_tolerance_days(默认 30 天)
    才自动停用;S1/S2 官方源无论如何都不自动停用。未达标的只标『观察中』,照常参与采集。
    返回 {retired, watching, fail_streak, quiet_days, note}。
    """
    src.fail_streak = (src.fail_streak or 0) + 1
    th = int(getattr(settings, "source_auto_retire_fail_streak", 0) or 0)
    tol = int(getattr(settings, "source_quiet_tolerance_days", 0) or 0)
    quiet = _quiet_days(src)
    cfg = dict(src.adapter_config or {})
    out = {"retired": False, "watching": False, "fail_streak": src.fail_streak,
           "quiet_days": quiet, "note": ""}
    if src.lifecycle not in ("active", "trial") or th <= 0 or src.fail_streak < th:
        db.flush()
        return out
    if _protected(src):
        cfg.setdefault("watch_since", datetime.utcnow().isoformat(timespec="seconds"))
        src.adapter_config = cfg
        out.update(watching=True,
                   note=f"连续 {src.fail_streak} 轮无产出,但 {src.credibility} 官方源不自动停用,转『观察中』")
        db.flush()
        return out
    if quiet < tol:
        cfg.setdefault("watch_since", datetime.utcnow().isoformat(timespec="seconds"))
        src.adapter_config = cfg
        out.update(watching=True,
                   note=f"连续 {src.fail_streak} 轮无产出,但距上次成功仅 {quiet} 天"
                        f"(容忍 {tol} 天),转『观察中』继续采,暂不停用")
        db.flush()
        return out
    src.lifecycle = "retired"
    cfg["auto_retired_at"] = datetime.utcnow().isoformat(timespec="seconds")
    cfg.pop("watch_since", None)
    src.adapter_config = cfg
    from app.services import actions
    actions.record(db, "source.auto_retire",
                   f"「{src.name}」连续 {src.fail_streak} 轮无产出且已 {quiet} 天没成功,自动停用",
                   need_id=(src.serves_needs or [None])[0], target=src.entry_url or src.name,
                   detail={"source_id": src.id, "fail_streak": src.fail_streak,
                           "quiet_days": quiet, "reason": reason[:200]})
    # 停用不该是终点:直连抓不到的页面源自动改走站内检索,借搜索引擎把它救回来。
    # 这一步以前只有人点「抓不到的页面源转站内检索」才会做。
    if getattr(settings, "auto_to_site_search", True):
        conv = convert_to_site_search(db, src, retire_original=True)
        if conv.get("ok"):
            out["converted"] = conv
            actions.record(db, "source.to_site_search",
                           f"「{src.name}」直连抓不到,自动改走站内检索 site:{conv['site']}",
                           need_id=(src.serves_needs or [None])[0],
                           target=src.entry_url or src.name, detail=conv)
    out.update(retired=True,
               note=f"连续 {src.fail_streak} 轮无产出且已 {quiet} 天没有成功产出,自动停用"
                    f"({int(getattr(settings, 'retired_recheck_days', 0) or 0)} 天后会自动复检)")
    db.flush()
    return out


def recheck_due(db, need_id: str) -> list[Source]:
    """到期该复检的自动停用源:误杀能自愈——隔 retired_recheck_days 天再试一次,能出数据就恢复。"""
    days = int(getattr(settings, "retired_recheck_days", 0) or 0)
    if days <= 0:
        return []
    cutoff = datetime.utcnow() - timedelta(days=days)
    out = []
    for s in db.query(Source).filter(Source.lifecycle == "retired").all():
        if need_id not in (s.serves_needs or []):
            continue
        cfg = s.adapter_config or {}
        if cfg.get("manually_retired"):
            continue                       # 人工停的尊重人工,不自动复活
        ts = cfg.get("auto_retired_at")
        if not ts:
            continue                       # 不是自动停的(或老数据没记时间),不动
        try:
            if datetime.fromisoformat(ts) <= cutoff:
                out.append(s)
        except ValueError:
            continue
    return out


def status() -> dict:
    with _lock:
        return dict(_state)


def cancel():
    _cancel.set()


def _set(**kw):
    with _lock:
        _state.update(kw)


def start(need_id: str) -> dict:
    """启动体检(幂等:已有任务在跑则直接返回其状态)。"""
    with _lock:
        if _state.get("running"):
            return dict(_state)
        _cancel.clear()
        _state.clear()
        _state.update({"running": True, "total": 0, "done": 0, "ok": 0, "fail": 0,
                       "retired": 0, "current": "", "need_id": need_id,
                       "started_at": datetime.utcnow().isoformat(timespec="seconds"),
                       "finished_at": None, "canceled": False, "results": []})
    threading.Thread(target=_run, args=(need_id,), daemon=True).start()
    return status()


def pick(db, need_id: str, limit: int | None = None, stale_days: int | None = None) -> list[Source]:
    """本轮该体检哪些源:生效源(可按"最久没测过"限量)+ 到期该复检的自动停用源。

    limit 让自动运维可以每轮只测一部分,摊到多天里跑完,不必一次几十分钟。
    """
    srcs = [s for s in db.query(Source).filter(Source.lifecycle.in_(["active", "trial"])).all()
            if need_id in (s.serves_needs or [])]
    if stale_days:
        cut = datetime.utcnow() - timedelta(days=stale_days)
        srcs = [s for s in srcs if not s.last_success_at or s.last_success_at < cut]
    srcs.sort(key=lambda s: (s.last_success_at is not None, s.last_success_at or datetime.min, s.id))
    if limit and limit > 0:
        srcs = srcs[:limit]
    return srcs + recheck_due(db, need_id)      # 误杀自愈:到期的自动停用源一并复检


def check_one(db, s: Source) -> dict:
    """体检一个源:试抓一次并把成败计入健康(带冗余度);能出数据的停用源自动恢复。"""
    from app.api.routes import test_fetch_source
    was_retired = s.lifecycle == "retired"
    r = test_fetch_source(s.id, q=None, mark=True, db=db, _=None)
    good = bool(r.get("ok")) and int(r.get("count") or 0) > 0
    revived = good and was_retired and s.lifecycle != "retired"
    if revived:
        from app.services import actions
        actions.record(db, "source.auto_revive",
                       f"「{s.name}」复检能出数据,自动恢复启用(此前被自动停用)",
                       need_id=(s.serves_needs or [None])[0], target=s.entry_url or s.name,
                       detail={"source_id": s.id, "count": r.get("count", 0)})
    return {"id": s.id, "name": s.name, "ok": good, "count": r.get("count", 0),
            "retired": bool(r.get("retired")), "revived": revived,
            "watching": bool(r.get("watching")),
            "hint": (r.get("health_note") or r.get("hint") or r.get("error")
                     or ("复检通过,已自动恢复" if revived else ""))}


def run_batch(db, need_id: str, limit: int | None = None, on_result=None,
              should_stop=None) -> dict:
    """同步跑一批体检(页面后台任务与自动运维共用这一份)。返回汇总计数与明细。"""
    srcs = pick(db, need_id, limit)
    out = {"total": len(srcs), "done": 0, "ok": 0, "fail": 0,
           "retired": 0, "revived": 0, "watching": 0, "results": []}
    for s in srcs:
        if should_stop and should_stop():
            out["canceled"] = True
            break
        try:
            r = check_one(db, s)
        except Exception as e:  # noqa: BLE001 单源异常不终止体检
            r = {"id": s.id, "name": s.name, "ok": False, "count": 0, "retired": False,
                 "revived": False, "watching": False, "hint": f"{type(e).__name__}: {e}"[:160]}
        out["ok" if r["ok"] else "fail"] += 1
        for k in ("retired", "revived", "watching"):
            if r.get(k):
                out[k] += 1
        out["results"].append(r)
        out["done"] += 1
        if on_result:
            on_result(r, out)
    return out


def _run(need_id: str):
    db = SessionLocal()
    try:
        srcs = pick(db, need_id)
        _set(total=len(srcs), rechecking=len(recheck_due(db, need_id)))

        def _tick(r, agg):
            with _lock:
                _state.update({k: agg[k] for k in ("done", "ok", "fail", "retired",
                                                   "revived", "watching")})
                _state["results"].append(r)
                _state["current"] = r["name"]

        res = run_batch(db, need_id, on_result=_tick, should_stop=_cancel.is_set)
        if res.get("canceled"):
            _set(canceled=True)
    except Exception as e:  # noqa: BLE001
        _set(error=str(e)[:300])
    finally:
        _set(running=False, current="",
             finished_at=datetime.utcnow().isoformat(timespec="seconds"))
        db.close()
def restore(db, source_id: int) -> Source:
    """恢复被(误)停用的源:重新启用并清零失败计数,避免刚恢复又被历史计数立刻停用。"""
    src = db.get(Source, source_id)
    if not src:
        return None
    src.lifecycle = "active"
    src.fail_streak = 0
    cfg = dict(src.adapter_config or {})
    cfg.pop("auto_merged", None)         # 人工恢复后不再被启动查重自动并掉
    cfg.pop("manually_retired", None)    # 恢复即撤销"人工停用",自动栏目发现可再纳入
    cfg["manually_restored"] = True
    src.adapter_config = cfg
    if src.note and "[自动查重" in src.note:
        src.note = src.note.replace(" [自动查重:并入同采集目标的源]", "") or None
    db.flush()
    return src
