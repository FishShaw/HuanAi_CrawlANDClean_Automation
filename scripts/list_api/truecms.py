"""truecms（南通系）列表接口：/truecms/messageController/getMessage.do，
jsonp 包一层 XML，翻页认 startrecord/endrecord。"""
import html as H
import json
import re
import sys
import time
import httpx

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"}

def fetch(origin, column_id, ref, start, n):
    r = httpx.get(f"{origin}/truecms/messageController/getMessage.do",
                  params={"startrecord": start, "endrecord": start + n - 1, "perpage": n,
                          "columnId": column_id, "callback": "cb"},
                  headers={**UA, "Referer": ref}, timeout=60)
    r.raise_for_status()
    m = re.search(r"\bcb\((.*)\)\s*;?\s*$", r.text, re.S)
    body = json.loads(m.group(1))["result"] if m else r.text
    out = []
    for tag, inner in re.findall(r"(<a\b[^>]*>)(.*?)</a>", body, re.S):
        m = re.search(r'href="([^"]+)"', tag)
        if not m or "/content/" not in m.group(1):
            continue
        u = m.group(1)
        out.append(((u if u.startswith("http") else origin + u),
                    H.unescape(re.sub(r"<[^>]+>", "", inner)).strip()))
    return out

if __name__ == "__main__":
    origin, column_id, ref = sys.argv[1], sys.argv[2], sys.argv[3]
    seen, rows, start, win = set(), [], 1, 50  # perpage 到 100 服务端直接回空
    while True:
        batch = fetch(origin, column_id, ref, start, win)
        new = [(u, t) for u, t in batch if u not in seen]
        for u, t in new:
            seen.add(u)
            rows.append((u, t))
        print(f"start={start} 取回 {len(batch)} 新增 {len(new)} 累计 {len(rows)}", file=sys.stderr)
        if not new or len(batch) < win:
            break
        start += win
        time.sleep(0.3)
    for u, t in rows:
        print(f"{u}\t{t}")
