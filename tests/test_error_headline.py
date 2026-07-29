"""失败原因必须是"真正的原因"而不是栈头,并给出可操作建议。"""
from sqlalchemy.exc import OperationalError

from app.services.errors import error_headline


def test_headline_is_type_and_message():
    e = ValueError("boom\nsecond line")
    h = error_headline(e)
    assert h.startswith("ValueError: boom second line")   # 压掉换行,不含调用栈


def test_sqlite_locked_gets_actionable_hint():
    e = OperationalError("UPDATE x", {}, Exception("database is locked"))
    h = error_headline(e)
    assert "database is locked" in h and "抓取并发数" in h


def test_no_such_column_hint():
    e = OperationalError("SELECT x", {}, Exception("no such column: source.site_key"))
    h = error_headline(e)
    assert "no such column" in h and "重新启动" in h


def test_headline_respects_limit():
    assert len(error_headline(ValueError("x" * 900), 120)) == 120
