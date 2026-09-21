#!/usr/bin/env python3
"""黄山市（341000）抓取。薄封装：逻辑在 crawl_jiangsu_eia.py，这里只固定市和输出路径。

    python cities/crawl_341000_黄山市.py --prompt-token
    I_ESG_TOKEN='...' python cities/crawl_341000_黄山市.py

支持引擎的全部参数，例如 --modules 1,3,5 / --interval 2 / --restart / --no-proxy。
中断后重跑同一条命令会从断点续上。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawl_jiangsu_eia import main  # noqa: E402

PROVINCE, CITY, NAME = "340000", "341000", "黄山市"

if __name__ == "__main__":
    out = ROOT / "raw" / f"{CITY}_{NAME}.json"
    out.parent.mkdir(exist_ok=True)
    sys.argv[1:] = [*sys.argv[1:], "--province", PROVINCE, "--cities", CITY, "--output", str(out)]
    main()
