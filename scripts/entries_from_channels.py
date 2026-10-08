"""从核实过的公示栏目出发，按「谁在栏目里发了受理公告」给单位挂入口。

不逐个单位猜入口：把 refs/channels_<省>.tsv 里每个受理栏目的公告标题拉下来，
从标题抽出发布机构（「金华市生态环境局婺城分局关于…受理…」→ 婺城分局），
用主数据同一套归并键（市|区域|职能）对上单位。

- 在某栏目里有自己发布的受理公告 → 填该栏目，核验情况「已核实」，备注写条数
- 标题不带发布机构的（「关于受理《…》的公告」「2026年9月28日受理…」）→ 记到栏目归属的市局
- 一条也没找到 → 不填地址，「未核实」。按约定没定位到发布平台的不填地址，不拿市局栏目去凑
- 其他部门只是发布渠道，只给首页

    .venv/bin/python scripts/entries_from_channels.py --province 330000          # 出计划
    .venv/bin/python scripts/entries_from_channels.py --province 330000 --run    # 写 entries/
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "list_api"))

import collect  # noqa: E402
from clean_jiangsu_eia import Places, normalize, role_of  # noqa: E402
from report_stage_probe import stage_of  # noqa: E402

COLS = ["单位", "首页入口", "受理公示入口", "审批前公示入口", "审批后意见入口",
        "入口（点击路径）", "备注", "核验情况", "核验日期"]
TODAY = "2026-10-08"
# 标题开头的发布机构：截到「关于 / 日期 / 拟对 / 受理 / 作出」之前，且以机构字结尾
PUBLISHER = re.compile(r"^(.{3,40}?(?:局|分局|厅|委员会|管委会|办公室|中心|保障局|）|\)))"
                       r"\s*(?:关于|\d{4}\s*年|拟|受理|作出|对)")
NINGBO = "https://jyh.nbepb.gov.cn"


def titles_of(url: str, max_pages: int) -> list[str]:
    """一个栏目的公告标题。宁波是独立 JSON 系统，其余走 collect。"""
    m = re.search(r"jyh\.nbepb\.gov\.cn/CPA/Jsxm\.html\?typeid=(\d+)&classid=(\d+)", url)
    if m:
        r = httpx.get(f"{NINGBO}/Json/CPA/List-{m.group(1)}-{m.group(2)}.json",
                      headers={**collect.UA, "Referer": url}, timeout=60)
        return [x["title"] for x in r.json()["list"]]
    _fam, rows = collect.collect(url, max_pages)
    return [t for _u, t in rows]


def paren(s: str) -> str:
    """按名称兜底匹配时统一括号：主数据里多是半角「(高新区)」，公告标题里是全角「（高新区）」。
    不改 clean_jiangsu_eia.normalize——那会改变江苏的归并结果。"""
    return s.replace("（", "(").replace("）", ")")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--province", required=True)
    ap.add_argument("--max-pages", type=int, default=40)
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()

    tree = json.loads((ROOT / "refs" / f"area_tree_{args.province}.json").read_text(encoding="utf-8"))["地区树"]
    places = Places(tree)
    code_of = {c["名称"]: c["编码"] for c in tree["市"]}
    prov = tree["名称"]
    units = list(csv.DictReader((ROOT / f"{prov}环评单位.csv").open(encoding="utf-8-sig")))

    def key(city: str, name: str) -> str:
        n = normalize(name)
        code = code_of.get(city, next(iter(code_of.values())))
        a = places.area_of(n, code, "")
        return f"{a[0]}|{a[1]}|{places.function_of(n)}" if a else ""

    by_key, by_name = {}, {}
    for u in units:
        k = f"{u['城市']}|{u['地区']}|{places.function_of(normalize(u['单位']))}"
        by_key.setdefault(k, u)
        # 归并键对不上时按名称兜底：标题算出的开发区标签可能和清洗时（借区县字段）算出的不同，
        # 「台州市生态环境局台州湾新区（高新区）分局」48 条原先因此被记到了台州市局名下
        for n in [u["单位"], *(u["合并的其他写法"] or "").split(" / ")]:
            if n.strip():
                by_name.setdefault((u["城市"], paren(normalize(n.strip()))), k)

    chans = collections.defaultdict(dict)    # 市 → 阶段 → (url, 首页)
    for line in (ROOT / "refs" / f"channels_{args.province}.tsv").open(encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        city, stage, url, home = line.rstrip("\n").split("\t")
        chans[city][stage] = (url, home)

    # 每个市的受理栏目里，各单位发了多少条受理公告
    hits: dict[tuple[str, str], int] = collections.Counter()       # (unit_key, 栏目url) → 条数
    owner_fallback: dict[str, tuple[str, int]] = {}
    for city, st in chans.items():
        url = (st.get("受理") or st.get("混合"))[0]
        ts = [t for t in titles_of(url, args.max_pages) if stage_of(t) == "受理"]
        owner = 0
        for t in ts:
            # 省厅老标题开头带日期（「2016年7月1日省环保厅关于…」），还有零宽空格
            t = re.sub(r"^[\u200b\ufeff\s]*(?:\d{4}年\d{1,2}月\d{1,2}日)?", "", t)
            m = PUBLISHER.match(t.strip())
            c = "—" if city == "省级" else city
            k = key(c, m.group(1)) if m else ""
            if m and k not in by_key:
                k = by_name.get((c, paren(normalize(m.group(1)))), k)
            if k in by_key:
                hits[(k, url)] += 1
            else:
                owner += 1
        owner_fallback[city] = (url, owner)
        print(f"  {city}: 受理公告 {len(ts)} 条，认出发布机构 {len(ts) - owner}，"
              f"无机构名记到市局 {owner}", file=sys.stderr)

    # 栏目归属的市局：该市市本级、生态环境口、记录数最多的那个单位（省级取省厅）
    for city, (url, n) in owner_fallback.items():
        if not n:
            continue
        cands = [u for u in units if (u["城市"] == ("—" if city == "省级" else city))
                 and u["地区"] == ("省级" if city == "省级" else "市本级") and u["职能"] == "生态环境"]
        if cands:
            top = max(cands, key=lambda u: int(u["记录数"]))
            hits[(f"{top['城市']}|{top['地区']}|生态环境", url)] += n

    out: dict[str, list[dict]] = collections.defaultdict(list)
    stat = collections.Counter()
    for u in units:
        city = u["城市"]
        ccode = code_of.get(city, "000000")
        fname = f"{ccode}_{city}.csv" if city != "—" else "000000_省级及国家.csv"
        ch = chans.get("省级" if city == "—" else city, {})
        home = next(iter(ch.values()))[1] if ch else ""
        k = f"{city}|{u['地区']}|{places.function_of(normalize(u['单位']))}"
        found = [(url, n) for (kk, url), n in hits.items() if kk == k]
        row = dict.fromkeys(COLS, "")
        row["单位"] = u["单位"]
        if role_of(u["单位"]) == "其他部门" and u["职能"] == "其他部门":
            row["备注"] = "其他部门只是公告发布渠道；本单位官网未查，不填"
            row["核验情况"] = "无独立环评栏目"
            stat["其他部门"] += 1
        elif found:
            url, n = max(found, key=lambda x: x[1])
            # 首页只给栏目所在站点的；没核到的单位不能借市局首页充数
            row["首页入口"] = home
            row["受理公示入口"] = url
            row["审批前公示入口"] = (ch.get("拟审批") or ch.get("混合") or ("", ""))[0]
            row["审批后意见入口"] = (ch.get("审批决定") or ch.get("混合") or ("", ""))[0]
            row["备注"] = f"{TODAY} 拉栏目标题核实：该单位在此栏目发布受理公告 {n} 条"
            row["核验情况"] = "已核实"
            stat["已核实"] += 1
        else:
            row["备注"] = (f"{TODAY}：市局受理栏目近 {args.max_pages} 页里没有该单位发布的受理公告，"
                          "可能在区县门户单独发布，未定位，不填地址")
            row["核验情况"] = "未核实"
            stat["未核实"] += 1
        row["核验日期"] = TODAY
        out[fname].append(row)

    print(f"\n{prov} {len(units)} 个单位：" + "，".join(f"{k} {v}" for k, v in stat.most_common()))
    for fname, rows in sorted(out.items()):
        ok = sum(1 for r in rows if r["核验情况"] == "已核实")
        print(f"  {fname}: {len(rows)} 行，已核实 {ok}")
    if not args.run:
        print("\n未写入。加 --run 才写。")
        return

    edir = ROOT / "entries"
    for fname, rows in out.items():
        f = edir / fname
        if fname.startswith("000000"):
            # 省级及国家是多省共用的文件：只追加本省还没有的单位，别人的行一律不动
            old = list(csv.DictReader(f.open(encoding="utf-8-sig"))) if f.exists() else []
            have = {r["单位"] for r in old}
            rows = old + [r for r in rows if r["单位"] not in have]
        elif f.exists():
            sys.exit(f"{f.name} 已存在，拒绝覆盖（可能有人工核实的内容），先人工比对")
        with f.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader()
            w.writerows(rows)
    print("已写入 entries/")


if __name__ == "__main__":
    main()
