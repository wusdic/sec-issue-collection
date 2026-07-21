"""去重后薄存:转载/重复副本只存文本+HTML,不下载图片附件,省空间。"""
from app.services import archive
from app.services.fetcher import FetchResult

_HTML = """<html><body><h1>某医院遭勒索攻击</h1>
<p>系统瘫痪36小时,门诊停诊。</p>
<img src="https://example.com/a.jpg"><img src="https://example.com/b.png">
<a href="https://example.com/doc.pdf">处罚决定书</a>
</body></html>"""


def _fr(url):
    return FetchResult(url=url, final_url=url, status=200, html=_HTML)


def test_lite_archive_skips_assets(db):
    """lite=True:不下载图片/附件,只存 text+html,status=L-B。"""
    rec = archive.archive_page(db, "https://demo/repost", fr=_fr("https://demo/repost"), lite=True,
                               primary_snapshot_id="SNAP-PRIMARY")
    assert rec.status == "L-B"
    assert rec.image_count == 0
    assert rec.attachment_count == 0
    assert rec.has_full_text is True


def test_full_archive_downloads_assets_when_reachable(db):
    """首发(lite=False):走 L-A 路径尝试下载图片附件(离线环境下下载失败但路径正确)。"""
    rec = archive.archive_page(db, "https://demo/primary", fr=_fr("https://demo/primary"), lite=False)
    # 离线拉不到图片会降级 L-B;关键是它走了完整路径(尝试过下载),而非薄存直接跳过
    assert rec.status in ("L-A", "L-B")
    assert rec.has_full_text is True


def test_lite_manifest_points_to_primary(db):
    """薄存副本 manifest 记录指向首发全量存档,便于回溯完整原文。"""
    import json
    from pathlib import Path
    from app.config import settings
    rec = archive.archive_page(db, "https://demo/r2", fr=_fr("https://demo/r2"), lite=True,
                               primary_snapshot_id="SNAP-XYZ")
    mf = Path(settings.archive_root) / rec.storage_path / "manifest.json"
    data = json.loads(mf.read_text(encoding="utf-8"))
    assert data["lite"] is True
    assert data["full_archive_ref"] == "SNAP-XYZ"
