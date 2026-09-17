#!/usr/bin/env python3
"""抓取：青绿数据（i-esg.com）南京市环评受理列表，导出「受理单位」原始记录。

接口：POST /environment/ep-query/doAction
    body = {"data": AES-128-ECB(明文, key="DaoGuangJianYing") 的 base64}
    明文是按参数名排序的表单串，例如
    cityCode=320100&method=get&moduleTypeCode=1&pageNum=1&pageSize=500&url=/ep-query/environmentalAssess/getList

    cityCode=320100 是南京市，已包含其下所有区县；moduleTypeCode=1 是环评受理。

两个单位字段都存下来，清洗时用 --field 选：
    processDepartment                     列表页「受理单位」列，一条公告一个发布单位
    acceptanceMonitorDepartmentForExport  受理/监督单位，常是「市局,区局」这样的组合值

需要登录：先在浏览器登录 i-esg.com，从任一 doAction 请求头 Authorization 里
取 Bearer 之后的内容。令牌不写进脚本、不落盘。

    python crawl_nanjing_eia.py --prompt-token     # 运行时隐藏输入
    I_ESG_TOKEN='...' python crawl_nanjing_eia.py  # 或走环境变量

输出 南京市环评受理_原始记录.json，交给 clean_nanjing_eia.py 清洗。
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
import time
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

API = "https://www.i-esg.com/environment/ep-query/doAction"
AES_KEY = b"DaoGuangJianYing"
CITY_CODE = "320100"  # 南京市
MODULE = 1  # 环评受理
PROC_FIELD = "processDepartment"  # 列表页「受理单位」列
EXPORT_FIELD = "acceptanceMonitorDepartmentForExport"  # 受理/监督单位，可能是组合值
PAGE_SIZE = 500
MAX_PAGES = 60  # 分页上限，异常响应时不至于死循环
INTERVAL = 1.0  # 每次请求最小间隔秒数
OUT = Path(__file__).resolve().parent / "南京市环评受理_原始记录.json"


def encrypt(params: dict) -> str:
    plain = urlencode(sorted(params.items()), safe="/,")
    return base64.b64encode(AES.new(AES_KEY, AES.MODE_ECB).encrypt(pad(plain.encode(), 16))).decode()


def fetch_page(client: httpx.Client, page: int) -> dict:
    body = {"data": encrypt({"cityCode": CITY_CODE, "method": "get", "moduleTypeCode": MODULE,
                             "pageNum": page, "pageSize": PAGE_SIZE,
                             "url": "/ep-query/environmentalAssess/getList"})}
    for attempt in range(3):
        try:
            r = client.post(API, json=body)
            r.raise_for_status()
            j = r.json()
            code = j.get("code")
            if code == 200:
                return j["data"] or {}
            if code in (100000, 100002):
                sys.exit("令牌无效或已过期，重新登录网站复制 Authorization")
            if code == 100006:
                sys.exit("当日访问额度已用完（100006），明天再跑")
            if code == 100007:
                sys.exit("账号权限不足（100007）")
            sys.exit(f"接口返回 {code} {j.get('msg')}")
        except (httpx.HTTPError, ValueError) as e:
            if attempt == 2:
                sys.exit(f"第 {page} 页连续失败：{e}")
            wait = 5 * (attempt + 1)
            print(f"  第 {page} 页失败（{e}），{wait}s 后重试")
            time.sleep(wait)
    raise AssertionError("unreachable")


def crawl(client: httpx.Client) -> tuple[list[list[str]], int]:
    """返回 ([[processDepartment, 组合单位字段, 公告日期], ...], 接口报告的总数)。"""
    records: list[list[str]] = []
    total = None
    outsiders = 0
    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(client, page)
        if total is None:
            total = data.get("total") or 0
            print(f"接口总数 {total}")
        values = data.get("values") or []
        if not values:
            break
        for v in values:
            if v.get("cityName") != "南京":  # 城市筛选应当只回南京，异常时记下来
                outsiders += 1
            records.append([(v.get(PROC_FIELD) or "").strip(),
                            (v.get(EXPORT_FIELD) or "").strip(),
                            v.get("noticeDate") or ""])
        print(f"  第 {page} 页，累计 {len(records)}/{total}")
        if len(records) >= total:
            break
        time.sleep(INTERVAL)
    if total and len(records) != total:
        sys.exit(f"只取到 {len(records)}/{total} 条，未取全，停止")
    if outsiders:
        print(f"注意：有 {outsiders} 条记录的城市不是南京")
    return records, total or 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prompt-token", action="store_true", help="运行时隐藏输入令牌，不经过环境变量")
    p.add_argument("--output", type=Path, default=OUT, help=f"输出 JSON，默认 {OUT.name}")
    args = p.parse_args()

    token = getpass.getpass("令牌：") if args.prompt_token else os.environ.get("I_ESG_TOKEN", "")
    token = token.removeprefix("Bearer ").strip()
    if not token:
        sys.exit("没有令牌：设置环境变量 I_ESG_TOKEN，或者加 --prompt-token")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json;charset=UTF-8",
        "client": "PC", "system-info": "PC", "app-version": "0.0.3-beta.01",
        "app-id": f"{uuid.uuid4()};{int(time.time() * 1000)}",
        "Origin": "https://www.i-esg.com",
        "Referer": "https://www.i-esg.com/envAssessment/assessList/1",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"),
    }
    with httpx.Client(headers=headers, timeout=30) as client:
        records, total = crawl(client)

    args.output.write_text(json.dumps(
        {"抓取日期": date.today().isoformat(), "接口总数": total, "记录": records},
        ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(records)} 条记录 → {args.output}")


if __name__ == "__main__":
    main()
