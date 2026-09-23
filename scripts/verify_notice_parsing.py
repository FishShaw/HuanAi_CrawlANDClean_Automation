"""抓一个受理栏目的全部公示，跑 resolve_notice 的解析规则，统计命中率。

用来回答一件事：标题括号 / 发文机关 / 建设地点这三样，机器能不能自己解析出来——
能的话，飞书爬虫表里的「审批单位」「三级筛选」这些列就不用人工事先填。

    .venv/bin/python scripts/verify_notice_parsing.py --city 南京市
    .venv/bin/python scripts/verify_notice_parsing.py --city 常州市
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from lxml import html as LH

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import resolve_notice as RN  # noqa: E402
from clean_jiangsu_eia import Places  # noqa: E402

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

# 每个栏目的抓法不同，这里只放测试用的两个样本市
CHANNELS = {
    "南京市": {
        "list": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/xzxkhzfxxhz/xmhpslqk_68811/",
        "pages": 21,
        "page_url": lambda base, p: base if p == 1 else f"{base}index_{21 - p}.html",
        "link_re": re.compile(r"受理"),
        "org": "南京市生态环境局",
    },
    "常州市": {
        "list": "https://sthjj.changzhou.gov.cn/class/ALFJELKI",
        "pages": 1,
        "page_url": lambda base, p: base,
        "link_re": re.compile(r"受理情况的公示"),
        "org": "常州市生态环境局",
    },
}


def fetch(client: httpx.Client, url: str) -> str:
    r = client.get(url, timeout=25, follow_redirects=True)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = "utf-8"
    return r.text


def list_items(client: httpx.Client, cfg: dict) -> list[tuple[str, str]]:
    out: dict[str, str] = {}
    for p in range(1, cfg["pages"] + 1):
        try:
            txt = fetch(client, cfg["page_url"](cfg["list"], p))
        except Exception:
            continue
        doc = LH.fromstring(txt)
        doc.make_links_absolute(cfg["list"])
        for a in doc.xpath("//a[@href]"):
            title = (a.get("title") or a.text_content() or "").strip()
            title = re.sub(r"\s+", " ", title)
            href = a.get("href")
            if len(title) > 12 and cfg["link_re"].search(title) and re.search(r"\.(html|shtml)$", href):
                out.setdefault(href, title)
    return list(out.items())


def parse_detail(txt: str) -> tuple[str, list[str]]:
    doc = LH.fromstring(txt)
    body = re.sub(r"\s+", " ", doc.text_content())
    src = RN.parse_source(body)
    # 页面常用嵌套表格做版面（常州就是），只认「表头单元格正好是建设地点、且列数≥4」的那张，
    # 否则会把「[打印] [关闭]」这种布局格当成建设地点取出来。取最内层、数据行最多的一张。
    best: tuple[int, list[str]] = (0, [])
    for tb in doc.xpath("//table"):
        if tb.xpath(".//table"):
            continue  # 外层布局表跳过
        rows = tb.xpath(".//tr")
        if len(rows) < 2:
            continue
        for hi in range(min(3, len(rows))):  # 表头可能不在第一行
            head = [re.sub(r"\s+", "", c.text_content()) for c in rows[hi].xpath("./td|./th")]
            if len(head) < 4:
                continue
            ci = next((i for i, c in enumerate(head) if c == "建设地点"), -1)
            if ci < 0:
                continue
            vals = []
            for tr in rows[hi + 1:]:
                cells = tr.xpath("./td|./th")
                if len(cells) > ci:
                    v = re.sub(r"\s+", " ", cells[ci].text_content()).strip()
                    if v and "建设地点" not in v:
                        vals.append(v)
            if len(vals) > best[0]:
                best = (len(vals), vals)
            break
    return src, best[1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", default="南京市")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dump", default="")
    args = ap.parse_args()

    cfg = CHANNELS[args.city]
    tree = json.loads((ROOT / "refs" / "area_tree_320000.json").read_text(encoding="utf-8"))["地区树"]
    places = Places(tree)
    city = next(c for c in tree["市"] if c["名称"] == args.city)

    with httpx.Client(headers={**HEADERS, "Referer": cfg["list"]}) as client:
        items = list_items(client, cfg)
        if args.limit:
            items = items[: args.limit]
        print(f"{args.city} 栏目取到 {len(items)} 条公示")

        def work(it):
            url, title = it
            try:
                src, locs = parse_detail(fetch(client, url))
            except Exception as e:
                return {"url": url, "title": title, "err": type(e).__name__, "src": "", "locs": []}
            return {"url": url, "title": title, "src": src, "locs": locs}

        with ThreadPoolExecutor(max_workers=8) as ex:
            got = list(ex.map(work, items))

    err = [g for g in got if g.get("err")]
    ok = [g for g in got if not g.get("err")]
    res = [dict(g, **RN.classify(g["title"], g["src"], g["locs"], city, places, cfg.get("org", ""))) for g in ok]

    n = len(res)
    print(f"抓取成功 {len(ok)}/{len(got)}，失败 {len(err)}")
    print(f"解析出发文机关 {sum(1 for r in res if r['approval_org'])}/{n}"
          f" = {sum(1 for r in res if r['approval_org']) / max(n, 1) * 100:.1f}%")
    print(f"发文机关能判出审批地区 {sum(1 for r in res if r['approval_district'])}/{n}"
          f" = {sum(1 for r in res if r['approval_district']) / max(n, 1) * 100:.1f}%")
    withloc = sum(1 for r in res if r["n_projects"])
    print(f"取到建设地点表 {withloc}/{n} = {withloc / max(n, 1) * 100:.1f}%，"
          f"项目总数 {sum(r['n_projects'] for r in res)}")
    pd = [d for r in res for d in r["project_districts"]]
    print(f"建设地点能判出区县 {sum(1 for d in pd if d)}/{len(pd)}"
          f" = {sum(1 for d in pd if d) / max(len(pd), 1) * 100:.1f}%")
    print("\n括号语义分布:", dict(collections.Counter(r["bracket_meaning"] for r in res).most_common()))
    print("辐射条目:", sum(1 for r in res if r["radiation"]))
    print("\n发文机关分布:", dict(collections.Counter(r["approval_org"] for r in res).most_common(12)))

    # 审批地 vs 项目地 是否一致
    same = diff = 0
    for r in res:
        for d in r["project_districts"]:
            if not d or not r["approval_district"]:
                continue
            if d == r["approval_district"]:
                same += 1
            else:
                diff += 1
    print(f"\n审批地与项目地：相同 {same}，不同 {diff}"
          f"（不同占 {diff / max(same + diff, 1) * 100:.1f}%）")

    if args.dump:
        Path(args.dump).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"明细写入 {args.dump}")


if __name__ == "__main__":
    main()
