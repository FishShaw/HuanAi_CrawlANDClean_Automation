"""宁波的环评公示在独立系统 jyh.nbepb.gov.cn 上，列表和详情都是 JSON，单独抽样。

- 列表：/Json/CPA/List-1-<classid>.json，classid 0=受理、1=拟批准意见、2=审批决定
- 详情：/Json/CPA/Detail-<infoid>.json，flowlist 每个项目带 filelist
- 附件：/UploadFiles_CPA/<相对路径>（门户上是 pdf.js 阅读器 + 加密 fileurl，真实地址是这个）

附件名字只有「附件1」，所以判定靠 report_stage_probe.is_report 的大小规则。
输出列和 report_stage_probe.py 一致，可以拼进同一份汇总。

    .venv/bin/python scripts/report_stage_probe_ningbo.py --per-stage 12 >> out.tsv
"""
from __future__ import annotations

import argparse
import random
import sys
import urllib.parse
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from report_stage_probe import _size, is_report, pdf_pages  # noqa: E402

BASE = "https://jyh.nbepb.gov.cn"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
      "Referer": f"{BASE}/CPA/Jsxm.html?typeid=1&classid=0"}
STAGES = {0: "受理", 1: "拟审批", 2: "审批决定"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-stage", type=int, default=12)
    ap.add_argument("--header", action="store_true")
    args = ap.parse_args()
    random.seed(7)
    if args.header:
        print("省\t市\t阶段\t栏目\t公告\t标题\t官方报告附件数\t非官方报告附件数\t附件样例\t全部附件数\t全部附件\t错误")
    for cid, stage in STAGES.items():
        lst = httpx.get(f"{BASE}/Json/CPA/List-1-{cid}.json", headers=UA, timeout=30).json()["list"]
        # 只抽近两年：老公告的附件不少已经下线，会把结论拉偏
        recent = [x for x in lst if x["updatetime"] >= "2024-10-01"]
        print(f"宁波 {stage}: 共 {len(lst)} 条，近两年 {len(recent)} 条", file=sys.stderr)
        for it in random.sample(recent, min(args.per_stage, len(recent))):
            url = f"{BASE}/CPA/Show.html?InfoID={it['infoid']}"
            try:
                det = httpx.get(f"{BASE}/Json/CPA/Detail-{it['infoid']}.json", headers=UA, timeout=30).json()["info"]
                files = [f for p in det.get("flowlist") or [] for f in p.get("filelist") or []]
                hits, every = [], []
                for f in files:
                    href = f"{BASE}/UploadFiles_CPA/{urllib.parse.quote(f['url'])}"
                    size = _size(href)
                    every.append(f"{f.get('name', '')} [{size / 1e6:.1f}MB]")
                    # 只要回答「这条公告挂没挂报告」，命中一个就停：宁波附件动辄 20–35MB，
                    # 页树不在头尾时要整份下载数页，全数一遍要半小时
                    if not hits and is_report(f.get("name", "") + " " + f["url"], size,
                                              lambda h=href: pdf_pages(h)):
                        hits.append(every[-1])
                row = ("浙江", "宁波市", stage, f"{BASE}/Json/CPA/List-1-{cid}.json", url, it["title"],
                       len(hits), 0, " | ".join(hits[:2]), len(every), " | ".join(every[:4]), "")
            except Exception as e:
                row = ("浙江", "宁波市", stage, "", url, it["title"], 0, 0, "", 0, "", type(e).__name__)
            print("\t".join(str(x).replace("\t", " ") for x in row), flush=True)


if __name__ == "__main__":
    main()
