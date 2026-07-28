"""LLM 产出形态不可信时的健壮性 + 粗筛小模型生效。"""
from datetime import date

from app.config import settings
from app.services import dedup, llm, url_tools


# ---------------- 粗筛小模型真正生效 ----------------

def test_screen_model_actually_used(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai_compat")
    monkeypatch.setattr(settings, "llm_base_url", "http://x/v1")
    monkeypatch.setattr(settings, "llm_model", "big-extract")
    monkeypatch.setattr(settings, "llm_screen_model", "small-fast")
    llm.reset()
    assert llm.get_llm().model == "big-extract"
    assert llm.get_screen_llm().model == "small-fast"   # 页面配的粗筛模型不再是死配置
    llm.reset()


def test_screen_model_falls_back_to_extract_model(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai_compat")
    monkeypatch.setattr(settings, "llm_base_url", "http://x/v1")
    monkeypatch.setattr(settings, "llm_model", "only-model")
    monkeypatch.setattr(settings, "llm_screen_model", "")
    llm.reset()
    assert llm.get_screen_llm().model == "only-model"
    llm.reset()


# ---------------- 日期/嵌套字段容错 ----------------

def test_to_date_tolerates_llm_shapes():
    assert url_tools.to_date("2026-07-21") == date(2026, 7, 21)
    assert url_tools.to_date({"date": "2026-04"}) == date(2026, 4, 1)      # 月精度
    assert url_tools.to_date({"value": "2022-07-01"}) == date(2022, 7, 1)  # 异形键
    assert url_tools.to_date("2026-06-01T12:00:00") == date(2026, 6, 1)
    assert url_tools.to_date({"raw_text": "近期"}) is None
    assert url_tools.to_date("未披露") is None
    assert url_tools.to_date(None) is None


def test_dget_survives_wrong_shapes():
    assert url_tools.dget({"region": {"province": "广东"}}, "region", "province") == "广东"
    assert url_tools.dget({"region": "广东省"}, "region", "province") is None   # 字符串不再崩
    assert url_tools.dget({}, "a", "b") is None
    assert url_tools.dget(None, "a") is None
    assert url_tools.dget({"a": {"b": 0}}, "a", "b", default="x") == 0


def test_fingerprint_match_tolerates_bad_dates(db, need):
    """LLM 给纯字符串/月精度日期或字符串 region 时不再抛异常(此前会致整篇处理失败)。"""
    for payload in [
        {"org_name": "某行", "occurred_date": "2026-07-21", "attack_type": ["勒索软件"]},
        {"org_name": "某行", "occurred_date": {"date": "2026-04"}, "attack_type": ["勒索软件"]},
        {"org_name": "某行", "occurred_date": {"date": "2026-07-21"},
         "region": "广东省", "attack_type": ["勒索软件"]},          # region 是字符串
        {"org_name": "某行", "occurred_date": {"raw_text": "近期"}, "attack_type": []},
    ]:
        dedup.fingerprint_match(db, need.id, payload)   # 不抛异常即通过


def test_org_key_tolerates_string_region():
    assert dedup._org_key({"org_name": "某行", "region": "广东省"}) == "某行|"
    assert dedup._org_key({"org_name": "某行", "region": {"province": "广东"}}) == "某行|广东"
