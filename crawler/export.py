"""导出：output/{市}/{审批机关}/{项目}/ 下的 PDF、项目总表、百度网盘清单、覆盖率报告。"""
from __future__ import annotations

import csv
import os
import re
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

from . import classify


def safe(name: str, limit: int = 80) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "未命名").strip(" .")
    return s[:limit] or "未命名"


def _place(src: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    stem, n = dest, 2
    while dest.exists():
        dest = stem.with_name(f"{stem.stem}_{n}{stem.suffix}")
        n += 1
    try:
        os.link(src, dest)
    except OSError:
        shutil.copyfile(src, dest)


def run(c: sqlite3.Connection, out_dir: Path, city: str) -> Path:
    city_dir = out_dir / city
    shutil.rmtree(city_dir / "按审批机关", ignore_errors=True)
    city_dir.mkdir(parents=True, exist_ok=True)
    att = {a["id"]: a for a in c.execute(
        "SELECT a.*, n.title AS notice_title, n.url AS notice_url FROM attachments a JOIN notices n ON n.id=a.notice_id"
        " WHERE a.status='converted'")}

    with open(city_dir / "projects.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["项目名称", "建设单位", "审批机关", "区县", "出现过的阶段", "报告阶段", "报告版本", "附件版本字样",
                    "报告为受理兜底", "报告日期", "报告PDF", "批复文号", "批复日期", "批复PDF", "报告来源公告", "批复来源公告"])
        for p in c.execute("SELECT * FROM projects ORDER BY COALESCE(approval_date, report_date) DESC"):
            authority = p["authority"] or "未知审批机关"
            if p["district"] and p["district"] not in authority:
                authority = f"{authority}（{p['district']}）"
            folder = city_dir / "按审批机关" / safe(authority, 60) / safe(p["name"])
            rep, apv = att.get(p["report_att_id"]), att.get(p["approval_att_id"])
            rep_path = apv_path = ""
            version = ""
            if rep:
                version = classify.report_version(p["report_stage"], rep["notice_title"] + (rep["name"] or ""))
                kind = classify.report_kind(rep["name"] or "", rep["notice_title"])
                dest = folder / f"{p['report_stage']}_{kind}_{version}_{p['report_date'] or '日期未知'}.pdf"
                _place(rep["pdf_path"], dest)
                rep_path = str(dest.relative_to(city_dir))
            if apv:
                label = safe(p["doc_no"] or p["approval_date"] or "日期未知", 40)
                dest = folder / f"S3_批复_{label}.pdf"
                _place(apv["pdf_path"], dest)
                apv_path = str(dest.relative_to(city_dir))
            w.writerow([p["name"], p["company"], authority, p["district"], p["stages"], p["report_stage"], version,
                        p["report_version"] or "", {1: "是", 0: "否"}.get(p["report_is_fallback"], ""),
                        p["report_date"], rep_path, p["doc_no"], p["approval_date"], apv_path,
                        rep["notice_url"] if rep else "", apv["notice_url"] if apv else ""])

    with open(city_dir / "baidupan_links.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["公告标题", "阶段", "发布日期", "网盘链接", "提取码", "公告链接"])
        for r in c.execute("SELECT n.title, n.stage, COALESCE(n.pub_date,n.list_date) d, b.url, b.code, n.url nurl"
                           " FROM baidupan_links b JOIN notices n ON n.id=b.notice_id ORDER BY d DESC"):
            w.writerow([r["title"], r["stage"], r["d"], r["url"], r["code"], r["nurl"]])
    return city_dir


def coverage(c: sqlite3.Connection, out_dir: Path, city: str, sources: dict) -> str:
    lines = [f"# {city} 采集覆盖率", ""]
    lines += ["## 各来源公告", "", "| 来源 | 公告数 | S0 | S1 | S2 | S3 | 排除/无关 | 未识别 | 详情失败 | 最早 | 最新 |", "|-|-|-|-|-|-|-|-|-|-|-|"]
    for sid, src in sources.items():
        r = c.execute(
            "SELECT count(*) n, sum(stage='S0') s0, sum(stage='S1') s1, sum(stage='S2') s2, sum(stage='S3') s3,"
            " sum(stage IN ('EXCLUDE','IRRELEVANT')) ex, sum(stage='UNKNOWN') unk, sum(status='error') err,"
            " min(COALESCE(pub_date,list_date)) lo, max(COALESCE(pub_date,list_date)) hi FROM notices WHERE source_id=?",
            (sid,)).fetchone()
        lines.append(f"| {src['name']} | {r['n']} | {r['s0'] or 0} | {r['s1'] or 0} | {r['s2'] or 0} | {r['s3'] or 0} |"
                     f" {r['ex'] or 0} | {r['unk'] or 0} | {r['err'] or 0} | {r['lo'] or ''} | {r['hi'] or ''} |")

    lines += ["", "## 附件状态", "", "| 类型 | 状态 | 数量 |", "|-|-|-|"]
    for r in c.execute("SELECT doc_type, status, count(*) n FROM attachments GROUP BY 1,2 ORDER BY 1,2"):
        lines.append(f"| {r['doc_type']} | {r['status']} | {r['n']} |")

    lines += ["", "## 项目匹配（按最新公告年份）", "", "| 年份 | 项目数 | 有报告 | 有批复 | 两者都有 | 批复找到报告的比例 |", "|-|-|-|-|-|-|"]
    by_year = defaultdict(lambda: [0, 0, 0, 0])
    for p in c.execute("SELECT substr(COALESCE(approval_date, report_date, ''),1,4) y, report_att_id r, approval_att_id a FROM projects"):
        y = by_year[p["y"] or "未知"]
        y[0] += 1
        y[1] += bool(p["r"])
        y[2] += bool(p["a"])
        y[3] += bool(p["r"] and p["a"])
    for year in sorted(by_year, reverse=True):
        n, r, a, b = by_year[year]
        lines.append(f"| {year} | {n} | {r} | {a} | {b} | {(b / a * 100 if a else 0):.0f}% |")

    lines += ["", "## 报告来源阶段（S3 批复公告 → S2/S1S2 拟审批·合并公示 → S1 受理兜底）", "",
              "| 报告阶段 | 版本字样 | 项目数 |", "|-|-|-|"]
    for r in c.execute("SELECT report_stage s, report_version v, count(*) n FROM projects WHERE report_att_id IS NOT NULL"
                       " GROUP BY 1,2 ORDER BY CASE s WHEN 'S3' THEN 1 WHEN 'S2' THEN 2 WHEN 'S1S2' THEN 3 ELSE 4 END, 2"):
        lines.append(f"| {r['s']} | {r['v']} | {r['n']} |")

    unknown = c.execute("SELECT title, url FROM notices WHERE stage='UNKNOWN' LIMIT 30").fetchall()
    if unknown:
        lines += ["", "## 规则未识别的标题（需补规则或人工看）", ""] + [f"- [{u['title']}]({u['url']})" for u in unknown]
    text = "\n".join(lines) + "\n"
    (out_dir / city).mkdir(parents=True, exist_ok=True)
    (out_dir / city / "coverage.md").write_text(text, encoding="utf-8")
    return text
