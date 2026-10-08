"""用公示栏目的受理公告标题，给受理单位主数据补 portal_publisher 和 crawl_note 两列。

江苏这两列来自项目级实爬明细（add_portal_publisher.py / reconcile_master.py）。
没有项目级明细的省份，标题开头的发布机构同样是「门户自称」——来源是官网，不是青绿——
口径和江苏一致：单独成列，不并进 name_variants。

- portal_publisher：「写法(条数) / 写法(条数)」，按归并键（市|区域|职能）挂到主数据行
- crawl_note：entries/ 里「已核实」行的备注（核实依据）

    .venv/bin/python scripts/portal_publisher_from_channels.py --province 330000
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from build_entries_xlsx import load_entries  # noqa: E402
from clean_jiangsu_eia import Places, normalize  # noqa: E402
from entries_from_channels import PUBLISHER, titles_of  # noqa: E402
from report_stage_probe import stage_of  # noqa: E402


def paren(s: str) -> str:
    """按名称兜底匹配时统一括号：主数据里多是半角「(高新区)」，公告标题里是全角「（高新区）」。
    不改 clean_jiangsu_eia.normalize——那会改变江苏的归并结果。"""
    return s.replace("（", "(").replace("）", ")")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--province", required=True)
    ap.add_argument("--max-pages", type=int, default=40)
    args = ap.parse_args()

    tree = json.loads((ROOT / "refs" / f"area_tree_{args.province}.json").read_text(encoding="utf-8"))["地区树"]
    places = Places(tree)
    code_of = {c["名称"]: c["编码"] for c in tree["市"]}
    master_f = ROOT / f"{tree['名称']}受理单位主数据.csv"
    rows = list(csv.DictReader(master_f.open(encoding="utf-8-sig")))
    hdr = list(rows[0])
    for c in ("crawl_note", "portal_publisher"):
        if c not in hdr:
            hdr.append(c)
    by_key = {r["function_key"]: r for r in rows}
    # 归并键对不上时按名称兜底：标题里算出的开发区标签可能和清洗时（借区县字段）算出的不同，
    # 「台州市生态环境局台州湾新区（高新区）分局」48 条就是这样漏的
    by_name = {}
    for r in rows:
        for n in [r["name_from_source"], *(r["name_variants"] or "").split(" / ")]:
            if n.strip():
                by_name.setdefault(paren(normalize(n.strip())), r["function_key"])

    tally: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    miss = collections.Counter()
    for line in (ROOT / "refs" / f"channels_{args.province}.tsv").open(encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        city, stage, url, _home = line.rstrip("\n").split("\t")
        if stage not in ("受理", "混合"):
            continue
        for t in titles_of(url, args.max_pages):
            if stage_of(t) != "受理":
                continue
            # 省厅老标题开头带日期（「2016年7月1日省环保厅关于…」），还有零宽空格
            t = re.sub(r"^[\u200b\ufeff\s]*(?:\d{4}年\d{1,2}月\d{1,2}日)?", "", t)
            m = PUBLISHER.match(t.strip())
            if not m:
                continue                 # 标题不带发布机构，没有「自称」可记
            raw = m.group(1)
            name = normalize(raw)
            if city == "省级":
                # 省厅栏目不属于任何一个市；「省环保厅」这种简称也只可能是本省省厅
                k = f"—|省级|{places.function_of(name)}"
            else:
                a = places.area_of(name, code_of[city], "")
                k = f"{a[0]}|{a[1]}|{places.function_of(name)}" if a else ""
            if k not in by_key:
                k = by_name.get(paren(name), k)
            if k in by_key:
                tally[k][raw] += 1
            else:
                miss[raw] += 1

    ent = load_entries()
    for r in rows:
        r.setdefault("crawl_note", "")
        r.setdefault("portal_publisher", "")
        c = tally.get(r["function_key"])
        if c:
            r["portal_publisher"] = " / ".join(f"{k}({n})" for k, n in c.most_common())
        e = ent.get((r["city_code"] and next((n for n, cc in code_of.items() if cc == r["city_code"]), "—")
                     or "—", r["name_from_source"]), {})
        if e.get("核验情况") == "已核实":
            r["crawl_note"] = e.get("备注", "")

    with master_f.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=hdr)
        w.writeheader()
        w.writerows(rows)
    print(f"portal_publisher 填了 {sum(1 for r in rows if r['portal_publisher'])} 行，"
          f"crawl_note {sum(1 for r in rows if r['crawl_note'])} 行；"
          f"挂不上主数据的发布机构写法 {len(miss)} 种 / {sum(miss.values())} 条")
    for k, n in miss.most_common(8):
        print(f"  {n:>4}  {k}")


if __name__ == "__main__":
    main()
