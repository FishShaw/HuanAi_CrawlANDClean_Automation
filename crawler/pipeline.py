"""列表入库、详情页解析入库（项目行 / 附件 / 百度网盘）。"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import adapters, classify, db, extract
from .http import FetchError, Fetcher

log = logging.getLogger("eia")
SKIP_STAGES = ("EXCLUDE", "IRRELEVANT")


def insert_items(c: sqlite3.Connection, src: dict, items: list[extract.ListItem]) -> int:
    new = 0
    for it in items:
        stage, reason = classify.stage(it.title, src.get("stage_hint"), require_keyword=src.get("mixed", False))
        if stage in SKIP_STAGES and re.search(r"(\.\.\.|…)$", it.title):
            stage, reason = "UNKNOWN", "列表标题被截断，待详情页确认"
        status = "skipped" if stage in SKIP_STAGES else "new"
        cur = c.execute(
            "INSERT OR IGNORE INTO notices(source_id,url,title,list_date,stage,stage_reason,authority,district,status,extra)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (src["id"], it.url, it.title, it.date, stage, reason,
             classify.authority_from_title(it.title) or src["authority"], classify.district_from_title(it.title),
             status, db.dumps(it.extra) if it.extra else None))
        new += cur.rowcount
    c.commit()
    return new


def _ext(url: str, name: str) -> str:
    for s in (unquote(urlsplit(url).path), name or ""):
        m = re.search(r"\.([A-Za-z0-9]{2,5})$", s)
        if m:
            return m.group(1).lower()
    return ""


def build_rows(d: extract.Detail, title: str, stage: str) -> list[dict]:
    rows: dict[str, dict] = {}

    def add(name, **kw):
        name = classify.clean(name)
        key = classify.name_key(name)
        dn = classify.doc_no(name)
        if len(key) < 4 or (dn and len(name) <= len(dn) + 2):  # 只有文号、没有项目名
            return
        row = rows.setdefault(key, {"name": name, "name_key": key, "links": []})
        for k, v in kw.items():
            if k == "links":
                row["links"] += v
            elif v and not row.get(k):
                row[k] = v

    for r in d.rows:
        file_name = r.get("file_name") or ""
        name = r.get("name") or classify.project_name_from_text(file_name) or file_name
        add(name, company=r.get("company"), location=r.get("location"), eia_org=r.get("eia_org"),
            doc_no=r.get("doc_no") or classify.doc_no(file_name), doc_date=r.get("doc_date"),
            file_name=file_name or None, links=r.get("links", []))
    if not rows:
        name = classify.project_name_from_text(title)
        if name:
            add(name, doc_no=classify.doc_no(title), links=[])
    if not rows and d.content_html:
        for name in classify.project_names_in_text(re.sub(r"<[^>]+>", " ", d.content_html)):
            add(name, links=[])
    if not rows:
        for url, fname in d.files:
            if classify.doc_type(fname, stage) in ("report", "approval"):
                name = classify.project_name_from_text(fname) or classify.project_name_from_attachment(fname)
                add(name, doc_no=classify.doc_no(fname), links=[(url, fname)])
    return list(rows.values())


def _assign(url: str, name: str, rows: list[dict]) -> int | None:
    for r in rows:
        if any(u == url for u, _ in r["links"]):
            return r["id"]
    if len(rows) == 1:
        return rows[0]["id"]
    dn = classify.doc_no(name)
    if dn:
        hits = [r["id"] for r in rows if r.get("doc_no") and classify.doc_no(r["doc_no"]) == dn]
        if len(hits) == 1:
            return hits[0]
    k = classify.name_key(classify.project_name_from_text(name) or classify.project_name_from_attachment(name))
    best, best_score = None, 0.0
    for r in rows:
        rk = r["name_key"]
        if k and (rk in k or k in rk):
            score = min(len(rk), len(k)) / max(len(rk), len(k))
            if score > best_score:
                best, best_score = r["id"], score
    if best:
        return best
    hits = [r["id"] for r in rows if len(core := classify.company_core(r.get("company"))) >= 3 and core[:3] in k]
    return hits[0] if len(hits) == 1 else None


def assign_all(files: list[tuple[str, str, str]], rows: list[dict]) -> dict[str, int | None]:
    """附件归属项目：链接在表格行里 → 名称包含 → 公司简称 → 数量一致时按顺序对应。"""
    result = {url: _assign(url, name, rows) for url, name, _ in files}
    wanted = [url for url, _, dt in files if dt in ("report", "approval")]
    unassigned = [u for u in wanted if result[u] is None]
    used = {result[u] for u in wanted if result[u]}
    free_rows = [r for r in rows if r["id"] not in used]
    if unassigned and len(unassigned) == len(free_rows):
        for u, r in zip(unassigned, free_rows):
            result[u] = r["id"]
    return result


def process_detail(c: sqlite3.Connection, fetcher: Fetcher, src: dict, notice: dict, files_dir: Path) -> None:
    adapter = adapters.get(src["adapter"])
    try:
        if hasattr(adapter, "fetch_detail"):
            d, raw = adapter.fetch_detail(src, fetcher, notice)
        else:
            html, raw = fetcher.get_text(notice["url"])
            d = extract.parse_detail(html, notice["url"], hint_title=notice["title"])
    except FetchError as e:
        c.execute("UPDATE notices SET status='error', error=? WHERE id=?", (str(e)[:500], notice["id"]))
        c.commit()
        log.warning("[详情] 失败 %s：%s", notice["url"], e)
        return

    list_title = notice["title"] or ""
    truncated = not list_title or bool(re.search(r"(\.\.\.|…)$", list_title))
    title = (d.title or list_title) if truncated else list_title
    stage, reason = classify.stage(title, src.get("stage_hint"), require_keyword=src.get("mixed", False))
    authority = classify.authority_from_title(title) or src["authority"]
    district = classify.district_from_title(title) or notice.get("district")
    status = "skipped" if stage in SKIP_STAGES else "fetched"

    with c:
        c.execute("UPDATE notices SET title=?, pub_date=?, stage=?, stage_reason=?, authority=?, district=?, status=?,"
                  " raw_path=?, error=NULL, fetched_at=datetime('now','localtime') WHERE id=?",
                  (title, d.pub_date or notice.get("list_date"), stage, reason, authority, district, status,
                   str(raw) if raw else None, notice["id"]))
        if status == "skipped":
            return
        c.execute("DELETE FROM project_rows WHERE notice_id=?", (notice["id"],))
        rows = build_rows(d, title, stage)
        for r in rows:
            cur = c.execute("INSERT INTO project_rows(notice_id,name,name_key,company,location,eia_org,doc_no,doc_date,file_name)"
                            " VALUES (?,?,?,?,?,?,?,?,?)",
                            (notice["id"], r["name"], r["name_key"], r.get("company"), r.get("location"),
                             r.get("eia_org"), r.get("doc_no"), r.get("doc_date"), r.get("file_name")))
            r["id"] = cur.lastrowid

        wanted = 0
        typed = [(url, name, classify.doc_type(name, stage)) for url, name in d.files]
        owner = assign_all(typed, rows)
        for url, name, dt in typed:
            extra = d.file_extra.get(url) if hasattr(d, "file_extra") else None
            c.execute("INSERT OR IGNORE INTO attachments(notice_id,project_row_id,kind,url,name,ext,doc_type,status,extra)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (notice["id"], owner[url], "file", url, name, _ext(url, name), dt,
                       "new" if dt in ("report", "approval") else "skipped", db.dumps(extra) if extra else None))
            wanted += dt in ("report", "approval")

        default_type = {"S3": "approval", "S0": "report", "S1": "report"}.get(stage)
        if d.images and not wanted and default_type:
            c.execute("INSERT OR IGNORE INTO attachments(notice_id,project_row_id,kind,url,name,ext,doc_type,status,extra)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (notice["id"], rows[0]["id"] if len(rows) == 1 else None, "images", notice["url"] + "#images",
                       f"{title}（正文图片）", "pdf", default_type, "new", db.dumps({"urls": d.images})))
            wanted += 1
        if stage == "S3" and not wanted and d.content_html:
            body = files_dir / "bodies" / f"{notice['id']}.html"
            body.parent.mkdir(parents=True, exist_ok=True)
            body.write_text(f'<html><head><meta charset="utf-8"><title>{title}</title></head><body><h2>{title}</h2>'
                            f"{d.content_html}</body></html>", encoding="utf-8")
            c.execute("INSERT OR IGNORE INTO attachments(notice_id,project_row_id,kind,url,name,ext,doc_type,status,path)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (notice["id"], rows[0]["id"] if len(rows) == 1 else None, "body", notice["url"] + "#body",
                       f"{title}（正文）", "html", "approval", "downloaded", str(body)))

        for link, code in d.baidu:
            c.execute("INSERT OR IGNORE INTO baidupan_links(notice_id,url,code) VALUES (?,?,?)", (notice["id"], link, code))
