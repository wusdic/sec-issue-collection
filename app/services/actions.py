"""动作分类分级 + 高级别优先提示。

系统现在会自己做很多有后果的事(自动停用源、自动定级、自动入库新源、自动合并重复源…)。
全都平铺成日志等于没提示;全都弹窗又会淹没人。这里给每个动作定"类别 + 基础级别 + 升级规则":

分级(level)按 **是否碰红线 × 影响面 × 可逆性** 定:
  4 紧急 —— 碰发布红线(如自动给 S1,S1 能支撑"已确认"金额并允许发布),或大面积生效;
  3 重要 —— 改变了源库构成(停用/转正/淘汰/新源入库),该看一眼但通常无需干预;
  2 关注 —— 有变化但低风险(定位栏目、复检恢复、转观察中);
  1 一般 —— 例行动作,只记账不打扰。

影响面会自动升级:同一动作一次作用于 escalate_at 条以上时,级别 +1(如自动停用 1 个源是
"重要",一次停用 8 个就是"紧急"——那多半是系统或网络出了问题,不是这 8 个源同时坏了)。

高级别(≥ notify_level)且未确认的动作,在对应模块页面顶部优先提示,并可一键确认/撤销。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models import ActionLog

INFO, NOTICE, HIGH, CRITICAL = 1, 2, 3, 4
LEVEL_NAME = {INFO: "一般", NOTICE: "关注", HIGH: "重要", CRITICAL: "紧急"}
MODULE_NAME = {"sources": "数据源", "crawl": "采集", "events": "事件",
               "review": "复核", "config": "配置"}
NOTIFY_LEVEL = HIGH          # ≥ 此级别才在页面顶部优先提示


@dataclass(frozen=True)
class Spec:
    module: str
    level: int
    label: str
    escalate_at: int = 0      # 一次影响这么多条即升一级;0=不按影响面升级
    reversible: str = ""      # 怎么撤销(给人看的一句话)


CATALOG: dict[str, Spec] = {
    # ---- 数据源:改变源库构成的动作 ----
    "source.auto_promote_s1": Spec(
        "sources", CRITICAL, "自动定级为 S1 官方权威", 0,
        "在数据源页把该源改回 S4/S3 即可。S1 可支撑事件『已确认』金额并允许发布,请核对确为官方来源"),
    "source.auto_promote": Spec(
        "sources", HIGH, "试运行源自动转正", 3,
        "在数据源页调整可信度等级,或删除该源"),
    "source.auto_retire": Spec(
        "sources", HIGH, "源被自动停用", 5,
        "数据源页点『恢复启用』即可复原;到期体检也会自动复检恢复"),
    "source.auto_graded_out": Spec(
        "sources", HIGH, "试运行源因相关率过低被自动淘汰", 3,
        "数据源页点『恢复启用』;若规则太严可调 config/discovery.yaml 的 grading 阈值"),
    "source.auto_trial": Spec(
        "sources", HIGH, "新源自动入库试运行", 8,
        "数据源页删除该源,或在候选池拉黑该域名"),
    "source.auto_merge": Spec(
        "sources", HIGH, "重复源自动合并", 5,
        "被合并方转为停用未删除,数据源页点『恢复启用』可撤销"),
    "source.auto_revive": Spec(
        "sources", NOTICE, "停用源复检通过自动恢复", 0, "数据源页再次停用即可"),
    "source.auto_watch": Spec(
        "sources", NOTICE, "源连续无产出转为『观察中』", 10,
        "无需处理:仍照常采集,只是被盯着;连续无产出且长期沉默才会停用"),
    "source.auto_columns": Spec(
        "sources", NOTICE, "根域源自动定位到栏目", 0, "数据源页删除不想要的子栏目"),
    "source.manual_grade": Spec("sources", NOTICE, "人工定级", 0, ""),

    # ---- 采集 ----
    "crawl.job_failed": Spec("crawl", CRITICAL, "采集任务整批失败", 0,
                             "看错误原因后重跑;常见为写锁冲突(调小抓取并发数)"),
    "crawl.mass_failure": Spec("crawl", CRITICAL, "本轮大量源抓取失败", 0,
                               "多半是网络/代理问题而非源本身,检查后重跑"),
    "crawl.job_done": Spec("crawl", INFO, "采集完成", 0, ""),

    # ---- 事件 ----
    "event.money_confirmed": Spec(
        "events", CRITICAL, "生成含『已确认』金额的事件", 0,
        "复核台可改回 claimed/estimated;发布前必须有 S1/S2 来源支撑"),
    "event.auto_draft": Spec("events", INFO, "自动生成草稿事件", 0, ""),
}

_FALLBACK = Spec("sources", INFO, "系统动作", 0, "")


def spec(action: str) -> Spec:
    return CATALOG.get(action, _FALLBACK)


def grade(action: str, count: int = 1) -> int:
    """算出这次动作的级别:基础级别 + 影响面升级(封顶紧急)。"""
    sp = spec(action)
    lvl = sp.level
    if sp.escalate_at and count >= sp.escalate_at:
        lvl = min(CRITICAL, lvl + 1)
    return lvl


def record(db, action: str, title: str, *, need_id: str | None = None, detail: dict | None = None,
           target: str | None = None, count: int = 1, actor: str = "auto") -> ActionLog:
    """记一笔动作台账。不 commit(由调用方所在事务统一提交),失败不抛。"""
    sp = spec(action)
    row = ActionLog(need_id=need_id, module=sp.module, action=action,
                    level=grade(action, count), title=title[:500], detail=detail or {},
                    target=target, count=max(1, int(count or 1)), actor=actor,
                    reversible=sp.reversible or None)
    try:
        db.add(row)
        db.flush()
    except Exception:  # noqa: BLE001 记账失败绝不能影响主流程
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return row


def feed(db, need_id: str | None = None, module: str | None = None, min_level: int = 1,
         unacked: bool | None = None, days: int = 30, limit: int = 100) -> list[dict]:
    """动作台账查询(页面用)。默认近 30 天、按时间倒序。"""
    q = db.query(ActionLog).filter(ActionLog.at >= datetime.utcnow() - timedelta(days=days))
    if need_id:
        q = q.filter((ActionLog.need_id == need_id) | (ActionLog.need_id.is_(None)))
    if module:
        q = q.filter(ActionLog.module == module)
    if min_level > 1:
        q = q.filter(ActionLog.level >= min_level)
    if unacked is True:
        q = q.filter(ActionLog.ack_at.is_(None))
    elif unacked is False:
        q = q.filter(ActionLog.ack_at.isnot(None))
    rows = q.order_by(ActionLog.level.desc(), ActionLog.at.desc()).limit(limit).all()
    return [_as_dict(r) for r in rows]


def _as_dict(r: ActionLog) -> dict:
    return {"id": r.id, "module": r.module, "module_name": MODULE_NAME.get(r.module, r.module),
            "action": r.action, "label": spec(r.action).label,
            "level": r.level, "level_name": LEVEL_NAME.get(r.level, str(r.level)),
            "title": r.title, "detail": r.detail, "target": r.target, "count": r.count,
            "actor": r.actor, "reversible": r.reversible,
            "at": r.at.isoformat(timespec="seconds"),
            "acked": r.ack_at is not None,
            "ack_at": r.ack_at.isoformat(timespec="seconds") if r.ack_at else None}


def alerts(db, need_id: str | None = None, module: str | None = None, limit: int = 20) -> dict:
    """某模块的优先提示:高级别 + 未确认。页面顶部据此提示,没有就完全不打扰。"""
    rows = feed(db, need_id, module, min_level=NOTIFY_LEVEL, unacked=True, limit=limit)
    return {"count": len(rows), "critical": sum(1 for r in rows if r["level"] >= CRITICAL),
            "items": rows}


def summary(db, need_id: str | None = None, days: int = 7) -> dict:
    """全局提示汇总(仪表盘用):各模块有多少条高级别未确认动作。"""
    rows = feed(db, need_id, None, min_level=NOTIFY_LEVEL, unacked=True, days=days, limit=500)
    by_mod: dict[str, int] = {}
    for r in rows:
        by_mod[r["module"]] = by_mod.get(r["module"], 0) + 1
    return {"total": len(rows),
            "critical": sum(1 for r in rows if r["level"] >= CRITICAL),
            "by_module": [{"module": m, "module_name": MODULE_NAME.get(m, m), "count": n}
                          for m, n in sorted(by_mod.items(), key=lambda kv: -kv[1])],
            "top": rows[:5]}


def ack(db, ids: list[int], user_id: int | None = None) -> int:
    """确认已读(高级别动作看过就不再顶在页面上)。"""
    n = 0
    for row in db.query(ActionLog).filter(ActionLog.id.in_(ids or [])).all():
        if row.ack_at is None:
            row.ack_at, row.ack_by, n = datetime.utcnow(), user_id, n + 1
    db.flush()
    return n


def ack_all(db, need_id: str | None = None, module: str | None = None,
            user_id: int | None = None) -> int:
    ids = [r["id"] for r in feed(db, need_id, module, min_level=NOTIFY_LEVEL,
                                 unacked=True, limit=1000)]
    return ack(db, ids, user_id)
