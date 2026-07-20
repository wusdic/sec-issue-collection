"""原文完整存档(方案 7.3 / M11):降级链 L-A → L-B → L-C → L-D。

存储:本地文件系统(ARCHIVE_ROOT),目录结构与 manifest 对齐 7.3;
生产切 MinIO 时替换 _write/verify_snapshot 的读写实现即可(S3 路径同构)。
"""
import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ArchiveManifest
from app.services import fetcher

_TEXT_TAGS_DROP = ["script", "style", "noscript"]
_ATTACH_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")


def _root() -> Path:
    p = Path(settings.archive_root)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write(dirpath: Path, name: str, data: bytes) -> str:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / name).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for t in soup(_TEXT_TAGS_DROP):
        t.decompose()
    text = soup.get_text("\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def archive_page(db: Session, url: str, fr: fetcher.FetchResult | None = None) -> ArchiveManifest:
    """执行存档降级链,返回 manifest 记录(已 add 未 commit)。"""
    now = datetime.utcnow()
    snapshot_id = f"{now:%Y%m%d}-{uuid.uuid4().hex[:12]}"
    rel_dir = Path(f"{now:%Y}") / f"{now:%m}" / snapshot_id
    dirpath = _root() / rel_dir
    files: dict[str, str] = {}  # 相对文件名 → sha256
    status, fail_reason = "L-D", None
    image_count = attachment_count = screenshot_count = 0
    has_full_text = False

    fr = fr or fetcher.fetch(url)
    final_url = fr.final_url or url

    if fr.ok:
        # ---- L-A: HTML + 图片/附件本地化 ----
        soup = BeautifulSoup(fr.html, "lxml")
        asset_fail = 0
        for i, img in enumerate(soup.find_all("img")):
            src = img.get("src") or img.get("data-src")
            if not src or src.startswith("data:"):
                continue
            if image_count >= settings.archive_max_assets:
                break
            abs_url = urljoin(final_url, src)
            data, err = fetcher.fetch_binary(abs_url, referer=final_url)  # 带 Referer 防盗链
            if data:
                ext = Path(urlparse(abs_url).path).suffix[:8] or ".img"
                name = f"assets/{i:03d}{ext}"
                files[name] = _write(dirpath / "assets", f"{i:03d}{ext}", data)
                img["src"] = name
                image_count += 1
            else:
                asset_fail += 1
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().endswith(_ATTACH_EXT) and attachment_count < settings.archive_max_assets:
                abs_url = urljoin(final_url, href)
                data, err = fetcher.fetch_binary(abs_url, referer=final_url)
                if data:
                    name = f"attachments/{attachment_count:03d}{Path(urlparse(abs_url).path).suffix}"
                    files[name] = _write(dirpath / "attachments",
                                         f"{attachment_count:03d}{Path(urlparse(abs_url).path).suffix}", data)
                    a["href"] = name
                    attachment_count += 1
        html_bytes = str(soup).encode("utf-8")
        files["page.html"] = _write(dirpath, "page.html", html_bytes)
        text = extract_text(fr.html)
        if text:
            files["text.txt"] = _write(dirpath, "text.txt", text.encode("utf-8"))
            has_full_text = True
        status = "L-A" if asset_fail == 0 or image_count > 0 or not soup.find_all("img") else "L-B"
    else:
        # ---- L-C: 截图兜底(需 Playwright) ----
        shots = fetcher.screenshot_pages(url)
        if shots:
            files["screenshots/full.png"] = _write(dirpath / "screenshots", "full.png", shots[0])
            for i, s in enumerate(shots[1:], 1):
                files[f"screenshots/part_{i:03d}.png"] = _write(dirpath / "screenshots", f"part_{i:03d}.png", s)
            screenshot_count = len(shots)
            status = "L-C"
        else:
            status, fail_reason = "L-D", fr.error or f"http {fr.status}"

    manifest = {
        "url": url, "final_url": final_url, "captured_at": now.isoformat(),
        "status": status, "files": files,
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
    manifest_sha = None
    if status != "L-D":
        manifest_sha = _write(dirpath, "manifest.json", manifest_bytes)

    rec = ArchiveManifest(
        snapshot_id=snapshot_id, status=status, captured_at=now, final_url=final_url,
        storage_path=str(rel_dir), has_full_text=has_full_text,
        image_count=image_count, attachment_count=attachment_count,
        screenshot_pages=screenshot_count, manifest_sha256=manifest_sha,
        fail_reason=fail_reason,
    )
    db.add(rec)
    db.flush()
    return rec


def verify_snapshot(rec: ArchiveManifest) -> bool:
    """月度抽检:manifest 中每个文件哈希复核。"""
    dirpath = _root() / rec.storage_path
    mf = dirpath / "manifest.json"
    if rec.status == "L-D":
        return True
    if not mf.exists():
        return False
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    for name, sha in manifest.get("files", {}).items():
        f = dirpath / name
        if not f.exists() or hashlib.sha256(f.read_bytes()).hexdigest() != sha:
            return False
    return True
