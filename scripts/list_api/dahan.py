"""大汉版通 CMS 的列表接口：/module/web/jpage/dataproxy.jsp，靠 startrecord/endrecord 取全量。
盐城系（市局、阜宁、东台）都是这套。参数从栏目页 HTML 里的 proxyUrl 串抠出来。"""
import html as H
import re
import sys
import urllib.parse
import httpx

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"}

def discover(col_url):
    r = httpx.get(col_url, headers=UA, timeout=30, follow_redirects=True)
    r.raise_for_status()
    t = r.text
    m = re.search(r"dataproxy\.jsp\?([^'\"]+)", t)
    if not m:
        raise SystemExit("没找到 dataproxy 参数")
    q = dict(urllib.parse.parse_qsl(H.unescape(m.group(1))))
    proxy = re.search(r"proxyUrl:'([^']+)'", t)
    path = proxy.group(1) if proxy else "/module/web/jpage/dataproxy.jsp"
    origin = "https://" + urllib.parse.urlparse(col_url).netloc
    return origin + path, q, origin

def fetch(url, q, origin, ref, start=1, n=2000):
    p = dict(q)
    p.update(startrecord=start, endrecord=start + n - 1, perpage=n)
    r = httpx.get(url, params=p, headers={**UA, "Referer": ref}, timeout=60)
    r.raise_for_status()
    out = []
    # 各站的 <a> 写法都不一样：盐城 href 在前单引号、阜宁 title 在前双引号、
    # 东台干脆没有 title 属性，只能退回锚文本（会被 … 截断，详情页里能拿到全名）
    for tag, inner in re.findall(r"(<a\b[^>]*>)(.*?)</a>", r.text, re.S):
        href = re.search(r"href=[\"']([^\"']+)[\"']", tag)
        if not href or not re.search(r"/art/|art_", href.group(1)):
            continue
        title = re.search(r"title=[\"']([^\"']*)[\"']", tag)
        text = title.group(1) if title else re.sub(r"<[^>]+>", "", inner)
        u = href.group(1)
        out.append(((u if u.startswith("http") else origin + u), H.unescape(text).strip()))
    total = re.search(r"<totalrecord>(\d+)</totalrecord>", r.text)
    print(f"totalrecord={total.group(1) if total else '?'} 取到 {len(out)}", file=sys.stderr)
    return out

def fetch_all(url, q, origin, ref, per=100, maxpage=60):
    """startrecord 被服务端忽略，翻页只认 page；单次返回还封顶 300 条
    （东台 621 条用 perpage=2000 只回 301），所以 perpage 压到 100 再按 page 滚。"""
    seen, rows = set(), []
    for pg in range(1, maxpage + 1):
        q2 = dict(q)
        q2["page"] = pg
        batch = fetch(url, q2, origin, ref, n=per)
        new = [(u, t) for u, t in batch if u not in seen]
        for u, t in new:
            seen.add(u)
            rows.append((u, t))
        if not new:
            break
    return rows


if __name__ == "__main__":
    col = sys.argv[1]
    url, q, origin = discover(col)
    for u, t in fetch_all(url, q, origin, col):
        print(f"{u}\t{t}")
