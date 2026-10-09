"""jpaas 建站系统的列表接口：翻页参数是 paramJson={"pageNo":N,"pageSize":"15"}。
泰州/扬州/盐城/徐州这批 JS 渲染的列表都是这套 CMS。"""
import json
import re
import sys
import time
import urllib.parse
import httpx

def fetch(base, params, page, size=15):
    q = dict(params)
    q["paramJson"] = json.dumps({"pageNo": page, "pageSize": str(size)}, ensure_ascii=False)
    r = httpx.get(base, params=q, timeout=30, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    })
    r.raise_for_status()
    d = r.json()
    html = (d.get("data") or {}).get("html") or d.get("html") or ""
    if not html and isinstance(d.get("data"), str):
        html = d["data"]
    return html

LINK = re.compile(r'href="(/[^"]*?art_[0-9a-f]{32}\.html)"[^>]*(?:title="([^"]*)")?')

def links(html, origin):
    out = []
    for m in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href, inner = m.group(1), m.group(2)
        if not re.search(r"art_[0-9a-f]{16,}\.html|/art/", href):
            continue
        t = re.sub(r"<[^>]+>", "", inner).strip()
        mt = re.search(r'title="([^"]*)"', m.group(0))
        if mt and mt.group(1).strip():
            t = mt.group(1).strip()
        url = href if href.startswith("http") else origin + href
        out.append((url, t))
    return out

if __name__ == "__main__":
    base = sys.argv[1]
    origin = "https://" + urllib.parse.urlparse(base).netloc
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(base).query))
    base = base.split("?")[0]
    maxp = int(sys.argv[2])
    seen, rows = set(), []
    for p in range(1, maxp + 1):
        try:
            html = fetch(base, params, p)
        except Exception as e:
            print(f"page {p} 失败: {e}", file=sys.stderr)
            break
        ls = links(html, origin)
        new = [(u, t) for u, t in ls if u not in seen]
        for u, t in new:
            seen.add(u)
            rows.append((u, t))
        print(f"page {p}: {len(ls)} 条, 新增 {len(new)}, 累计 {len(rows)}", file=sys.stderr)
        if not ls or not new:
            break
        time.sleep(0.6)
    for u, t in rows:
        print(f"{u}\t{t}")
