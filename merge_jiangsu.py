#!/usr/bin/env python3
"""合并各市数据，做一次全省统一清洗，产出全省单位表。文件名叫 jiangsu 但不限江苏。

    python merge_jiangsu.py                          # 默认江苏（320000），读 raw/32*.json
    python merge_jiangsu.py --province 330000        # 换省：只读 raw/33*.json，输出「浙江省…」
    python merge_jiangsu.py --field export           # 受理/监督单位字段口径

输出（省名取自地区树）：
    <省>环评单位.csv      完整表，带「职能」列，备查
    <省>环评审批机构.csv  去掉「其他部门」的审批机构口径，对外交付用这份

**为什么不是把 by_city/ 下的 CSV 拼起来**：江苏省生态环境厅、生态环境部在每个市的列表里
都会出现，拼接会重复计数；「灌南县环境保护局」这类被站点错标到外市的记录，也要全省一起看
才能归到连云港。所以这里回到原始记录，把各市的记录并到一起重新清洗一遍。

分市 CSV 仍然有用：用来逐市核对，以及看某个市单独的删除清单。
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from clean_jiangsu_eia import MODULES, Places, clean, names_of, normalize

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "raw"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--province", default="320000", help="省编码，默认 320000（江苏）；按文件名前两位筛 raw/")
    p.add_argument("--raw-dir", type=Path, default=RAW_DIR, help=f"分市原始记录目录，默认 {RAW_DIR.name}/")
    p.add_argument("--field", choices=["proc", "export"], default="proc")
    p.add_argument("--output", type=Path, help="输出 CSV，默认按 --field 取名")
    p.add_argument("--module", type=int, help=f"只合并某个模块，默认全部。可选 {sorted(MODULES)}")
    args = p.parse_args()

    # raw/ 里可能同时放着几个省，只取本省的市（文件名是 <市编码>_<市名>.json）
    files = sorted(args.raw_dir.glob(f"{args.province[:2]}*.json"))
    if not files:
        raise SystemExit(f"{args.raw_dir} 下没有省编码 {args.province} 的原始记录，先跑 cities/crawl_*.py")

    tree = None
    records: list[list] = []
    per_city: Counter[str] = Counter()
    missing: list[str] = []
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        tree = tree or data["地区树"]
        rows = [r for r in data["记录"] if args.module is None or r[0] == args.module]
        records.extend(rows)
        per_city[f.stem] = len(rows)
        # 抓到一半中断的市要点出来，免得当成"这个市就这么多"
        done = {tuple(x) for x in data.get("已完成", [])}
        want = {(m, data["记录"][0][1]) for m in data.get("模块", [])} if data["记录"] else set()
        if want - done:
            missing.append(f.stem)
        for w in data.get("截断警告", []):
            print(f"警告（{f.stem}）：{w}")

    places = Places(tree)
    if tree["编码"] != args.province:
        raise SystemExit(f"原始记录的地区树是 {tree['名称']}（{tree['编码']}），和 --province {args.province} 不符")
    out_proc = HERE / f"{places.prov}环评单位.csv"
    out_export = HERE / f"{places.prov}环评单位_两字段对照.csv"
    out_approvers = HERE / f"{places.prov}环评审批机构.csv"
    proc_recs = [(r[2], r[4], r[1], r[5]) for r in records]
    if args.field == "proc":
        recs, label, out_path = proc_recs, "单位", args.output or out_proc
    else:
        from clean_jiangsu_eia import SPLIT
        recs = [(s.strip(), r[4], r[1], r[5]) for r in records
                for s in SPLIT.split(r[3] or "") if s.strip()]
        label, out_path = "受理/监督单位", args.output or out_export

    rows, dropped = clean(recs, places)
    columns = ["城市", "地区", label, "职能", "最新公告日期", "记录数", "合并的其他写法"]
    if args.field == "export":
        proc_names = {n for row in clean(proc_recs, places)[0] for n in names_of(row)}
        for row in rows:
            row["单位列是否出现"] = "是" if names_of(row) & proc_names else "否"
        columns.insert(5, "单位列是否出现")

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows({**row, label: row["单位"]} for row in rows)

    if args.field == "proc" and not args.output:
        # 审批机构口径：去掉只是发布渠道的其他部门（交通、水务、司法、人民政府等）。完整表仍保留全部单位备查
        approvers = [r for r in rows if r["职能"] != "其他部门"]
        with open(out_approvers, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(approvers)
        print(f"审批机构口径：{len(approvers)} 个单位（去掉其他部门 {len(rows) - len(approvers)} 个、"
              f"{sum(r['记录数'] for r in rows if r['职能'] == '其他部门')} 条）→ {out_approvers}")

    kept = sum(r["记录数"] for r in rows)
    empty = sum(1 for r in recs if not normalize(r[0]))
    bad = sum(s["count"] for s in dropped.values())
    print(f"\n{places.prov}：{len(files)} 个市文件、{len(records)} 条记录")
    for stem, n in sorted(per_city.items()):
        print(f"  {stem}: {n} 条")
    print(f"\n合并清洗：{len(recs)} 条 → 保留 {kept} 条、{len(rows)} 个单位、"
          f"{len({r['城市'] for r in rows})} 个地区（空 {empty} 条、删除 {bad} 条）→ {out_path}")
    for city in sorted({r["城市"] for r in rows}):
        print(f"  {city}: {sum(1 for r in rows if r['城市'] == city)} 个")
    by_reason: dict[str, Counter[str]] = {}
    for (name, city_code), s in dropped.items():
        reason = places.drop_reason(name, city_code, s["district"]) or "不规范"
        # 同一个写法可能来自多个市的查询，按写法合并计数
        by_reason.setdefault(reason, Counter())[name] += s["count"]
    for reason, items in by_reason.items():
        print(f"\n删除（{reason}）{sum(items.values())} 条、{len(items)} 种写法，条数前 15：")
        for n, c in items.most_common(15):
            print(f"  {n}  ({c}条)")

    if missing:
        print(f"\n注意：这些市的原始记录没抓完，结果不完整：{'、'.join(missing)}")
    done_cities = {r[1] for r in records}
    absent = [c["名称"] for c in tree["市"] if c["编码"] not in done_cities]
    if absent:
        print(f"注意：还没有数据的市：{'、'.join(absent)}")


if __name__ == "__main__":
    main()
