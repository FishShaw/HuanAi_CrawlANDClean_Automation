"""给一个受理栏目入口，自动认出建站系统并把整栏的 URL\t标题 导出来。

江苏 13 市 + 57 个区县门户一共只有五种取数方式，但每个市自己的写法都不一样，
一个个手配成本太高。这里先按页面里的指纹认家族（dataproxy / jpaas / truecms / EWB），
认不出就当静态页，拿第 2 页去试一串候选 URL 模板，哪个能翻出新链接就用哪个。

    .venv/bin/python scripts/list_api/collect.py <栏目URL> [--max-pages N]
"""
from __future__ import annotations

import argparse
import collections
import html as H
import json
import re
import sys
import time
import urllib.parse

import httpx

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
# 正文链接：各站的详情页要么是 /art/ /content/，要么就是普通 .html/.shtml
ART = re.compile(r"(/art/|art_[0-9a-zA-Z]{8,}|/content/[0-9a-f-]{8,}|\.(?:s?html)(?:\?|$))")
SKIP = re.compile(r"(index|list|_list|default)\.s?html?$|^#|^javascript:")


def get(url: str, ref: str = "", **kw) -> httpx.Response:
    # 如皋的 truecms 接口不带 Referer 直接回 404，其他站不挑，统一带上栏目页地址
    h = {**UA, "Referer": ref} if ref else UA
    return httpx.get(url, headers=h, timeout=30, follow_redirects=True, **kw)


def links_in(markup: str, origin: str) -> list[tuple[str, str]]:
    """从任意 HTML / XML 片段里捞 (URL, 标题)。

    各站的 <a> 写法差得很远：盐城 href 在前单引号、阜宁 title 在前双引号、
    东台干脆没有 title 属性。所以按属性逐个取，取不到 title 就退回锚文本
    （会被 … 截断，详情页里能拿回全名）。
    """
    out = []
    for tag, inner in re.findall(r"(<a\b[^>]*>)(.*?)</a>", markup, re.S):
        m = re.search(r"href=[\"']([^\"']+)[\"']", tag)
        if not m:
            continue
        href = H.unescape(m.group(1))
        if SKIP.search(href) or not ART.search(href):
            continue
        t = re.search(r"title=[\"']([^\"']*)[\"']", tag)
        text = t.group(1) if t and t.group(1).strip() else re.sub(r"<[^>]+>", "", inner)
        url = href if href.startswith("http") else urllib.parse.urljoin(origin, href)
        out.append((url, H.unescape(text).strip()))
    return out


# ---------- 家族识别 ----------

def sniff(url: str) -> tuple[str, dict]:
    r = get(url)
    t, origin = r.text, "https://" + urllib.parse.urlparse(str(r.url)).netloc
    if urllib.parse.urlparse(str(r.url)).scheme == "http":
        origin = "http://" + urllib.parse.urlparse(str(r.url)).netloc
    cfg = {"origin": origin, "page1": t, "url": str(r.url)}

    if "dataproxy.jsp" in t:
        m = re.search(r"dataproxy\.jsp\?([^'\"]+)", t)
        proxy = re.search(r"proxyUrl:'([^']+)'", t)
        cfg["q"] = dict(urllib.parse.parse_qsl(H.unescape(m.group(1))))
        cfg["api"] = origin + (proxy.group(1) if proxy else "/module/web/jpage/dataproxy.jsp")
        return "dahan", cfg
    if "module/xxgk/" in t and "dataproxy.jsp" not in t:
        # 大汉版通的「信息公开」模块：列表由 POST search.jsp 画出来，翻页认 currpage。
        # search.jsp 不按栏目过滤，返回全区的信息公开条目，所以要按 art_<栏目号>_ 收窄
        area = re.search(r"tree\.jsp\?area=([^&\"']+)", t)
        col = re.search(r"col(\d+)", url)
        if area:
            cfg["api"] = origin + "/module/xxgk/search.jsp"
            cfg["q"] = {"infotypeId": "", "vc_title": "", "vc_number": "", "area": area.group(1)}
            cfg["only"] = f"art_{col.group(1)}_" if col else ""
            return "xxgk", cfg
    if "messageController/getMessage.do" in t:
        cid = re.search(r"columnId:'([^']+)'", t)
        proxy = re.search(r"proxyUrl:'([^']+)'", t)
        if cid:
            cfg["column_id"] = cid.group(1)
            cfg["api"] = origin + proxy.group(1)
            return "truecms", cfg
    if "jpaas-publish-server" in t:
        need = {}
        for k in ("webId", "pageId", "tplSetId", "tagId"):
            m = re.search(rf"{k}[\"']?\s*[:=]\s*[\"']([^\"']+)", t)
            if m:
                need[k] = m.group(1)
        if {"webId", "pageId", "tplSetId"} <= need.keys():
            need.update(parseType="bulidstatic", pageType="column")
            cfg["q"] = need
            cfg["api"] = origin + "/api-gateway/jpaas-publish-server/front/page/build/unit"
            return "jpaas", cfg
    if "govInfoPub" in url or "EWB-FRONT" in t:
        try:
            js = get(origin + "/js/webBuilderCommon.js").text
            info = json.loads(re.search(r"var siteInfo\s*=\s*(\{.*?\})", js).group(1))
        except Exception:
            info = None
        if info:
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            cfg["api"] = origin + info["projectName"] + "/rest/lightfrontaction/getgovinfolist"
            cfg["body"] = {"deptcode": q.get("deptcode", ""), "categorynum": q.get("categorynum", ""),
                           "siteGuid": info["siteGuid"]}
            return "ewb", cfg
    return "static", cfg


# ---------- 各家族的翻页 ----------

def pull_dahan(cfg, maxp):
    """startrecord 被服务端忽略，只认 page；perpage 超过 ~300 会被截断，压到 100。"""
    seen, rows = set(), []
    for pg in range(1, maxp + 1):
        q = dict(cfg["q"], page=pg, perpage=100, startrecord=1, endrecord=100)
        r = get(cfg["api"], cfg["url"], params=q)
        new = [x for x in links_in(r.text, cfg["origin"]) if x[0] not in seen]
        if not new:
            break
        seen.update(u for u, _ in new)
        rows += new
    return rows


def _truecms_page(cfg, start, win):
    r = get(cfg["api"], cfg["url"],
            params={"startrecord": start, "endrecord": start + win - 1,
                    "perpage": win, "columnId": cfg["column_id"], "callback": "cb"})
    if r.status_code != 200:
        return None
    m = re.search(r"\bcb\((.*)\)\s*;?\s*$", r.text, re.S)
    if not m:
        return None
    body = json.loads(m.group(1)).get("result", "")
    return links_in(body, cfg["origin"]) if body else []


def pull_truecms(cfg, maxp):
    """翻页认 startrecord/endrecord，响应是 jsonp 包一层 XML。

    每个站的 perpage 上限都不一样——南通 50 可以、100 回空，如皋 50 直接 404——
    所以先从大到小试一遍，谈出这个站能吃的窗口再开始翻。
    """
    win = next((w for w in (50, 20, 10) if _truecms_page(cfg, 1, w)), 0)
    if not win:
        return []
    seen, rows, start = set(), [], 1
    for _ in range(maxp):
        batch = _truecms_page(cfg, start, win)
        if batch is None:
            break
        new = [x for x in batch if x[0] not in seen]
        seen.update(u for u, _ in new)
        rows += new
        if not new or len(batch) < win:
            break
        start += win
    return rows


def pull_jpaas(cfg, maxp):
    """翻页参数必须包进 paramJson，裸 pageNo / page / currentPage 全部无效。"""
    seen, rows = set(), []
    for pg in range(1, maxp + 1):
        q = dict(cfg["q"], paramJson=json.dumps({"pageNo": pg, "pageSize": "15"}))
        r = get(cfg["api"], cfg["url"], params=q)
        d = r.json()
        markup = (d.get("data") or {}).get("html") or (d.get("data") if isinstance(d.get("data"), str) else "")
        new = [x for x in links_in(markup or "", cfg["origin"]) if x[0] not in seen]
        if not new:
            break
        seen.update(u for u, _ in new)
        rows += new
    return rows


def pull_ewb(cfg, maxp):
    seen, rows = set(), []
    for pg in range(1, maxp + 1):
        body = dict(cfg["body"], pageIndex=pg, pageSize=50)
        r = httpx.post(cfg["api"], json=body, headers={**UA, "Referer": cfg["url"]}, timeout=40)
        items = ((r.json().get("custom") or {}).get("data")) or []
        new = [(cfg["origin"] + it["infourl"], it.get("realtitle") or it.get("title"))
               for it in items if cfg["origin"] + it["infourl"] not in seen]
        if not new:
            break
        seen.update(u for u, _ in new)
        rows += new
    return rows


def page_templates(url: str):
    """静态列表页的候选翻页写法，实测这几种覆盖了江苏所有市。"""
    base, _, query = url.partition("?")
    q = f"?{query}" if query else ""
    stem, dot, ext = base.rpartition(".")
    if dot:                                    # a_list.shtml / index.html
        yield lambda p: f"{stem}_{p - 1}.{ext}{q}"
        yield lambda p: f"{stem}_{p}.{ext}{q}"
        d = base.rsplit("/", 1)[0]
        yield lambda p: f"{d}/index_{p - 1}.html{q}"
        yield lambda p: f"{d}/index_{p}.html{q}"
    else:                                      # 目录式 .../ 或 /class/XXXX
        d = base.rstrip("/")
        yield lambda p: f"{d}/{p}{q}"
        yield lambda p: f"{d}/index_{p - 1}.html{q}"
        yield lambda p: f"{d}/index_{p}.html{q}"


def _prefix(u: str) -> str:
    return "/".join(urllib.parse.urlparse(u).path.split("/")[:3])


def pull_static(cfg, maxp):
    """静态列表页：试出翻页模板，然后只收和第 1 页同栏目的链接。

    没有这层约束会翻进别的栏目——常州新北的 /class/XXXX/2 返回的是首页新闻，
    沭阳的 xxgk_list_N.shtml 会串到统计局、城管局的公开目录去。
    """
    rows = links_in(cfg["page1"], cfg["origin"])
    if not rows:
        return rows
    # 本栏目的路径前缀优先取入口 URL 自己的——虎丘那页顶上挂着「批准项目公告」的
    # 最新条目，按页面多数算会把入口所属的 hjbh 栏目整个滤掉
    own = _prefix(cfg["url"])
    if not any(_prefix(u) == own for u, _ in rows):
        own = collections.Counter(_prefix(u) for u, _ in rows).most_common(1)[0][0]
    rows = [x for x in rows if _prefix(x[0]) == own]
    seen = {u for u, _ in rows}

    def fresh(text):
        return [x for x in links_in(text, cfg["origin"])
                if _prefix(x[0]) == own and x[0] not in seen]

    tpl = None
    for cand in page_templates(cfg["url"]):
        try:
            r = get(cand(2))
        except Exception:
            continue
        if r.status_code == 200 and fresh(r.text):
            tpl = cand
            break
    if tpl is None:
        return rows
    for pg in range(2, maxp + 1):
        try:
            r = get(tpl(pg))
        except Exception:
            break
        if r.status_code != 200:
            break
        new = fresh(r.text)
        if not new:
            break
        seen.update(u for u, _ in new)
        rows += new
        time.sleep(0.3)
    return rows


def pull_xxgk(cfg, maxp):
    seen, rows = set(), []
    for pg in range(1, maxp + 1):
        r = httpx.post(cfg["api"], params=dict(cfg["q"], currpage=pg),
                       headers={**UA, "Referer": cfg["url"]}, timeout=30)
        if r.status_code != 200:
            break
        batch = [x for x in links_in(r.text, cfg["origin"]) if cfg["only"] in x[0]]
        new = [x for x in batch if x[0] not in seen]
        if not new:
            break
        seen.update(u for u, _ in new)
        rows += new
        time.sleep(0.2)
    return rows


PULL = {"dahan": pull_dahan, "xxgk": pull_xxgk, "truecms": pull_truecms, "jpaas": pull_jpaas,
        "ewb": pull_ewb, "static": pull_static}


def inner_frame(cfg) -> str:
    """常州区县、淮安区县的栏目页只是个壳，真列表在同域 iframe 里。"""
    host = urllib.parse.urlparse(cfg["url"]).netloc
    for src in re.findall(r"<iframe[^>]*src=[\"']([^\"']+)", cfg["page1"]):
        u = urllib.parse.urljoin(cfg["url"], H.unescape(src))
        if urllib.parse.urlparse(u).netloc != host:
            continue
        if re.search(r"visitcount|login|tree\.jsp|share|footer", u):
            continue
        return u
    return ""


def collect(url: str, maxp: int = 80) -> tuple[str, list[tuple[str, str]]]:
    fam, cfg = sniff(url)
    rows = PULL[fam](cfg, maxp)
    if not rows and fam == "static":
        inner = inner_frame(cfg)
        if inner:
            fam2, cfg2 = sniff(inner)
            rows = PULL[fam2](cfg2, maxp)
            if rows:
                return f"{fam2}(iframe)", rows
    return fam, rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--max-pages", type=int, default=80)
    args = ap.parse_args()
    fam, rows = collect(args.url, args.max_pages)
    print(f"{fam}: {len(rows)} 条", file=sys.stderr)
    for u, t in rows:
        print(f"{u}\t{t}")


if __name__ == "__main__":
    main()
