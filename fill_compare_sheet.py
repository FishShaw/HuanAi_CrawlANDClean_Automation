#!/usr/bin/env python3
"""把清洗后的审批机构按行政区划代码填进飞书「<省>比对结果」子表（E–L 列），一单位一行。

    python fill_compare_sheet.py --url <飞书链接> --sheet-id <子表id>                  # 只做计划，不写
    python fill_compare_sheet.py --url <飞书链接> --sheet-id <子表id> --run            # 插行 + 写入 + 回读核对
    python fill_compare_sheet.py --url ... --sheet-id ... --province 330000            # 换省

表格约定（江苏试点那张表的格式）：第 1 行表头；A=行政区划代码，B=省，C=市，D=区县，每个代码一行；
E–G=青绿地区树里的省/市/区县简称，H=受理单位名称，I–L=首页 / 受理 / 拟审批 / 审批入口。

规则（2026-09-17 与用户逐条确认）：
- 单位来自 <省>环评审批机构.csv（已去掉交通、水务、人民政府等只是发布渠道的「其他部门」）。
- 「市本级」挂市那一行，「省级」挂省那一行；区县按地区树简称对上代码。
- 开发区 / 园区没有区县代码，默认不填、列进「无区县代码」清单交给用户看；
  用户确认过能对上区县的，写进 ZONE_DISTRICT 后才挂过去（如苏州高新区→虎丘）。
- 一个代码下多个单位时，在该行下方插行（继承样式），A–D 照抄，按记录数从多到少排；
  没有单位的代码只填 E–G。入口取 entries/*.csv，没查过的留空。
需要 lark-cli 已用 `--as user` 登录（写入用的是用户本人身份）。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY_COLS = ["首页入口", "受理公示入口", "审批前公示入口", "审批后意见入口"]

# 用户确认过的「开发区 → 所在区县」映射，按省编码分。键是清洗结果里的 (城市, 地区)。
ZONE_DISTRICT = {
    "320000": {
        ("苏州市", "本级高新区"): "虎丘", ("苏州市", "常熟高新区"): "常熟", ("苏州市", "常熟开发区"): "常熟",
        ("苏州市", "相城开发区"): "相城", ("苏州市", "太仓港区"): "太仓", ("苏州市", "太仓港开发区"): "太仓",
        ("常州市", "国家高新区"): "新北", ("常州市", "本级高新区"): "新北", ("常州市", "新北高新区"): "新北",
        ("常州市", "本级新区"): "新北", ("泰州市", "医药高新区"): "高港", ("泰州市", "高港港区"): "高港",
    },
}


def core(place: str) -> str:
    return re.sub(r"(省|市|区|县|自治区|自治县|自治州)$", "", place or "")


class Sheet:
    def __init__(self, url: str, sheet_id: str):
        self.url, self.sheet_id = url, sheet_id

    def cli(self, *args: str, payload: str | None = None) -> dict:
        p = subprocess.run(["lark-cli", "sheets", *args, "--as", "user", "--url", self.url,
                            "--sheet-id", self.sheet_id], input=payload, capture_output=True, text=True)
        res = json.loads(p.stdout or "{}")
        if not res.get("ok"):
            raise SystemExit(f"lark-cli {args[0]} 失败：{p.stdout[:500]} {p.stderr[:300]}")
        return res["data"]

    def read(self) -> list[list[str]]:
        data = self.cli("+csv-get", "--range", "A1:L5000")
        text = re.sub(r"(^|\n)\[row=\d+\] ", r"\1", data["annotated_csv"])
        rows = [(r + [""] * 12)[:12] for r in csv.reader(io.StringIO(text))]
        while rows and not any(rows[-1]):
            rows.pop()
        return rows


def load_tree(province: str, raw_dir: Path | None = None) -> dict:
    raw_dir = raw_dir or HERE / "raw"
    for f in [*sorted(raw_dir.glob(f"{province[:2]}*.json")), *sorted((HERE / "trees").glob(f"{province}_*.json"))]:
        tree = json.loads(f.read_text(encoding="utf-8"))["地区树"]
        if tree["编码"] == province:
            return tree
    raise SystemExit(f"raw/ 和 trees/ 里都没有省编码 {province} 的地区树")


def load_entries() -> dict[tuple[str, str], dict]:
    out = {}
    for f in sorted((HERE / "entries").glob("*.csv")):
        city = f.stem.split("_", 1)[1]
        city = "—" if city == "省级及国家" else city
        with open(f, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                out[(city, r["单位"])] = r
    return out


def plan(sheet_rows: list[list[str]], province: str, raw_dir: Path | None = None):
    tree = load_tree(province, raw_dir)
    code_names = {tree["编码"]: (tree["简称"], "", "")}
    unit_code: dict[tuple[str, str], str] = {("—", "省级"): tree["编码"]}
    for c in tree["市"]:
        code_names[c["编码"]] = (tree["简称"], c["简称"], "")
        unit_code[(c["名称"], "市本级")] = c["编码"]
        for d in c["区县"]:
            code_names[d["编码"]] = (tree["简称"], c["简称"], d["简称"])
            unit_code[(c["名称"], core(d["名称"]))] = d["编码"]
            unit_code[(c["名称"], d["简称"])] = d["编码"]
    zone_map = ZONE_DISTRICT.get(province, {})

    approvers = HERE / f"{tree['名称']}环评审批机构.csv"
    if not approvers.exists():
        raise SystemExit(f"没有 {approvers.name}，先跑 merge_jiangsu.py --province {province}")
    by_code: dict[str, list[dict]] = {}
    unmatched = []
    with open(approvers, encoding="utf-8-sig") as fh:
        approver_rows = list(csv.DictReader(fh))
    for u in approver_rows:
        area = zone_map.get((u["城市"], u["地区"]), u["地区"])
        code = unit_code.get((u["城市"], area))
        if code:
            by_code.setdefault(code, []).append(u)
        else:
            unmatched.append(u)

    entries = load_entries()
    codes, seen = [], set()
    for r in sheet_rows[1:]:
        if r[0] and r[0] not in seen:
            seen.add(r[0])
            codes.append((r[0], r[:4]))
    layout = []  # (是否新插的行, A–L)
    no_unit, not_in_tree = [], []
    for code, ad in codes:
        names = code_names.get(code)
        if names is None:
            not_in_tree.append(code)
            names = ("", "", "")
        units = sorted(by_code.pop(code, []), key=lambda u: -int(u["记录数"]))
        if not units:
            no_unit.append(f"{ad[2]}{ad[3]}")
            layout.append((False, [*ad, *names, "", "", "", "", ""]))
        for i, u in enumerate(units):
            e = entries.get((u["城市"], u["单位"]), {})
            layout.append((i > 0, [*ad, *names, u["单位"], *[(e.get(k) or "").strip() for k in ENTRY_COLS]]))
    # 地区树里有、表里没有这个代码的单位
    for units in by_code.values():
        unmatched.extend(units)
    return tree, codes, layout, unmatched, no_unit, not_in_tree


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="飞书电子表格或 wiki 链接")
    p.add_argument("--sheet-id", required=True, help="比对结果子表的 sheet_id（链接里 ?sheet= 后面那段）")
    p.add_argument("--province", default="320000")
    p.add_argument("--run", action="store_true", help="真正插行写入；不加只打印计划")
    args = p.parse_args()

    sheet = Sheet(args.url, args.sheet_id)
    current = sheet.read()
    tree, codes, layout, unmatched, no_unit, not_in_tree = plan(current, args.province)
    want = [[*row] for _, row in layout]

    out = HERE / f"{tree['名称']}比对_无区县代码的单位.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["城市", "清洗归属（开发区/园区等）", "单位", "职能", "记录数"])
        w.writerows(sorted(([u["城市"], u["地区"], u["单位"], u["职能"], u["记录数"]] for u in unmatched),
                           key=lambda r: (r[0], r[1], -int(r[4]))))
    print(f"{tree['名称']}：表里 {len(codes)} 个代码 → 拆分后 {len(layout)} 行；"
          f"{len(codes) - len(no_unit)} 个代码有单位，没有单位：{'、'.join(no_unit) or '无'}")
    if not_in_tree:
        print(f"注意：这些代码在青绿地区树里没有，E–G 留空：{not_in_tree}")
    print(f"找到但没有区县代码、不填的单位 {len(unmatched)} 个 → {out.name}")

    body = [r for r in current[1:] if any(r)]
    if body == want:
        print("飞书表已与本地结果一致，无需写入。")
        return
    fresh = len(body) == len(codes) and not any(any(r[4:12]) for r in body)
    if not fresh:
        raise SystemExit("飞书表既不是空白模板（一代码一行、E–L 全空），也不等于本地结果；"
                         "为免覆盖手工改动，不写。确认后先还原表格或手动处理差异。")
    if not args.run:
        print("计划已生成。确认无误后加 --run 写入。")
        return

    # 自下而上插行，上方行号不变
    row_of = {code: i + 2 for i, (code, _) in enumerate(codes)}
    extra = {code: n - 1 for code, n in _counts(layout).items() if n > 1}
    for code in sorted(extra, key=lambda c: -row_of[c]):
        sheet.cli("+dim-insert", "--position", str(row_of[code] + 1), "--count", str(extra[code]),
                  "--inherit-style", "before")
    # 新行的 A–D 按连续块写（代码写成数字，和原列一致）
    r, block = 2, None
    blocks = []
    for is_new, row in layout:
        if is_new:
            if block and block[0] + len(block[1]) == r:
                block[1].append(row[:4])
            else:
                block = (r, [row[:4]])
                blocks.append(block)
        r += 1
    for start, vals in blocks:
        cells = [[{"value": int(a) if a.isdigit() else a}, {"value": b}, {"value": c}, {"value": d}]
                 for a, b, c, d in vals]
        sheet.cli("+cells-set", "--range", f"A{start}:D{start + len(vals) - 1}", "--cells", "-",
                  payload=json.dumps(cells, ensure_ascii=False))
    end = 1 + len(layout)
    cells = [[{"value": v} for v in row[4:12]] for _, row in layout]
    sheet.cli("+cells-set", "--range", f"E2:L{end}", "--cells", "-", payload=json.dumps(cells, ensure_ascii=False))

    after = [r for r in sheet.read()[1:] if any(r)]
    bad = [i + 2 for i, (a, b) in enumerate(zip(after, want)) if a != b]
    if len(after) != len(want) or bad:
        raise SystemExit(f"回读不一致：行数 {len(after)}/{len(want)}，不一致的行 {bad[:10]}")
    print(f"写入完成并回读核对：{len(want)} 行全部一致（插入 {sum(extra.values())} 行）。")


def _counts(layout) -> dict[str, int]:
    n: dict[str, int] = {}
    for _, row in layout:
        n[row[0]] = n.get(row[0], 0) + 1
    return n


if __name__ == "__main__":
    main()
