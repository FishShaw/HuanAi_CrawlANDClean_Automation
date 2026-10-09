"""给主数据里还没找到入口的单位，从青绿各取几条受理记录的完整字段（项目名、原文链接等）。

清洗只留了发布单位和日期，找官网栏目时缺抓手：有了原文链接能直接反推栏目，
有了项目名可以拿去门户站内搜。每个单位只查它最后一条公告前后的一个短窗口，一单位一次请求。

    .venv/bin/python scripts/iesg_samples.py --province 330000 --out <路径> --prompt-token
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import sys
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crawl_jiangsu_eia import LIST_URL, PROC_FIELD, Stop, call  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--province", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--status", default="未核实",
                    help="只取这些核验情况的单位，逗号分隔；verified / unverified 是「已核实」「未核实」的别名")
    ap.add_argument("--per-unit", type=int, default=5)
    ap.add_argument("--days", type=int, default=20, help="最后一条公告往前取多少天")
    ap.add_argument("--interval", type=float, default=1.5)
    ap.add_argument("--prompt-token", action="store_true")
    args = ap.parse_args()

    token = getpass.getpass("令牌：") if args.prompt_token else os.environ.get("I_ESG_TOKEN", "")
    token = token.removeprefix("Bearer ").strip()
    if not token:
        sys.exit("没有令牌：设置环境变量 I_ESG_TOKEN，或者加 --prompt-token")

    tree = json.loads((ROOT / "refs" / f"area_tree_{args.province}.json").read_text(encoding="utf-8"))["地区树"]
    master = list(csv.DictReader((ROOT / f"{tree['名称']}受理单位主数据.csv").open(encoding="utf-8-sig")))
    alias = {"verified": "已核实", "unverified": "未核实"}
    want = {alias.get(s, s) for s in args.status.split(",")}
    units = [r for r in master if r["verify_status"] in want and r["city_code"] and r["last_notice_date"]]

    out = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {}
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
    with httpx.Client(headers=headers, timeout=60) as client:
        for i, u in enumerate(units, 1):
            uid = u["unit_uid"]
            if uid in out:
                continue
            names = {u["name_from_source"], *filter(None, (u["name_variants"] or "").split(" / "))}
            end = date.fromisoformat(u["last_notice_date"])
            start = end - timedelta(days=args.days)
            params = {"cityCode": u["city_code"], "endNoticeDate": end.isoformat(), "method": "get",
                      "moduleTypeCode": 1, "pageNum": 1, "pageSize": 500,
                      "startNoticeDate": start.isoformat(), "url": LIST_URL}
            try:
                data = call(client, params, f"{u['name_from_source']} {start}~{end}") or {}
            except Stop as e:
                print(f"停止：{e}")
                break
            hits = [v for v in data.get("values") or [] if (v.get(PROC_FIELD) or "").strip() in names]
            out[uid] = {"单位": u["name_from_source"], "城市": u["city_code"], "样本": hits[:args.per_unit]}
            print(f"[{i}/{len(units)}] {u['name_from_source']}: {len(hits)} 条")
            args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            time.sleep(args.interval)
    print(f"写入 {args.out}，{sum(1 for v in out.values() if v['样本'])}/{len(out)} 个单位有样本")


if __name__ == "__main__":
    main()
