#!/usr/bin/env python3
"""马鞍山市（340500）清洗。薄封装：逻辑在 clean_jiangsu_eia.py，这里只固定输入输出路径。

    python cities/clean_340500_马鞍山市.py
    python cities/clean_340500_马鞍山市.py --field export

单市结果只用于查看核对。**全省表不要拿这些 CSV 拼接**——省厅、生态环境部在每个市的
列表里都会出现，拼接会重复计数；跨市错标的记录也要全省一起看才能归位。
合并用 merge_jiangsu.py，它回到原始记录做一次全省统一清洗。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clean_jiangsu_eia import main  # noqa: E402

CITY, NAME = "340500", "马鞍山市"

if __name__ == "__main__":
    out = ROOT / "by_city" / f"{CITY}_{NAME}.csv"
    out.parent.mkdir(exist_ok=True)
    sys.argv[1:] = [*sys.argv[1:],
                    "--input", str(ROOT / "raw" / f"{CITY}_{NAME}.json"),
                    "--output", str(out)]
    main()
