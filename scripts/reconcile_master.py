"""拿全省实爬的结果去校对受理单位主数据表的「入口」那几列。

为什么要单独一步：主数据里的 entry_url / verify_status 是上一轮人工开网页核的，
「打开栏目页看到环评条目」就算已核实。这轮真按入口把整栏抓了一遍，
能证伪其中一部分——栏目号是空壳、站点整个 403、栏目里根本不发受理。
实爬比人工看一眼强，所以按实爬结果回写，但只动入口那几列，
名称、归属、record_count 那些来自青绿和人工确认的列一律不碰。

    .venv/bin/python scripts/reconcile_master.py          # 出计划
    .venv/bin/python scripts/reconcile_master.py --run    # 真写
"""
from __future__ import annotations

import argparse
import collections
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "江苏省受理单位主数据.csv"
DATA = ROOT / "江苏省环评受理数据_样本.csv"
TODAY = "2026-09-24"

# 这轮实测打不开的站点。域名 → 判定 + 依据
BLOCKED = {
    # 这两个单次抓取成功过（溧阳 139 条、常州开发区 86 条），连抓才被封；
    # 真实列表页记在这里，换 IP 或放慢重试时不用再找一遍
    "liyang.gov.cn": ("访问受限", "2026-09-24 实爬：单次成功过（139 条），连抓被 WAF 封，"
                      "浏览器窗格同样 403；真实列表页 "
                      "/index.php?m=content&c=index&a=lists&catid=41617"),
    "jkq.changzhou.gov.cn": ("访问受限", "2026-09-24 实爬：单次成功过（86 条），连抓被 WAF 封；"
                             "栏目页是 iframe 壳，真实列表页 "
                             "/index.php?m=content&c=index&a=lists&catid=37877"),
    "tzw.nantong.gov.cn": ("访问受限", "2026-09-24 实爬：http/https 均 403，浏览器窗格同样"),
    "netda.gov.cn": ("访问受限", "2026-09-24 实爬：http/https 均 403，浏览器窗格同样"),
    "jurong.gov.cn": ("访问受限", "2026-09-24 实爬：整站跳 /403/"),
    "kfq.suqian.gov.cn": ("访问受限", "2026-09-24 实爬：http/https 均 403"),
    # 入口本身失效的退回未核实——四种核验情况里没有「入口失效」这一档
    "zghy.gov.cn": ("未核实", "2026-09-24 实爬：栏目页是壳，iframe 指向的内页 404，入口需重新定位"),
    "zghaq.gov.cn": ("未核实", "2026-09-24 实爬：栏目页是壳，iframe 指向的内页 404，入口需重新定位"),
}

# 实爬发现登记错了的入口。旧 URL 片段 → (新 URL, 新 entry_category, 依据)
REMAP = {
    "13403_336214": (
        "https://sthjj.huaian.gov.cn/col/13256_883485/index.html", "辐射类环评项目",
        "2026-09-24 实爬：13403_336214 是空壳栏目；真实栏目 13256_883485，"
        "且 435 条里只有 58 条受理、其中 56 条是核与辐射"),
    "shuyang.gov.cn/shuyang/sthj": (
        "http://www.shuyang.gov.cn/systj/xmsp/xxgk_list.shtml", "",
        "2026-09-24 实爬：原栏目翻页会串到统计局、城管局；受理公示在 /systj/xmsp/"),
    "cznd.gov.cn/class/PMOHQMDK": (
        "", "", "2026-09-24 实爬：该栏 54 条全是批复和拟批准，无受理；"
                 "新北与市局合署，受理走常州市局那条混合栏目"),
}


def norm(u: str) -> str:
    return u.rstrip("/").replace("https://", "").replace("http://", "").replace("www.", "")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()

    data = list(csv.DictReader(DATA.open(encoding="utf-8-sig")))
    crawled = collections.Counter(norm(r["入口URL"]) for r in data)

    rows = list(csv.DictReader(MASTER.open(encoding="utf-8-sig")))
    hdr = list(rows[0])
    if "crawl_note" not in hdr:
        hdr.append("crawl_note")
    changes: list[tuple[str, str, str]] = []

    for r in rows:
        r.setdefault("crawl_note", "")
        url = r["entry_url"]
        if not url:
            continue
        host = norm(url).split("/")[0]
        n = crawled.get(norm(url), 0)

        remap = next((v for k, v in REMAP.items() if k in norm(url)), None)
        if remap:
            new_url, cat, why = remap
            if new_url and new_url != url:
                changes.append((r["unit_uid"], "entry_url", new_url))
                r["entry_url"] = new_url
            if cat and r["entry_category"] != cat:
                changes.append((r["unit_uid"], "entry_category", cat))
                r["entry_category"] = cat
            r["crawl_note"] = why
            r["verify_date"] = TODAY
            changes.append((r["unit_uid"], "crawl_note", why[:40]))
            continue

        if host in BLOCKED:
            status, why = BLOCKED[host]
            if r["verify_status"] != status:
                changes.append((r["unit_uid"], "verify_status", f"{r['verify_status']}→{status}"))
                r["verify_status"] = status
            r["crawl_note"] = why
            r["verify_date"] = TODAY
            continue

        if n:
            why = f"2026-09-24 实爬：该入口抓到 {n} 条受理公告明细"
            if r["verify_status"] != "已核实":
                changes.append((r["unit_uid"], "verify_status", f"{r['verify_status']}→已核实"))
                r["verify_status"] = "已核实"
                r["verify_date"] = TODAY
            r["crawl_note"] = why

    print(f"待改 {len(changes)} 处，涉及 {len({c[0] for c in changes})} 个单位")
    for f, c in collections.Counter(c[1] for c in changes).most_common():
        print(f"  {f}: {c}")
    for c in changes[:12]:
        print("   ", c)
    touched = sum(1 for r in rows if r.get("crawl_note"))
    print(f"打上实爬佐证的行 {touched}")
    if not args.run:
        print("\n未写入。加 --run 才写。")
        return
    w = csv.DictWriter(MASTER.open("w", newline="", encoding="utf-8-sig"), fieldnames=hdr)
    w.writeheader()
    w.writerows(rows)
    print(f"已写回 {MASTER.name}")


if __name__ == "__main__":
    main()
