"""徐州市局列表：https 整站 403，http 可用；列表走 POST /EWB-FRONT/rest/lightfrontaction/getgovinfolist。
categorynum 003011=环评审批。"""
import sys
import time
import httpx

ORIGIN = "http://sthj.xz.gov.cn"
SITE = "fb2ed5f8-902e-48bd-998f-a622a60c0951"
H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
     "Referer": f"{ORIGIN}/dynamic/zwgk/govInfoPub.html?categorynum=003011"}

cat = sys.argv[1] if len(sys.argv) > 1 else "003011"
seen, page = set(), 1
while True:
    r = httpx.post(f"{ORIGIN}/EWB-FRONT/rest/lightfrontaction/getgovinfolist",
                   json={"deptcode": "", "categorynum": cat, "pageIndex": page,
                         "pageSize": 50, "siteGuid": SITE}, headers=H, timeout=40)
    r.raise_for_status()
    d = (r.json().get("custom") or {})
    items = d.get("data") or []
    new = 0
    for it in items:
        u = ORIGIN + it["infourl"]
        if u in seen:
            continue
        seen.add(u)
        new += 1
        print(f"{u}\t{it.get('realtitle') or it.get('title')}")
    print(f"page {page}: {len(items)} 条, 新增 {new}, 累计 {len(seen)}/{d.get('total')}", file=sys.stderr)
    if not items or not new:
        break
    page += 1
    time.sleep(0.4)
