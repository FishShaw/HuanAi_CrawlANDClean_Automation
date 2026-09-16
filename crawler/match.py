"""以项目为单位归并：同名（规范化后）或高度相似的项目行合为一个项目，按阶段优先级取报告 + 最新批复。

报告：批复公告附件 → 拟审批（含受理+拟审批合并）公示附件 → 受理公示附件兜底；S0 不取。
同阶段内按附件名版本（报批稿 → 送审稿 → 公示稿 → 未标）→ 日期新。规则见 classify.REPORT_RANK / VERSION_RULES。
告知承诺、辐射类项目整组不入 projects。
"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from . import classify


def similar(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 8 and short in long_ and len(short) / len(long_) >= 0.8:
        return True
    return len(short) / len(long_) >= 0.85 and SequenceMatcher(None, a, b).ratio() >= 0.92


def pick_report(reports: list[dict]) -> dict | None:
    """阶段优先级 → 版本 → 日期新（先按日期倒排，min 取并列里的第一个）。"""
    newest_first = sorted(reports, key=lambda a: (a["d"] or "", a["id"]), reverse=True)
    return min(newest_first, key=lambda a: (classify.REPORT_RANK[a["report_stage"]], a["version_rank"]), default=None)


def build(c: sqlite3.Connection, city: str) -> dict:
    rows = c.execute(
        "SELECT r.*, n.stage, COALESCE(n.pub_date, n.list_date) AS d, n.authority, n.district, n.source_id, n.title"
        " FROM project_rows r JOIN notices n ON n.id = r.notice_id"
        " WHERE n.stage IN ('S0','S1','S2','S3') AND length(r.name_key) >= 4").fetchall()

    canon: dict[str, str] = {}
    buckets: dict[str, list[str]] = defaultdict(list)
    for key in sorted({r["name_key"] for r in rows}, key=len, reverse=True):
        bucket = buckets[key[:4]]
        hit = next((k for k in bucket if similar(key, k)), None)
        canon[key] = hit or key
        if not hit:
            bucket.append(key)

    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[canon[r["name_key"]]].append(r)

    atts: dict[int, list] = defaultdict(list)
    for a in c.execute(
            "SELECT a.id, a.project_row_id, a.doc_type, a.name, n.stage, n.stage_reason, COALESCE(n.pub_date, n.list_date) AS d"
            " FROM attachments a JOIN notices n ON n.id = a.notice_id"
            " WHERE a.status='converted' AND a.doc_type IN ('report','approval') AND a.project_row_id IS NOT NULL"):
        a = dict(a)
        a["report_stage"] = classify.report_source_stage(a["stage"], a["stage_reason"])
        a["version_rank"], a["version"] = classify.version_label(a["name"])
        atts[a["project_row_id"]].append(a)

    c.execute("DELETE FROM projects")
    stats = {"projects": 0, "with_report": 0, "with_approval": 0, "both": 0, "out_of_scope": Counter(),
             "report_stage": Counter()}
    for key, rs in groups.items():
        scope = next((why for r in rs if (why := classify.out_of_scope(r["source_id"], r["title"]))), None)
        if scope:
            stats["out_of_scope"][scope] += 1
            continue
        group_atts = [a for r in rs for a in atts.get(r["id"], [])]
        reports = [a for a in group_atts if a["doc_type"] == "report" and a["report_stage"]]
        approvals = [a for a in group_atts if a["doc_type"] == "approval" and a["stage"] == "S3"]
        rep = pick_report(reports)
        apv = max(approvals, key=lambda a: a["d"] or "", default=None)
        latest = max(rs, key=lambda r: r["d"] or "")
        s3_rows = [r for r in rs if r["stage"] == "S3"]
        c.execute(
            "INSERT INTO projects(city,name_key,name,company,authority,district,report_att_id,report_stage,report_date,"
            "report_is_fallback,report_version,approval_att_id,approval_date,doc_no,stages,notice_ids)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (city, key, max((r["name"] for r in rs), key=len),
             next((r["company"] for r in rs if r["company"]), None),
             (s3_rows[0] if s3_rows else latest)["authority"],
             next((r["district"] for r in rs if r["district"]), None),
             rep["id"] if rep else None, rep["report_stage"] if rep else None, rep["d"] if rep else None,
             int(rep["report_stage"] in classify.FALLBACK_STAGES) if rep else None, rep["version"] if rep else None,
             apv["id"] if apv else None, apv["d"] if apv else None,
             next((r["doc_no"] for r in s3_rows if r["doc_no"]), None),
             ",".join(sorted({r["stage"] for r in rs})),
             ",".join(str(i) for i in sorted({r["notice_id"] for r in rs}))))
        stats["projects"] += 1
        stats["with_report"] += bool(rep)
        stats["with_approval"] += bool(apv)
        stats["both"] += bool(rep and apv)
        stats["report_stage"][rep["report_stage"] if rep else None] += 1
    c.commit()
    stats["out_of_scope"] = dict(stats["out_of_scope"])
    stats["report_stage"] = dict(stats["report_stage"])
    return stats
