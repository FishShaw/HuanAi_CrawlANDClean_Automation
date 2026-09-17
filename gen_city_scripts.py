#!/usr/bin/env python3
"""按地区树给每个市生成一对 crawl / clean 脚本，放在 cities/ 下。

    python gen_city_scripts.py --tree trees/330000_浙江省.json   # 换省：先 crawl_jiangsu_eia.py --tree-only 取树
    python gen_city_scripts.py --tree raw/320100_南京市.json     # 任何一份原始记录里也带地区树

生成的是**薄封装**：只固定市编码和输入输出路径，抓取和清洗逻辑仍在
crawl_jiangsu_eia.py / clean_jiangsu_eia.py 里。改规则只改那两个文件，
不用动这 26 个封装——这也是不给每个市复制一份完整逻辑的原因。

每市各自跑：

    .venv/bin/python cities/crawl_320500_苏州市.py --prompt-token
    .venv/bin/python cities/clean_320500_苏州市.py

全部跑完后合并成全省表：

    .venv/bin/python merge_jiangsu.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CITIES_DIR = HERE / "cities"
# 地区树在任何一份原始记录里都有，随便挑一份读
DEFAULT_TREE = next(iter(sorted((HERE / "raw").glob("*.json"))), HERE / "raw" / "（还没有）.json")

CRAWL_TEMPLATE = '''#!/usr/bin/env python3
"""{name}（{code}）抓取。薄封装：逻辑在 crawl_jiangsu_eia.py，这里只固定市和输出路径。

    python cities/{crawl_file} --prompt-token
    I_ESG_TOKEN='...' python cities/{crawl_file}

支持引擎的全部参数，例如 --modules 1,3,5 / --interval 2 / --restart / --no-proxy。
中断后重跑同一条命令会从断点续上。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawl_jiangsu_eia import main  # noqa: E402

PROVINCE, CITY, NAME = "{province}", "{code}", "{name}"

if __name__ == "__main__":
    out = ROOT / "raw" / f"{{CITY}}_{{NAME}}.json"
    out.parent.mkdir(exist_ok=True)
    sys.argv[1:] = [*sys.argv[1:], "--province", PROVINCE, "--cities", CITY, "--output", str(out)]
    main()
'''

CLEAN_TEMPLATE = '''#!/usr/bin/env python3
"""{name}（{code}）清洗。薄封装：逻辑在 clean_jiangsu_eia.py，这里只固定输入输出路径。

    python cities/{clean_file}
    python cities/{clean_file} --field export

单市结果只用于查看核对。**全省表不要拿这些 CSV 拼接**——省厅、生态环境部在每个市的
列表里都会出现，拼接会重复计数；跨市错标的记录也要全省一起看才能归位。
合并用 merge_jiangsu.py，它回到原始记录做一次全省统一清洗。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clean_jiangsu_eia import main  # noqa: E402

CITY, NAME = "{code}", "{name}"

if __name__ == "__main__":
    out = ROOT / "by_city" / f"{{CITY}}_{{NAME}}.csv"
    out.parent.mkdir(exist_ok=True)
    sys.argv[1:] = [*sys.argv[1:],
                    "--input", str(ROOT / "raw" / f"{{CITY}}_{{NAME}}.json"),
                    "--output", str(out)]
    main()
'''


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tree", type=Path, default=DEFAULT_TREE,
                   help=f"含地区树的原始记录 JSON，默认 {DEFAULT_TREE.name}")
    args = p.parse_args()

    if not args.tree.exists():
        raise SystemExit(f"没有 {args.tree}，先跑 crawl_jiangsu_eia.py --province <省编码> --tree-only 取回地区树")
    tree = json.loads(args.tree.read_text(encoding="utf-8"))["地区树"]

    CITIES_DIR.mkdir(exist_ok=True)
    for city in tree["市"]:
        code, name = city["编码"], city["名称"]
        crawl_file, clean_file = f"crawl_{code}_{name}.py", f"clean_{code}_{name}.py"
        (CITIES_DIR / crawl_file).write_text(
            CRAWL_TEMPLATE.format(province=tree["编码"], code=code, name=name, crawl_file=crawl_file), encoding="utf-8")
        (CITIES_DIR / clean_file).write_text(
            CLEAN_TEMPLATE.format(code=code, name=name, clean_file=clean_file), encoding="utf-8")
        print(f"  cities/{crawl_file}  cities/{clean_file}")
    print(f"\n{tree['名称']}：{len(tree['市'])} 个市，共 {len(tree['市']) * 2} 个脚本")


if __name__ == "__main__":
    main()
