"""抓一个受理栏目的全部公示，跑 resolve_notice 的解析规则，统计命中率。

用来回答一件事：标题括号 / 发文机关 / 建设地点这三样，机器能不能自己解析出来——
能的话，飞书爬虫表里的「审批单位」「三级筛选」这些列就不用人工事先填。

    .venv/bin/python scripts/verify_notice_parsing.py --city 南京市
    .venv/bin/python scripts/verify_notice_parsing.py --city 常州市
"""
from __future__ import annotations

import argparse
import collections
import csv
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
        "pages": 30,
        "page_url": lambda base, p: base if p == 1 else f"{base}/{p}",
        "link_re": re.compile(r"受理情况的公示"),
        "org": "常州市生态环境局",
    },
    "镇江市": {
        "list": "https://sthj.zhenjiang.gov.cn/sthj/slqkgs/xxgk_list.shtml",
        "pages": 15,
        "page_url": lambda base, p: base if p == 1 else base.replace("xxgk_list.shtml", f"xxgk_list_{p-1}.shtml"),
        "link_re": re.compile(r"受理"),
        "org": "镇江市生态环境局",
    },
    "泰州市": {
        "list": "https://hbj.taizhou.gov.cn/ztzl/jsxm/xmslgs/index.html",
        "pages": 15,
        "page_url": lambda base, p: base if p == 1 else base.replace("index.html", f"index_{p-1}.html"),
        "link_re": re.compile(r"受理"),
        "org": "泰州市生态环境局",
    },
    "扬州市": {
        "list": "https://sthj.yangzhou.gov.cn/zfxxgk/fdzdgk/ywgz/hpsp/",
        "pages": 15,
        "page_url": lambda base, p: base if p == 1 else f"{base}index_{p-1}.html",
        "link_re": re.compile(r"受理"),
        "org": "扬州市生态环境局",
    },
    "盐城市": {
        "list": "https://jsychb.yancheng.gov.cn/col/col17849/index.html",
        "pages": 1, "page_url": lambda base, p: base,
        "link_re": re.compile(r"受理"), "org": "盐城市生态环境局",
    },
    "宿迁市": {
        "list": "https://sthj.suqian.gov.cn/shbj/jsxm/xxgk_list.shtml",
        "pages": 12,
        "page_url": lambda base, p: base if p == 1 else base.replace("xxgk_list.shtml", f"xxgk_list_{p-1}.shtml"),
        "link_re": re.compile(r"受理情况的公示"), "org": "宿迁市生态环境局",
    },
    "南通市": {
        "list": "https://shuju.nantong.gov.cn/ntsxzspj/sphjgs/sphjgs.html",
        "pages": 12,
        "page_url": lambda base, p: base if p == 1 else base.replace("sphjgs.html", f"sphjgs_{p-1}.html"),
        "link_re": re.compile(r"受理公示"), "org": "南通市数据局",
    },
    "苏州市": {
        "list": "https://sthjj.suzhou.gov.cn/szhbj/jsslgs/bjgs_list.shtml",
        "pages": 25,
        "page_url": lambda base, p: base if p == 1 else base.replace("bjgs_list.shtml", f"bjgs_list_{p}.shtml"),
        "link_re": re.compile(r"受理"), "org": "苏州市生态环境局",
    },
    "无锡市": {
        "list": "https://bigdata.wuxi.gov.cn/gggs/jsxmhpspgszl/ffsjsxmhpspgs/slgs/index.shtml",
        "pages": 3,
        "page_url": lambda base, p: base if p == 1 else base.replace("index.shtml", f"index_{p-1}.shtml"),
        "link_re": re.compile(r"受理"), "org": "无锡市数据局",
    },
    "徐州市": {
        # https 整站对程序化访问回 403，http 可用；列表是 POST 接口，靠 --links-file 喂进来
        "list": "http://sthj.xz.gov.cn/dynamic/zwgk/govInfoPub.html?categorynum=003011",
        "pages": 1, "page_url": lambda base, p: base,
        "link_re": re.compile(r"受理"), "org": "徐州市生态环境局",
    },
    "淮安市": {
        # 列表是 JS 画的，但 gotopage() 只是跳静态 index_N.html，直接按静态页抓即可。
        # 注意栏目号：entries 里记的 13403_336214 是空壳，真实栏目是 13256_883485
        "list": "https://sthjj.huaian.gov.cn/col/13256_883485/index.html",
        "pages": 29,
        "page_url": lambda base, p: base if p == 1 else base.replace("index.html", f"index_{p}.html"),
        "link_re": re.compile(r"受理"), "org": "淮安市生态环境局",
    },
    "连云港市": {
        "list": "http://hbj.lyg.gov.cn/slgs/slgs.html",
        "pages": 1, "page_url": lambda base, p: base,
        "link_re": re.compile(r"受理"), "org": "连云港市生态环境局",
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
            if len(title) > 12 and cfg["link_re"].search(title) and re.search(r"(\.(html|shtml)(\?|$)|/content/[0-9a-f-]{8,})", href) and "javascript" not in href:
                out.setdefault(href, title)
    return list(out.items())


FIELD_ALIASES = {
    "项目名称": ("项目名称", "建设项目名称"),
    "建设地点": ("建设地点", "建设地址", "项目地点"),
    "建设单位": ("建设单位", "申请单位", "建设单位名称"),
    "环评机构": ("环境影响评价机构", "环评机构", "评价机构", "环境影响评价单位"),
    "受理日期": ("受理日期", "受理时间"),
}


def _field_of(cell: str) -> str:
    for name, aliases in FIELD_ALIASES.items():
        if cell in aliases:
            return name
    return ""


def title_of(doc) -> str:
    """列表页由 JS 渲染时拿不到标题，从详情页自己取。"""
    for xp in ('//meta[@name="ArticleTitle"]/@content', '//h1//text()', '//title/text()'):
        v = doc.xpath(xp)
        if v:
            t = re.sub(r"\s+", " ", str(v[0])).strip()
            t = re.split(r"\s*[|_]\s*", t)[0].strip()
            if len(t) > 8:
                return t
    return ""


def parse_detail(txt: str) -> tuple[str, list[dict]]:
    """返回 (发文机关, [每个项目一个 dict])。

    两种表格形态都要认：横向表头（南京、常州：一行一个项目）和纵向键值表
    （镇江：左列是「建设地点」这类标签，一页一个项目）。页面还常用嵌套表格做版面，
    所以横向表要求「表头单元格正好等于字段名且列数>=4」并跳过含子表的外层表，
    否则会把「[打印] [关闭]」当成建设地点取出来。
    """
    doc = LH.fromstring(txt)
    body = re.sub(r"\s+", " ", doc.text_content())
    src = RN.parse_source(body)
    page_title = title_of(doc)

    best: list[dict] = []
    for tb in doc.xpath("//table"):
        if tb.xpath(".//table"):
            continue
        rows = tb.xpath(".//tr")
        if len(rows) < 2:
            continue
        for hi in range(min(3, len(rows))):
            head = [re.sub(r"\s+", "", c.text_content()) for c in rows[hi].xpath("./td|./th")]
            if len(head) < 4:
                continue
            cmap = {_field_of(c): i for i, c in enumerate(head) if _field_of(c)}
            if "建设地点" not in cmap:
                continue
            vals = []
            for tr in rows[hi + 1:]:
                cells = tr.xpath("./td|./th")
                rec = {}
                for name, ci in cmap.items():
                    if len(cells) > ci:
                        rec[name] = re.sub(r"\s+", " ", cells[ci].text_content()).strip()
                if rec.get("建设地点") and "建设地点" not in rec["建设地点"]:
                    vals.append(rec)
            if len(vals) > len(best):
                best = vals
            break
    if best:
        return src, best, page_title

    rec = {}
    for tb in doc.xpath("//table"):
        for tr in tb.xpath(".//tr"):
            cells = tr.xpath("./td|./th")
            if len(cells) < 2:
                continue
            name = _field_of(re.sub(r"\s+", "", cells[0].text_content()))
            if name and name not in rec:
                v = re.sub(r"\s+", " ", cells[1].text_content()).strip()
                if v:
                    rec[name] = v
        if rec.get("建设地点"):
            break
    if rec.get("建设地点"):
        return src, [rec], page_title
    return src, _from_body(body), page_title


# 南通的公示没有表格，正文直接写「项目名称：…； 建设地点：…； 建设单位：…」，一条公示一个项目
_INLINE = re.compile(r"(项目名称|建设地点|建设地址|建设单位|环境影响评价机构|环评机构|受理日期)"
                     r"[：:]\s*([^；;]{1,120}?)\s*(?=[；;]|$|项目名称|建设地点|建设单位|环评机构)")


def _from_body(body: str) -> list[dict]:
    rec = {}
    for label, val in _INLINE.findall(body):
        name = _field_of(label)
        if name and name not in rec and val.strip():
            rec[name] = val.strip()
    return [rec] if rec.get("建设地点") else []


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", default="南京市")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dump", default="")
    ap.add_argument("--projects", default="")
    ap.add_argument("--links-file", default="",
                    help="列表页由 JS 渲染时，用浏览器导出的 URL\\t标题 文件喂进来")
    ap.add_argument("--entry-url", default="", help="覆盖入口URL：区县门户各有各的入口，"
                    "但共用所在市的地区树，所以 --city 只选地区上下文，入口单独给")
    ap.add_argument("--org", default="", help="覆盖栏目归属机构（来源不含地名时用它补全）")
    args = ap.parse_args()

    # CHANNELS 只登记了 13 个市的市局栏目；区县门户和省厅栏目靠 --entry-url + --links-file 传进来
    cfg = dict(CHANNELS.get(args.city) or
               {"list": "", "pages": 1, "page_url": lambda b, p: b,
                "link_re": re.compile(r"受理"), "org": ""})
    if args.entry_url:
        cfg["list"] = args.entry_url
    if args.org:
        # 飞书「栏目归属机构」列带人工注解（「（含XX局6条）」「（青绿旧名：…）」），
        # 它会被当成发文机关写进产出，先剥掉。机构名本身的括号（「(太湖办)」
        # 「(杨舍镇)」）要留，所以只认注解词
        cfg["org"] = re.sub(r"[（(](?=[^）)]*(?:含|另有|旧名|青绿|\d+\s*条))[^）)]*[）)]\s*$",
                            "", args.org).strip().strip("【】 ")
    tree = json.loads((ROOT / "refs" / "area_tree_320000.json").read_text(encoding="utf-8"))["地区树"]
    places = Places(tree)
    # 省厅栏目不属于任何一个市，用一个没有区县列表的伪「市」占位，
    # resolve_notice 见到空区县列表就改走全省唯一命中
    city = next((c for c in tree["市"] if c["名称"] == args.city),
                {"编码": tree["编码"], "名称": tree["名称"], "简称": tree["简称"], "区县": []})

    with httpx.Client(headers={**HEADERS, "Referer": cfg["list"]}) as client:
        if args.links_file:
            items = []
            for line in Path(args.links_file).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                u, _, t = line.partition("\t")
                items.append((u.strip(), t.strip()))
            print(f"从 {args.links_file} 读入 {len(items)} 条链接")
        else:
            items = list_items(client, cfg)
        if args.limit:
            items = items[: args.limit]
        print(f"{args.city} 栏目取到 {len(items)} 条公示")

        def work(it):
            url, title = it
            try:
                src, recs, ptitle = parse_detail(fetch(client, url))
            except Exception as e:
                return {"url": url, "title": title, "err": type(e).__name__, "src": "", "recs": []}
            return {"url": url, "title": title or ptitle, "src": src, "recs": recs,
                    "locs": [r.get("建设地点", "") for r in recs]}

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

    if args.projects:
        cols = ["城市","入口URL","公告URL","标题","发布日期","发文机关","审批地区",
                "项目名称","建设地点","项目地区","建设单位","环评机构","是否辐射","括号语义"]
        out = []
        for r in res:
            n = max(len(r["project_districts"]), 1)
            for i in range(n):
                out.append({
                    "城市": args.city, "入口URL": cfg["list"], "公告URL": r["url"], "标题": r["title"],
                    "发布日期": (re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", r["title"]) or [""])[0],
                    "发文机关": r["approval_org"], "审批地区": r["approval_district"],
                    "项目名称": (r["recs"][i].get("项目名称", "") if i < len(r["recs"]) else ""),
                    "建设地点": (r["locs"][i] if i < len(r["locs"]) else ""),
                    "项目地区": (r["project_districts"][i] if i < len(r["project_districts"]) else ""),
                    "建设单位": (r["recs"][i].get("建设单位", "") if i < len(r["recs"]) else ""),
                    "环评机构": (r["recs"][i].get("环评机构", "") if i < len(r["recs"]) else ""),
                    "是否辐射": "是" if r["radiation"] else "否",
                    "括号语义": r["bracket_meaning"],
                })
        pth = Path(args.projects)
        new = not pth.exists()
        with open(pth, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            if new:
                w.writeheader()
            w.writerows(out)
        print(f"项目级明细追加 {len(out)} 行 → {pth}")

    if args.dump:
        Path(args.dump).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"明细写入 {args.dump}")


if __name__ == "__main__":
    main()
