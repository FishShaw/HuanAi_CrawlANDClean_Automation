"""下载附件：按文件头识别真实类型，按 sha256 去重存放；网页冒充文件视为失败（通常是文件已下架）。"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

from . import adapters, db
from .http import FetchError, Fetcher

log = logging.getLogger("eia")

MAGIC = [(b"%PDF", "pdf"), (b"PK\x03\x04", "zip"), (b"\xd0\xcf\x11\xe0", "ole"), (b"Rar!", "rar"),
         (b"7z\xbc\xaf", "7z"), (b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpg"), (b"{\\rtf", "rtf")]


def sniff(path: Path, url_ext: str) -> str:
    with open(path, "rb") as f:
        head = f.read(512)
    for magic, kind in MAGIC:
        if head.startswith(magic):
            if kind == "zip":
                return {"doc": "docx", "docx": "docx", "xls": "xlsx", "xlsx": "xlsx", "ofd": "ofd"}.get(url_ext, "zip")
            if kind == "ole":
                return url_ext if url_ext in ("doc", "xls", "wps", "ppt") else "doc"
            return kind
    low = head.lstrip().lower()
    if low.startswith((b"<!doctype", b"<html", b"<head", b"<script", b"<?xml", b"{\"")):
        return "html"
    return url_ext or "bin"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def store(tmp: Path, files_dir: Path, ext: str) -> tuple[str, Path]:
    sha = sha256_of(tmp)
    final = files_dir / sha[:2] / f"{sha}.{ext}"
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        tmp.unlink()
    else:
        tmp.replace(final)
    return sha, final


def download_one(c: sqlite3.Connection, fetcher: Fetcher, att: dict, files_dir: Path) -> None:
    extra = db.loads(att.get("extra"))
    notice_url = c.execute("SELECT url FROM notices WHERE id=?", (att["notice_id"],)).fetchone()[0]
    headers = {"Referer": notice_url} if notice_url.startswith("http") else None
    try:
        if att["kind"] == "images":
            paths = []
            for i, u in enumerate(extra["urls"]):
                tmp = files_dir / "tmp" / f"{att['id']}_{i}"
                fetcher.download(u, tmp, headers)
                ext = sniff(tmp, "")
                if ext not in ("png", "jpg"):
                    tmp.unlink(missing_ok=True)
                    continue
                paths.append(str(store(tmp, files_dir, ext)[1]))
            if not paths:
                raise FetchError("正文图片均无法下载")
            extra["paths"] = paths
            c.execute("UPDATE attachments SET status='downloaded', extra=?, error=NULL WHERE id=?", (db.dumps(extra), att["id"]))
        else:
            tmp = files_dir / "tmp" / f"{att['id']}.part_dl"
            if extra.get("adapter"):
                content_type, size = adapters.get(extra["adapter"]).download(fetcher, att, extra, tmp)
            else:
                content_type, size = fetcher.download(att["url"], tmp, headers)
            ext = sniff(tmp, att.get("ext") or "")
            if ext == "html":
                tmp.unlink(missing_ok=True)
                raise FetchError("返回的是网页而不是文件（可能已下架或需要登录）")
            sha, final = store(tmp, files_dir, ext)
            c.execute("UPDATE attachments SET status='downloaded', ext=?, sha256=?, path=?, size=?, content_type=?, error=NULL"
                      " WHERE id=?", (ext, sha, str(final), size, content_type, att["id"]))
    except (FetchError, OSError, KeyError) as e:
        c.execute("UPDATE attachments SET status='failed', error=? WHERE id=?", (str(e)[:500], att["id"]))
        log.warning("[下载] 失败 #%s %s：%s", att["id"], att["url"], e)
    c.commit()
