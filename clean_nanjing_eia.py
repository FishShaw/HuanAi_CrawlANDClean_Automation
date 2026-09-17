#!/usr/bin/env python3
"""清洗：把 crawl_nanjing_eia.py 抓到的原始记录归并成单位清单。

    python clean_nanjing_eia.py                  # 受理单位列，写 南京市环评受理单位.csv
    python clean_nanjing_eia.py --field export   # 受理/监督单位字段，写两字段对照 CSV

两个字段的区别（2026-09-16 实测 7070 条）：processDepartment 是列表页「受理单位」列，
一条公告一个发布单位，唯一值 68 个；acceptanceMonitorDepartmentForExport 是受理/监督单位，
2576 条里是「市局,区局」这样的组合值，按分隔符拆开后唯一值 120 个，多出的部分是
水务、文物、消防、规划资源、人民政府等本不出现在发布单位列的机构。

--field export 会按 `,，、;；` 和换行拆分组合值，并多出一列标注该单位是否也出现在
受理单位列，便于两个口径对照。注意：拆分会切断括号内含顿号的机构名，这是该字段的固有问题。

删除的原始写法及原因打印到屏幕（不另外落盘）。

清洗规则：
    1. 标错地区：名称开头是南京 / 江苏以外的省市，删除。
    2. 不规范：公告原文代称（「我局(江宁)」）、截断残句、错字、结尾不是机构后缀的，删除。
    3. 写法不同：同一地区 + 同一职能的多种写法归并成一条，保留公告日期最新的规范写法，
       被合并掉的原文写进「合并的其他写法」列。
    4. 非环保部门（水务局、交通运输局等）保留。

按地区 + 职能归并只依据名称写法和公告日期，不代表机构法定更名。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
IN = HERE / "南京市环评受理_原始记录.json"
OUT_PROC = HERE / "南京市环评受理单位.csv"
OUT_EXPORT = HERE / "南京市环评受理单位_两字段对照.csv"
# acceptanceMonitorDepartmentForExport 的组合值分隔符
SPLIT = re.compile(r"[,，、;；\r\n]+")

# 允许出现的地区词：南京本级、11 个区、江北新区、两个开发区，以及跨江的长江航道机构。
DISTRICTS = ["玄武", "秦淮", "建邺", "鼓楼", "浦口", "栖霞", "雨花台", "江宁", "六合", "溧水", "高淳"]
# 顺序重要：开发区要先于所在区匹配，否则「江宁开发区」会被认成「江宁」。
AREA_RULES: list[tuple[str, str]] = [
    (r"江北新区", "江北新区"),
    (r"化学工业园区", "南京化学工业园区"),
    (r"江宁(经济技术)?开发区", "江宁开发区"),
    (r"(南京)?(经济技术开发区|经开区|开发区)", "南京经济技术开发区"),
    *[(rf"{d}", d) for d in DISTRICTS],
    (r"长江", "长江"),
    # 「环境保护部」「生态环境部」不带地名，单独认成国家级
    (r"中华人民共和国|国家|^环境保护部$|^生态环境部$", "国家"),
    (r"江苏", "江苏省"),
    (r"南京", "南京市"),
]
# 生态环境主管部门的各种写法
ECO = re.compile(r"(生态环境(局|厅|部|和水务局)|环境保护(局|部)|环保(局|厅|总局))")
# 行政审批 / 政务服务口径的机构
ADMIN = re.compile(r"(行政审批局|政务服务管理办公室|政务服务中心|数据局|管理委员会|管委会|市民中心)")
# 机构名合法结尾（「会」覆盖管委会 / 委员会）
SUFFIX = re.compile(r"(局|厅|部|委|会|中心|办公室|政府|院|科|处|所|队|组|站)$|\)$|）$")
# 内设科室，归并到所属单位
SECTION = re.compile(r"(环评科|固废科|审批科|行政审批服务科|环评管理科)$")
# 明显的错字 / 残句特征
TYPO = re.compile(r"(行审|环许|管审|政服环|部门$)")
# 地区前缀：形如「开封市」「安徽省」，用于识别标错地区的记录。
# 只认纯中文前缀，避免把「我局(市级)」这类代称误判成地区。
PLACE_PREFIX = re.compile(r"^([一-龥]{2,5}?)(省|市|自治区|特别行政区)")
# 南京各区也要算允许前缀，否则「溧水区市民中心生态环境局」会被
# 当成「溧水区市」这个不存在的地级市而误删。
ALLOWED_PREFIX = {"南京", "江苏", *DISTRICTS}
# 去掉地区词后剩下的部分作为「其他部门」的职能键
PLACE_TOKENS = re.compile(
    r"^(中华人民共和国|国家|江苏省|江苏|南京市|南京)|"
    rf"(江北新区|化学工业园区|江宁(经济技术)?开发区|(南京)?(经济技术开发区|经开区|开发区)|({'|'.join(DISTRICTS)})(区|县)?)"
)


def area_of(name: str) -> str | None:
    """名称里的地区，找不到返回 None。"""
    for pattern, area in AREA_RULES:
        if re.search(pattern, name):
            return area
    return None


def function_of(name: str) -> str:
    """职能键：生态环境口、行政审批口，或者具体的委办局名称。"""
    if ECO.search(name):
        return "生态环境"
    if ADMIN.search(name):
        return "行政审批"
    rest = PLACE_TOKENS.sub("", name)
    rest = PLACE_TOKENS.sub("", rest)
    return rest or name


def is_misplaced(name: str) -> bool:
    """标错地区：开头是南京 / 江苏以外的省市。"""
    m = PLACE_PREFIX.match(name)
    if not m:
        return False
    return re.sub(r"[区县]$", "", m.group(1)) not in ALLOWED_PREFIX


def is_irregular(name: str) -> bool:
    """不规范：代称、截断、错字、缺机构后缀。"""
    if not name or name in {"（空）", "(空)", "-", "无"}:
        return True
    if "我局" in name:  # 公告原文里的自称，如「我局(江宁)」
        return True
    if re.search(r"[;；]", name):  # 分隔符残留，如「江宁开发区;行政审批局」
        return True
    if not SUFFIX.search(name):  # 「江宁区」「宁经政服环许」这类不是机构名
        return True
    if area_of(name) is None:  # 「会行政审批局」「京市环保局」这类开头被截断
        return True
    if TYPO.search(name):  # 「江宁开发区行审审批局」这类错字
        return True
    return False


def drop_reason(name: str) -> str | None:
    if is_misplaced(name):
        return "标错地区"
    if is_irregular(name):
        return "不规范"
    return None


def clean(records: list[tuple[str, str]]) -> list[dict]:
    """records 是 (单位原文, 公告日期)，返回清洗归并后的单位列表。"""
    stats: dict[str, dict] = {}
    for raw, date in records:
        name = SECTION.sub("", (raw or "").strip())
        if not name or drop_reason(name):
            continue
        s = stats.setdefault(name, {"count": 0, "date": ""})
        s["count"] += 1
        s["date"] = max(s["date"], date or "")

    groups: dict[tuple, list[str]] = defaultdict(list)
    for name in stats:
        groups[(area_of(name), function_of(name))].append(name)

    out = []
    for names in groups.values():
        # 公告日期最新的写法优先；同日期比条数，再比名称长度。
        ranked = sorted(names, key=lambda n: (stats[n]["date"], stats[n]["count"], len(n)), reverse=True)
        out.append({
            "单位": ranked[0],
            "最新公告日期": max(stats[n]["date"] for n in names),
            "记录数": sum(stats[n]["count"] for n in names),
            # 机构名里本身可能有「、」，这里用斜杠分隔，避免歧义
            "合并的其他写法": " / ".join(ranked[1:]),
        })
    out.sort(key=lambda r: (-r["记录数"], r["单位"]))
    return out


def names_of(row: dict) -> set[str]:
    """一组里的全部写法：规范名 + 被合并掉的原文。"""
    return {row["单位"], *(n for n in row["合并的其他写法"].split(" / ") if n)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--field", choices=["proc", "export"], default="proc",
                   help="proc=受理单位列（默认）；export=受理/监督单位字段，拆分组合值并附两字段对照列")
    p.add_argument("--input", type=Path, default=IN, help=f"原始记录 JSON，默认 {IN.name}")
    p.add_argument("--output", type=Path, help="输出 CSV，默认按 --field 取名")
    args = p.parse_args()

    if not args.input.exists():
        sys.exit(f"没有 {args.input.name}，先运行 crawl_nanjing_eia.py")
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    # 每条记录是 [processDepartment, 组合单位字段, 公告日期]
    proc_pairs = [(r[0], r[-1]) for r in raw["记录"]]

    if args.field == "proc":
        records, label = proc_pairs, "受理单位"
        out_path = args.output or OUT_PROC
    else:
        records = [(s.strip(), r[-1]) for r in raw["记录"]
                   for s in SPLIT.split(r[1] or "") if s.strip()]
        label = "受理/监督单位"
        out_path = args.output or OUT_EXPORT
        if len(raw["记录"][0]) < 3:
            sys.exit("这份原始记录没有组合单位字段，重新运行 crawl_nanjing_eia.py")

    rows = clean(records)
    columns = [label, "最新公告日期", "记录数", "合并的其他写法"]
    if args.field == "export":
        # 标注该单位是否也出现在受理单位列，便于两个口径对照
        proc_names = {n for row in clean(proc_pairs) for n in names_of(row)}
        for row in rows:
            row["受理单位列是否出现"] = "是" if names_of(row) & proc_names else "否"
        columns.insert(3, "受理单位列是否出现")

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows({**row, label: row["单位"]} for row in rows)

    kept = sum(r["记录数"] for r in rows)
    print(f"原始 {len(records)} 条 → 保留 {kept} 条、{len(rows)} 个单位 → {out_path}")
    if args.field == "export":
        only_here = sum(r["受理单位列是否出现"] == "否" for r in rows)
        print(f"其中 {only_here} 个不出现在受理单位列")

    dropped: dict[str, list] = defaultdict(list)
    for name, _date in records:
        stripped = SECTION.sub("", (name or "").strip())
        reason = drop_reason(stripped) if stripped else "不规范"
        if reason:
            dropped[reason].append(name or "（空）")
    for reason, names in dropped.items():
        counts = {n: names.count(n) for n in dict.fromkeys(names)}
        print(f"\n删除（{reason}）{len(names)} 条：")
        for n, c in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n}  ({c}条)")


if __name__ == "__main__":
    main()
