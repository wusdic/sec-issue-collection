"""任务模式与参数库:任务 = 参数库引用 + 覆盖 + 节奏/状态;编译成画像后引擎零改动;提炼可把片段存回库。"""
import yaml

from app.services import need_ctx, profiles, tasklib


def test_merge_semantics():
    a = {"scope": {"regions": [{"value": "江苏省", "terms": ["江苏"]}], "x": 1}, "keywords": {"time_filters": ["近7天"]}}
    b = {"scope": {"regions": [{"value": "江苏省", "terms": ["江苏"]}, {"value": "浙江省", "terms": ["浙江"]}], "x": 2},
         "keywords": {"time_filters": ["近30天"]}}
    m = tasklib.merge(a, b)
    assert [r["value"] for r in m["scope"]["regions"]] == ["江苏省", "浙江省"]      # 列表追加去重
    assert m["scope"]["x"] == 2 and m["keywords"]["time_filters"] == ["近7天", "近30天"]
    assert tasklib.merge({"a": 1}, {"a": None}) == {"a": 1}                       # None 不覆盖


def test_library_lists_presets_with_usage():
    rows = tasklib.list_presets()
    ids = {r["id"] for r in rows}
    assert {"scope.region.jiangsu", "record.tender_notice", "schedule.hourly_30d", "outputs.local_library"} <= ids
    jiangsu = next(r for r in rows if r["id"] == "scope.region.jiangsu")
    assert jiangsu["kind"] == "scope" and "tender_watch" in jiangsu["used_by"] and jiangsu["keys"] == ["scope"]
    assert all(r["kind"] == "scope" for r in tasklib.list_presets(kind="scope"))


def test_task_compiles_to_profile_and_schedule_maps():
    cfg = tasklib.compile_task_id("tender_watch")
    assert cfg["need"]["id"] == "tender_watch" and cfg["need"]["timeliness_sla"] == "小时级"
    assert cfg["scope"]["time_window_days"] == 30 and cfg["scope"]["require_mention"] == ["regions"]
    assert cfg["keywords"]["query_budget_per_source_daily"] == 200
    assert [r["value"] for r in cfg["scope"]["regions"]] == ["江苏省"]
    assert cfg["record"]["id_prefix"] == "TND" and cfg["sources"]["credibility_levels"]["confirm_allowed"] == ["S1"]
    assert any(e["kind"] == "local_library" for e in cfg["outputs"]["exports"])
    assert cfg["task"]["status"] == "active" and cfg["compiled_from"]["use"][0] == "scope.region.jiangsu"
    assert profiles.validate_profile(cfg) == []
    c = need_ctx.from_config(cfg)
    assert c.id_prefix == "TND" and c.scope_values("regions") == ["江苏省"] and c.time_window_days == 30


def test_setup_task_registers_and_runs_pipeline(db):
    from app.models import KeywordSet, NeedProfile
    r = profiles.setup_need(db, "tender_watch")        # 没有 need_*.yaml → 自动走任务模式
    assert r["need_id"] == "tender_watch" and r["use"] and r["task"]["status"] == "active"
    assert db.query(KeywordSet).filter_by(need_id="tender_watch", is_active=True).first()
    np = db.get(NeedProfile, "tender_watch")
    assert tasklib.is_runnable(np.config)
    # 暂停后不进自动化
    np.config = {**np.config, "task": {**np.config["task"], "status": "paused"}}
    assert not tasklib.is_runnable(np.config)
    assert "tender_watch" not in profiles.active_need_ids(db, runnable_only=True)
    assert "tender_watch" in profiles.active_need_ids(db)


def test_is_runnable_respects_window():
    assert tasklib.is_runnable({"task": {"status": "active", "schedule": {"start": "2000-01-01", "end": "2999-01-01"}}})
    assert not tasklib.is_runnable({"task": {"status": "active", "schedule": {"end": "2000-01-01"}}})
    assert not tasklib.is_runnable({"task": {"status": "finished"}})
    assert tasklib.is_runnable({"need": {"id": "x"}})                  # 非任务模式画像视为可跑


def test_extract_preset_roundtrip(tmp_path, monkeypatch):
    cfg = tasklib.compile_task_id("tender_watch")          # 先用真实参数库编译,再把库指到临时目录
    monkeypatch.setattr(tasklib, "LIBRARY_DIR", tmp_path)
    path = tasklib.extract_preset(cfg, "scope.doc_types", "scope.doctypes.test", "scope", "测试文种",
                                  tags=["文种"], provenance={"from_task": "tender_watch"})
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert d["preset"]["id"] == "scope.doctypes.test" and d["preset"]["provenance"]["section"] == "scope.doc_types"
    assert d["body"]["scope"]["doc_types"] == cfg["scope"]["doc_types"]
    assert "scope.doctypes.test" in tasklib.library()
    try:
        tasklib.extract_preset(cfg, "scope.doc_types", "scope.doctypes.test", "scope", "x")
        assert False, "应拒绝覆盖"
    except FileExistsError:
        pass
    try:
        tasklib.extract_preset(cfg, "no.such.key", "x", "scope", "x")
        assert False
    except ValueError:
        pass


def test_missing_preset_is_reported():
    try:
        tasklib.compile_task({"task": {"id": "t"}, "use": ["scope.nope"]})
        assert False
    except KeyError as e:
        assert "scope.nope" in str(e)


def test_task_api_and_cli(db):
    from app.api import routes
    from app.models import AppUser
    user = db.query(AppUser).filter_by(role="admin").first()
    rows = routes.list_tasks(db=db, _=user)
    assert any(r["id"] == "tender_watch" for r in rows)
    lib = routes.library_list(kind="scope", tag=None, _=user)
    assert lib and all(r["kind"] == "scope" for r in lib)
    from typer.testing import CliRunner
    from app.cli import cli
    out = CliRunner().invoke(cli, ["library-list", "--kind", "schedule"]).output
    assert "schedule.hourly_30d" in out
