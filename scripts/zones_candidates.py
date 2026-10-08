"""从「<省>环评单位.csv」生成开发区维表候选 refs/zones_<省>.csv。

「地区」列不是区县、也不是市本级/省级/国家的，就是开发区/园区标签（「本级开发区」「前湾新区」）。
每个 (城市, 标签) 一行，分配 zone_id。归属关系（合署 / 上级派出 / 下属部门）和挂哪个区县
需要逐个查门户、由需求方确认，这里一律留空、confirmed=否——没确认的不自己归。

文件已存在时只追加新标签，已有行（可能有人工填的归属）原样保留。

    .venv/bin/python scripts/zones_candidates.py --province 330000
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

from clean_jiangsu_eia import ZONE, ZONE_KIND, core  # noqa: E402

COLS = ["zone_id", "city_code", "city", "source_label", "kind", "zone_name", "zone_short", "zone_level",
        "unit_count", "record_count", "sample_unit", "admin_relation", "zone_admin_code", "district_short",
        "confirmed", "merge_group", "portal_url", "evidence"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--province", required=True)
    ap.add_argument("--suspect", default="", help="疑似外省标签，逗号分隔的 城市:标签，evidence 里注明")
    args = ap.parse_args()

    tree = json.loads((ROOT / "refs" / f"area_tree_{args.province}.json").read_text(encoding="utf-8"))["地区树"]
    code_of = {c["名称"]: c["编码"] for c in tree["市"]}
    districts = set()
    for c in tree["市"]:
        for d in c["区县"]:
            districts |= {(c["名称"], d["简称"]), (c["名称"], core(d["名称"])),
                          (c["名称"], re.sub(r"(市|区|县)$", "", d["简称"]))}
    units = list(csv.DictReader((ROOT / f"{tree['名称']}环评单位.csv").open(encoding="utf-8-sig")))
    suspect = {tuple(x.split(":", 1)) for x in args.suspect.split(",") if ":" in x}

    groups = collections.defaultdict(list)
    for u in units:
        if u["地区"] in ("市本级", "省级", "国家") or (u["城市"], u["地区"]) in districts:
            continue
        if not ZONE.search(u["地区"]):
            continue                      # 镇、街道一级（「南马镇」）不是开发区
        groups[(u["城市"], u["地区"])].append(u)

    f = ROOT / "refs" / f"zones_{args.province}.csv"
    old = list(csv.DictReader(f.open(encoding="utf-8-sig"))) if f.exists() else []
    have = {(r["city"], r["source_label"]) for r in old}
    seq = collections.Counter(r["city_code"] for r in old)
    new = []
    for (city, label), us in sorted(groups.items(), key=lambda kv: (code_of.get(kv[0][0], ""), kv[0][1])):
        if (city, label) in have:
            continue
        cc = code_of[city]
        seq[cc] += 1
        top = max(us, key=lambda u: int(u["记录数"]))
        m = ZONE.search(label)
        new.append({
            "zone_id": f"{cc}-Z{seq[cc]:02d}", "city_code": cc, "city": city, "source_label": label,
            "kind": ZONE_KIND.get(m.group(2), "") if m else "",
            "zone_name": "", "zone_short": "", "zone_level": "",
            "unit_count": len(us), "record_count": sum(int(u["记录数"]) for u in us),
            "sample_unit": top["单位"], "admin_relation": "", "zone_admin_code": "", "district_short": "",
            "confirmed": "否", "merge_group": "", "portal_url": "",
            "evidence": "疑似外省单位被标到本省（名称里的开发区不在本省）" if (city, label) in suspect else "",
        })
    with f.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(old + new)
    print(f"{f.name}：原有 {len(old)} 行，新增 {len(new)} 行；"
          f"覆盖 {sum(r['unit_count'] for r in new)} 个单位、{sum(r['record_count'] for r in new)} 条记录")


if __name__ == "__main__":
    main()
