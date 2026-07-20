"""运维 CLI:python -m app.cli <命令>

命令:
  init          建库 + 加载 sec_events 画像/词表/关键词/种子源 + 建默认账号
  run-daily     执行每日采集与处理(真实网络)
  demo          离线端到端演示(MockLLM,无网络):采集→抽取→复核→发布→回访→线索
  verify-archives  存档抽样校验
"""
import json
import sys

import typer

from app.db import SessionLocal, init_db

cli = typer.Typer(no_args_is_help=True)

NEED_ID = "sec_events"


@cli.command()
def init(with_users: bool = True):
    """初始化:建库、注册 sec_events 需求、导入词表/关键词/种子源、建默认账号。"""
    init_db()
    from app.auth import hash_password
    from app.models import AppUser
    from app.services import profiles

    db = SessionLocal()
    try:
        paths = profiles.default_sec_events_paths()
        cfg = profiles.load_profile_file(paths["profile"])
        np = profiles.register_need(db, cfg)
        profiles.load_dictionaries(db, np.id, paths["dictionaries"])
        profiles.load_keyword_set(db, np.id, paths["keywords"])
        n = profiles.load_seed_sources(db, np.id, paths["sources"])
        users = 0
        if with_users:
            for uname, role in [("admin", "admin"), ("editor1", "editor"),
                                ("reviewer1", "reviewer"), ("reviewer2", "reviewer"),
                                ("analyst1", "analyst")]:
                if not db.query(AppUser).filter_by(username=uname).one_or_none():
                    db.add(AppUser(username=uname, display_name=uname,
                                   password_hash=hash_password("ChangeMe!2026"), role=role))
                    users += 1
        db.commit()
        typer.echo(f"初始化完成: 需求={np.id} 新增源={n} 新增账号={users}(默认口令 ChangeMe!2026,请立即修改)")
    finally:
        db.close()


@cli.command("run-daily")
def run_daily(limit_sources: int = typer.Option(None, help="限制本轮源数(调试用)"),
              no_archive: bool = False):
    """每日主任务(真实网络):到期源抓取→处理→候选评分→线索刷新。"""
    from app.services.scheduler import run_daily as _run
    db = SessionLocal()
    try:
        stats = _run(db, NEED_ID, do_archive=not no_archive, limit_sources=limit_sources)
        typer.echo(json.dumps(stats, ensure_ascii=False, indent=1, default=str))
    finally:
        db.close()


@cli.command()
def demo():
    """离线端到端演示:注入样例文章,跑完整链路,打印每步结果。"""
    from datetime import datetime

    from app.models import NeedProfile, RawDocument, Source
    from app.services import dedup, profiles
    from app.services.events import PublishError
    from app.services.extraction import load_record_schema
    from app.services.followup import schedule_followups
    from app.services.leads import generate_leads
    from app.services.pipeline import process_document
    from app.services.review import approve
    from app.config import settings
    from app.models import AppUser

    db = SessionLocal()
    try:
        need = db.get(NeedProfile, NEED_ID)
        if need is None:
            typer.echo("先运行 init")
            raise typer.Exit(1)
        src = db.query(Source).filter_by(adapter="freebuf").first() or db.query(Source).first()
        demo_url = f"https://example.com/demo-incident-{datetime.utcnow():%Y%m%d%H%M%S}"
        article = (
            "某市第三人民医院遭勒索软件攻击,HIS 系统瘫痪超过 36 小时,门诊一度停诊。"
            "攻击者要求支付 200 万元赎金。医院表示未支付赎金,数据由备份恢复,"
            "但部分备份也被加密。初步判断入侵与某 VPN 设备未修补漏洞有关。"
            "目前监管部门已介入调查。"
        )
        doc = RawDocument(
            need_id=NEED_ID, source_id=src.id, url=demo_url,
            url_normalized=demo_url, final_url=demo_url,
            title="某三甲医院遭勒索攻击 系统瘫痪36小时", publisher=src.name,
            published_at=datetime.utcnow(), content_text=article, screen_status="pending",
        )
        db.add(doc)
        db.flush()
        dedup.assign_cluster(db, doc)

        typer.echo("== ① 粗筛+抽取 ==")
        result = process_document(db, need, doc)
        typer.echo(json.dumps({k: v for k, v in result.items() if k != "extraction"},
                              ensure_ascii=False, indent=1))
        event_id = result["event_id"]
        from app.models import Event
        ev = db.get(Event, event_id)
        ransom = (ev.payload.get("ransom") or {})
        typer.echo(f"赎金要求={ransom.get('demanded_amount')} / loss_L1 状态={ev.payload['loss_L1'].get('status')}"
                   "(要求≠损失 ✓)")

        typer.echo("== ② 复核发布(编辑提交→复核通过) ==")
        schema = load_record_schema((need.config["record_schemas"][0]).get("file")
                                    or str(settings.schema_dir / "event.schema.json"))
        reviewer = db.query(AppUser).filter_by(username="reviewer1").one()
        try:
            approve(db, event_id, reviewer.id, schema)
            typer.echo(f"发布成功: {event_id}")
        except PublishError as e:
            typer.echo(f"发布被红线阻断: {e}")

        typer.echo("== ③ 回访任务 ==")
        tasks = schedule_followups(db, ev)
        typer.echo(json.dumps([{"kind": t.kind, "due": str(t.due_date), "reason": t.reason}
                               for t in tasks], ensure_ascii=False, indent=1))

        typer.echo("== ④ 线索 ==")
        leads = generate_leads(db, ev)
        typer.echo(json.dumps([{"org": l.target_org, "score": l.score, "stage": l.window_stage,
                                "products": l.products} for l in leads], ensure_ascii=False, indent=1))
        db.commit()
        typer.echo("演示完成 ✓")
    finally:
        db.close()


@cli.command("verify-archives")
def verify_archives(sample: int = 20):
    from app.models import ArchiveManifest
    from app.services.archive import verify_snapshot
    from datetime import datetime
    db = SessionLocal()
    try:
        bad = 0
        rows = db.query(ArchiveManifest).limit(sample).all()
        for r in rows:
            ok = verify_snapshot(r)
            r.last_verified_at = datetime.utcnow()
            r.verify_ok = ok
            bad += 0 if ok else 1
        db.commit()
        typer.echo(f"抽检 {len(rows)} 个快照, 损坏 {bad} 个")
        if bad:
            raise typer.Exit(2)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(cli())
