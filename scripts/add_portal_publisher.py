"""把实爬到的「门户自称发文机关」写法挂到受理单位主数据表上，单独成列。

为什么不并进 name_variants：那一列的口径是青绿清洗合并出来的其他写法。
门户自称是另一个来源（公示详情页的「来源：」），两者混在一起这列就不可追溯了，
而且它们常常不一样——青绿记「扬州市仪征生态环境局」，门户自己写「仪征生态环境局」。

挂载靠 function_key（市|区域|职能），主数据里它 334 行唯一。门户写法按同一套
Places 规则算出同一个键，就落到那一行；算不出或落不到的单独列出来，不硬塞。

    .venv/bin/python scripts/add_portal_publisher.py          # 出计划
    .venv/bin/python scripts/add_portal_publisher.py --run    # 真写
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import resolve_notice as RN  # noqa: E402
from clean_jiangsu_eia import Places, normalize  # noqa: E402

MASTER = ROOT / "江苏省受理单位主数据.csv"
DATA = ROOT / "江苏省环评受理数据_样本.csv"
COL = "portal_publisher"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()

    tree = json.loads((ROOT / "refs" / "area_tree_320000.json").read_text(encoding="utf-8"))["地区树"]
    places = Places(tree)
    code_of = {c["名称"]: c["编码"] for c in tree["市"]}

    rows = list(csv.DictReader(MASTER.open(encoding="utf-8-sig")))
    hdr = list(rows[0])
    if COL not in hdr:
        hdr.append(COL)
    by_key = {r["function_key"]: r for r in rows}

    # 一种写法在不同市可能是不同单位（「高新区行政审批局」在南通、泰州、宿迁各是一家），
    # 所以按 (城市, 写法) 计数，不按写法
    seen: collections.Counter = collections.Counter()
    # 写法里没写地名时（太仓那 118 条只写「港区管委会」），用这个写法自己那批公告
    # 最常见的项目地区当线索——和 clean 的 area_of(district=...) 是同一条规则
    hint: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    for r in csv.DictReader(DATA.open(encoding="utf-8-sig")):
        if not r["发文机关"]:
            continue
        k = (r["城市"], normalize(r["发文机关"]))
        seen[k] += 1
        if r["项目地区"]:
            hint[k][r["项目地区"]] += 1

    hits: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    miss: list[tuple[str, str, int, str]] = []
    prov = {"编码": tree["编码"], "名称": tree["名称"], "简称": tree["简称"], "区县": []}
    for (city, name), n in seen.items():
        code = code_of.get(city, "")
        # 省厅栏目不属于任何一个市，area_of 拿省编码查不到市
        area = RN._area(places, name, prov) if not code else places.area_of(name, code, "")
        keys = []
        if area:
            keys.append(f"{area[0]}|{area[1]}|{places.function_of(name)}")
        for d, _ in hint[(city, name)].most_common(3):
            a2 = places.area_of(name, code, d)
            if a2:
                keys.append(f"{a2[0]}|{a2[1]}|{places.function_of(name)}")
        key = next((k for k in keys if k in by_key), "")
        if key:
            hits[key][name] += n
        elif keys:
            miss.append((city, name, n, f"主数据里没有这些键：{' 或 '.join(dict.fromkeys(keys))}"))
        else:
            miss.append((city, name, n, "认不出地区"))

    changed = 0
    for key, c in hits.items():
        v = " / ".join(f"{k}({n})" for k, n in c.most_common())
        r = by_key[key]
        if r.get(COL, "") != v:
            r[COL] = v
            changed += 1
    for r in rows:
        r.setdefault(COL, "")

    print(f"实爬写法 {len(seen)} 个 (城市,写法) 组合，挂上 {sum(len(c) for c in hits.values())} 个，"
          f"落到 {changed} 行；挂不上 {len(miss)} 个 / {sum(m[2] for m in miss)} 条")
    for city, name, n, why in sorted(miss, key=lambda x: -x[2]):
        print(f"  {n:>5}  {city} {name:<28} {why}")
    if not args.run:
        print("\n未写入。加 --run 才写。")
        return
    w = csv.DictWriter(MASTER.open("w", newline="", encoding="utf-8-sig"), fieldnames=hdr)
    w.writeheader()
    w.writerows(rows)
    print(f"已写回 {MASTER.name}")


if __name__ == "__main__":
    main()
