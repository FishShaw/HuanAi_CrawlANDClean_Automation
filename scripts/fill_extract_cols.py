"""用实爬结果回填飞书「归口总结」表的抽取区四列：发文机关 / 审批地区 / 项目地区 / 是否辐射。

只填空格：这四列是人工和脚本共用的，已有内容一律不覆盖。
每行按「入口URL 的域名 + 覆盖范围里的区县」去实爬明细里切片——
表里一个入口常常对应十几行（南京市局那个混合栏目就摊在 12 行上），
所以不能整条入口的汇总值往每行灌，否则玄武那行会写上全市的项目地区。

    .venv/bin/python scripts/fill_extract_cols.py            # 出计划
    .venv/bin/python scripts/fill_extract_cols.py --run      # 真写
"""
from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "https://qcn0u9dor8f6.feishu.cn/wiki/RH7CwbOiBiCSsBkfvD6cscJ9nSH"
SHEET = "4c47f2"
DATA = ROOT / "江苏省环评受理数据_样本.csv"
# 抽取区四列在表里的位置
COLS = {"发文机关": "AG", "审批地区": "AH", "项目地区": "AI", "是否辐射": "AJ"}


def host(u: str) -> str:
    return u.split("//")[-1].split("/")[0].removeprefix("www.").lower()


def read_sheet() -> tuple[list[str], list[list[str]]]:
    out = subprocess.run(
        ["lark-cli", "sheets", "+csv-get", "--as", "user", "--url", URL,
         "--sheet-id", SHEET, "--range", "A1:AL204", "--include-row-prefix=false"],
        capture_output=True, text=True, check=True).stdout
    rows = list(csv.reader(io.StringIO(json.loads(out)["data"]["annotated_csv"])))
    return rows[0], rows[1:]


def tally(vals: list[str], top: int = 6) -> str:
    """把一组取值压成「值(条数)｜值(条数)」，空值不计。"""
    c = collections.Counter(v for v in vals if v)
    parts = [f"{k}({n})" for k, n in c.most_common(top)]
    if len(c) > top:
        parts.append(f"…共{len(c)}种")
    return "｜".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true", help="真的写回，默认只出计划")
    args = ap.parse_args()

    data = list(csv.DictReader(DATA.open(encoding="utf-8-sig")))
    # 先按入口URL精确分，再按域名兜底：省厅的辐射栏目和非辐射栏目同域不同栏，
    # 只按域名分会把两行写成一样的值
    by_entry: dict[str, list[dict]] = collections.defaultdict(list)
    by_host: dict[str, list[dict]] = collections.defaultdict(list)
    for r in data:
        by_entry[r["入口URL"]].append(r)
        by_host[host(r["公告URL"])].append(r)

    head, body = read_sheet()
    idx = {n.split("\n")[0]: i for i, n in enumerate(head)}
    c_city, c_scope, c_url = idx["二级筛选"], idx["覆盖范围"], idx["入口URL"]

    def cell(r: list[str], i: int) -> str:
        return r[i].strip() if len(r) > i else ""

    entry_rows = collections.Counter(
        cell(r, c_url) for r in body if cell(r, c_url))

    plan: dict[str, dict[int, str]] = {k: {} for k in COLS}
    city = ""
    skipped_no_data = 0
    for n, r in enumerate(body, start=2):
        city = cell(r, c_city) or city
        scope, entry = cell(r, c_scope), cell(r, c_url)
        if not entry:
            continue
        pool = by_entry.get(entry) or [x for x in by_host.get(host(entry), [])
                                        if x["城市"] == city]
        if not pool:
            skipped_no_data += 1
            continue
        # 市本级行是整条入口的汇总（覆盖范围里列了一串区县）；区县行只取自己那一片
        names = [s.strip() for s in scope.replace("｜", "|").split("|") if s.strip()]
        if len(names) > 1 or scope in ("", "市本级"):
            rows = pool
        else:
            key = names[0].removesuffix("区").removesuffix("县").removesuffix("市")
            rows = [x for x in pool if x["审批地区"] == key] or \
                   [x for x in pool if x["项目地区"] == key]
            # 「本级开发区」这类是相对指代，不是区县名，切不出片；改用 F 列的园区全称
            # 直接在建设地点原文里找——写着园区名就是园区的项目，属直接证据，不是推断
            if not rows:
                zone = cell(r, idx["园区名称"])
                zone = zone.removeprefix(city.removesuffix("市"))
                if len(zone) >= 4:
                    rows = [x for x in pool if zone in x["建设地点"]]
            if not rows and entry_rows[entry] == 1 and \
                    all(host(x["公告URL"]) == host(entry) for x in pool):
                # 入口只被这一行用，且抓到的公告都出自这个域名 —— 那它就是这个区县
                # 自己的门户，整栏都属于这一行。阜宁的公告发文机关写的是盐城市局、
                # 建设地点在附件里，按区县名一条也切不出来，但它们确实是阜宁的。
                # 必须限定「只被这一行用」：无锡市局那个入口摊在 9 行上，
                # 整池灌下去会把全市的数据写到新吴区那一行。
                rows = pool
            if not rows:
                skipped_no_data += 1
                continue
        vals = {
            "发文机关": tally([x["发文机关"] for x in rows]),
            "审批地区": tally([x["审批地区"] for x in rows]),
            "项目地区": tally([x["项目地区"] for x in rows], top=10),
            "是否辐射": tally([x["是否辐射"] for x in rows], top=2),
        }
        for name, v in vals.items():
            if v and not cell(r, idx[name]):
                plan[name][n] = v

    total = sum(len(v) for v in plan.values())
    for name, col in COLS.items():
        print(f"{name}({col}): 待填 {len(plan[name])} 格")
    print(f"合计 {total} 格；入口有行但明细里没对应数据、跳过 {skipped_no_data} 行")
    for n in sorted(plan["发文机关"])[:5]:
        print(f"  行{n}: 发文机关={plan['发文机关'][n][:50]} / 项目地区={plan['项目地区'].get(n, '')[:50]}")
    if not args.run:
        print("\n未写入。加 --run 才写。")
        return

    for name, col in COLS.items():
        for first, last, vals in runs(plan[name]):
            # allow-overwrite=false 是护栏：计划里只有空格，真撞上有内容的就该报错停下
            subprocess.run(
                ["lark-cli", "sheets", "+cells-set", "--as", "user", "--url", URL,
                 "--sheet-id", SHEET, "--range", f"{col}{first}:{col}{last}",
                 "--allow-overwrite=false",
                 "--cells", json.dumps([[{"value": v}] for v in vals], ensure_ascii=False)],
                capture_output=True, text=True, check=True)
        print(f"{name} 写入 {len(plan[name])} 格")


def runs(d: dict[int, str]):
    """把散落的行号压成连续段，少发几十次请求。"""
    out, cur = [], []
    for n in sorted(d):
        if cur and n == cur[-1] + 1:
            cur.append(n)
        else:
            if cur:
                out.append((cur[0], cur[-1], [d[i] for i in cur]))
            cur = [n]
    if cur:
        out.append((cur[0], cur[-1], [d[i] for i in cur]))
    return out


if __name__ == "__main__":
    sys.exit(main())
