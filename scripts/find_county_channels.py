"""在区县政府门户里找生态环境分局的环评受理栏目，并用栏目里的受理公告核实。

浙江、江苏的县 / 县级市分局不在市局栏目里发受理公示，而是在本地政府门户
（「政府信息公开 → 部门 → XX 分局 → 行政许可 / 环评审批」）。门户结构各不相同，
这里不猜栏目地址，而是有限深度地顺着链接走：

1. 从门户首页出发，只跟进名字像「信息公开 / 部门 / 生态环境 / 环保 / 分局」的链接，最多 3 层、每县 40 页
2. 沿途收集名字像「环评 / 环境影响 / 受理 / 建设项目 / 审批公示 / 行政许可」的栏目
3. 每个候选栏目拉前几页标题，数受理公告：标题开头带发布机构的，按归并键对上单位；
   不带的，栏目所在页面（面包屑 / 标题）里写着生态环境或分局，才记到本县的生态环境分局
4. 每个单位取受理公告最多的栏目，写进 TSV 供人复核，不直接改 entries/

    .venv/bin/python scripts/find_county_channels.py --province 330000 --portals portals.json --out found.tsv
portals.json：{"市": {"区县全称": "门户首页"}}
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import csv
import json
import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "list_api"))

import collect  # noqa: E402
from clean_jiangsu_eia import Places, normalize  # noqa: E402
from entries_from_channels import PUBLISHER  # noqa: E402
from report_stage_probe import stage_of  # noqa: E402

FOLLOW = re.compile(r"信息公开|公开目录|部门|生态环境|环保|分局|政务公开|主动公开|法定")
CHANNEL = re.compile(r"环评|环境影响|受理|建设项目|审批公示|行政许可|许可公示|审批结果|项目公示|公示公告")
SKIP = re.compile(r"\.(pdf|docx?|xlsx?|zip|rar|jpg|png)$|javascript:|mailto:|/art/|art_|/content/", re.I)
ECO_PAGE = re.compile(r"生态环境|环保|环境保护|分局")


def anchors(html: str, base: str):
    for tag, inner in re.findall(r"(<a\b[^>]*>)(.*?)</a>", html, re.S):
        m = re.search(r"href=[\"']([^\"'#]+)", tag)
        if not m:
            continue
        text = re.sub(r"<[^>]+>|\s+", "", inner)
        t2 = re.search(r"title=[\"']([^\"']+)", tag)
        title_attr = t2.group(1) if t2 else ""
        u = urllib.parse.urljoin(base, m.group(1).strip())
        u = re.sub(r"\?t=_blank.*$", "", u)
        # 截断的锚文本（「…」结尾）用 title 属性里的全名
        yield (title_attr if title_attr and (len(text) < 2 or text.endswith("...") or text.endswith("…")) else text), u


# 标题像环评受理 / 审批的公告：由公告链接反推它所在的栏目
NOTICE = re.compile(r"(受理|拟对|拟作出|审批决定|审批意见|批复).{0,40}(环评|环境影响|建设项目)|"
                    r"(环评|环境影响).{0,30}(受理|审批)")


def column_of(url: str) -> str:
    """公告 URL → 栏目首页。jpaas：/col/col123/art/2026/art_x.html；大汉：/art/2026/9/1/art_123_456.html"""
    m = re.search(r"^(.*?/col/col\d+)/", url)
    if m:
        return m.group(1) + "/index.html"
    m = re.search(r"^(https?://[^/]+)/art/\d{4}/\d+/\d+/art_(\d+)_\d+\.html", url)
    if m:
        return f"{m.group(1)}/col/col{m.group(2)}/index.html"
    return ""


def rendered(html: str, url: str) -> str:
    """jpaas 页面的列表区块由接口渲染，原始 HTML 里没有：把页面上每个区块都请求一遍拼回来。"""
    if "jpaas-publish-server" not in html:
        return html
    try:
        fam, cfg = collect.sniff(url)
    except Exception:
        return html
    if fam != "jpaas":
        return html
    parts = [html]
    for tag in dict.fromkeys(re.findall(r"tagId[\"']?\s*[:=]\s*[\"']([^\"']+)", html)):
        try:
            d = collect.get(cfg["api"], cfg["url"], params=dict(cfg["q"], tagId=tag)).json()
            parts.append((d.get("data") or {}).get("html") or "")
        except Exception:
            pass
    return "\n".join(parts)


def explore(home: str, max_pages: int = 40, depth: int = 3) -> dict[str, tuple[str, str]]:
    """返回 {候选栏目 URL: (依据, 来路页面标题)}。

    两种候选：链接文字像环评栏目的；以及页面上标题像环评受理 / 审批公告的，反推它所在的栏目
    （瑞安的部门列表是 JS 画的，但「组配分类」区块里有「温州市生态环境局瑞安分局关于…」的公告）。
    """
    host = urllib.parse.urlparse(home).netloc.removeprefix("www.")
    seen, out = set(), {}
    frontier = [(home, 0)]
    n = 0
    while frontier and n < max_pages:
        url, d = frontier.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            r = collect.get(url)
            n += 1
        except Exception:
            continue
        html = rendered(r.text, str(r.url))
        title = re.search(r"<title>(.*?)</title>", r.text, re.S)
        ptitle = re.sub(r"\s+", "", title.group(1))[:40] if title else ""
        for text, u in anchors(html, str(r.url)):
            if not text or urllib.parse.urlparse(u).netloc.removeprefix("www.") != host:
                continue
            if NOTICE.search(text):
                col = column_of(u)
                if col and col not in out:
                    out[col] = ("公告：" + text[:30], ptitle)
                continue
            if len(text) > 24 or SKIP.search(u):
                continue
            if CHANNEL.search(text) and u not in out:
                out[u] = (text, ptitle)
            if d + 1 <= depth and FOLLOW.search(text) and u not in seen:
                frontier.append((u, d + 1))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--province", required=True)
    ap.add_argument("--portals", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="", help="只跑这些区县，逗号分隔")
    args = ap.parse_args()

    tree = json.loads((ROOT / "refs" / f"area_tree_{args.province}.json").read_text(encoding="utf-8"))["地区树"]
    places = Places(tree)
    code_of = {c["名称"]: c["编码"] for c in tree["市"]}
    short = {(c["名称"], d["名称"]): d["简称"] for c in tree["市"] for d in c["区县"]}
    units = list(csv.DictReader((ROOT / f"{tree['名称']}环评单位.csv").open(encoding="utf-8-sig")))
    by_key = {f"{u['城市']}|{u['地区']}|{places.function_of(normalize(u['单位']))}": u for u in units}
    portals = json.loads(Path(args.portals).read_text(encoding="utf-8"))
    only = set(filter(None, args.only.split(",")))

    def one(job):
        city, dist, home = job
        dshort = short.get((city, dist), dist)
        cands = explore(home)
        hits = collections.Counter()
        detail = {}
        for url, (text, ptitle) in list(cands.items())[:25]:
            try:
                fam, cfg = collect.sniff(url)
                rows_ = collect.PULL[fam](cfg, 3)
            except Exception:
                continue
            ct = re.search(r"<title>(.*?)</title>", cfg.get("page1", ""), re.S)
            col_title = re.sub(r"\s+", "", ct.group(1)) if ct else ""
            # 一个栏目只属于一个发布机构：先看整栏里写明了机构的标题都指向谁
            keyed, bare = [], 0
            for _u, t in rows_:
                t = re.sub(r"^[\u200b\ufeff\s]*(?:\d{4}年\d{1,2}月\d{1,2}日)?", "", t).strip()
                m = PUBLISHER.match(t)
                k = ""
                if m:
                    n = normalize(m.group(1))
                    a = places.area_of(n, code_of[city], "")
                    k = f"{a[0]}|{a[1]}|{places.function_of(n)}" if a else ""
                if stage_of(t) == "受理":
                    if k:
                        keyed.append(k)
                    else:
                        bare += 1
            owners = collections.Counter(
                k for _u, t in rows_
                for m in [PUBLISHER.match(re.sub(r"^[\u200b\ufeff\s]*(?:\d{4}年\d{1,2}月\d{1,2}日)?", "", t).strip())]
                if m
                for a in [places.area_of(normalize(m.group(1)), code_of[city], "")] if a
                for k in [f"{a[0]}|{a[1]}|{places.function_of(normalize(m.group(1)))}"])
            owner = owners.most_common(1)[0][0] if owners else ""
            # 整栏都没写机构：栏目页正文（面包屑）里写着「平阳分局」「生态环境局平阳分局」，
            # 或栏目名 / 来路页面像生态环境口，才记到本县生态环境分局
            page = re.sub(r"<[^>]+>|\s+", "", cfg.get("page1", ""))
            if not owner and (f"{dshort}分局" in page or f"生态环境局{dshort}" in page
                              or ECO_PAGE.search(col_title + text + ptitle)):
                owner = f"{city}|{dshort}|生态环境"
            # 县门户上以市局名义发、标题或栏目名带本县县名的（宁波「宁波市生态环境局…受理公告（慈溪…」），
            # 是本县分局代市局发——和南京标题括号「（玄武）」同一种写法。市局自己的公告发在市局网站上
            city_key, county_key = f"{city}|市本级|生态环境", f"{city}|{dshort}|生态环境"
            names_county = dshort in col_title or any(dshort in t for _u, t in rows_ if stage_of(t) == "受理")
            def own(k):
                return county_key if k == city_key and county_key in by_key and names_county else k
            for k in keyed:
                k = own(k)
                if k in by_key:
                    hits[(k, url)] += 1
                    detail[(k, url)] = (col_title or text, ptitle)
            owner = own(owner)
            if bare and owner in by_key:
                hits[(owner, url)] += bare
                detail[(owner, url)] = (col_title or text, ptitle)
        rows = []
        for (k, url), n in hits.items():
            rows.append({"市": city, "区县": dist, "单位": by_key[k]["单位"], "栏目": url,
                         "受理公告数": n, "栏目名": detail[(k, url)][0], "来路页面": detail[(k, url)][1],
                         "门户": home, "候选栏目数": len(cands)})
        if not rows:
            rows.append({"市": city, "区县": dist, "单位": "", "栏目": "", "受理公告数": 0,
                         "栏目名": "", "来路页面": "", "门户": home, "候选栏目数": len(cands)})
        return rows

    jobs = [(c, d, h) for c, m in portals.items() for d, h in m.items() if not only or d in only]
    out = []
    with cf.ThreadPoolExecutor(8) as ex:
        for rows in ex.map(one, jobs):
            out += rows
            r0 = rows[0]
            got = [r for r in rows if r["单位"]]
            print(f"  {r0['市']} {r0['区县']}: 候选栏目 {r0['候选栏目数']}，核到 "
                  + ("；".join(f"{r['单位']}({r['受理公告数']})" for r in got) if got else "—"),
                  file=sys.stderr, flush=True)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]), delimiter="\t")
        w.writeheader()
        w.writerows(out)


if __name__ == "__main__":
    main()
