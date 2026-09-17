#!/usr/bin/env python3
"""抓取：i-esg.com 某个省的环评单位列表，按市逐个抓，可断点续跑。

默认江苏省（320000）环评受理。地区树从站点接口取，不写死任何地名：

    /ep-query/screen/area   省(level 1) → 市(level 2) → 区县(level 3)，各级带 code / name / shortName

之所以按市逐个抓而不是传 provinceCode：一个省一次要翻 100 多页，深翻页没验证过，
中途失败也没法续；按市抓每市最多二十来页，天然可以断点续跑。cityCode 是验证过可用的。

需要登录：浏览器登录 i-esg.com 后，从任一 doAction 请求头 Authorization 里取 Bearer 之后的内容。
令牌不写进脚本、不落盘。

    python crawl_jiangsu_eia.py --prompt-token
    python crawl_jiangsu_eia.py --prompt-token --modules 1,3,5        # 多个模块
    python crawl_jiangsu_eia.py --prompt-token --cities 320100,320500 # 只抓指定市
    python crawl_jiangsu_eia.py --prompt-token --province 330000 --tree-only  # 换省第一步：只取地区树

中断（额度用完、限流、Ctrl-C）后重跑同一条命令即可，已抓完的市不会重抓；
要从头来加 --restart。输出交给 clean_jiangsu_eia.py 清洗。
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
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

API = "https://www.i-esg.com/environment/ep-query/doAction"
AES_KEY = b"DaoGuangJianYing"
AREA_URL = "/ep-query/screen/area"
LIST_URL = "/ep-query/environmentalAssess/getList"
MODULES = {1: "环评受理", 2: "环评拟审批", 3: "环评审批", 5: "环评验收"}
DEFAULT_PROVINCE = "320000"  # 江苏省
# 接口的 total 封顶在一万，超过就只给最近的一万条且不报错（2026-09-16 实测：
# 苏州市受理返回 total=10000，日期只到 2022-11-29，更早的全被吞掉）。
# 所以凡是 total 撞到这个数，就按公告日期把查询窗口二分，直到每个窗口都在封顶以下。
CAP = 10000
EARLIEST = "2000-01-01"  # 起始窗口下界，站点数据不会早于此
PROC_FIELD = "processDepartment"  # 列表页「受理/审批/发布单位」列
EXPORT_FIELD = "acceptanceMonitorDepartmentForExport"  # 受理/监督单位，可能是组合值
OUT = Path(__file__).resolve().parent / "江苏省环评单位_原始记录.json"


class Stop(Exception):
    """需要保存进度并退出的情况（额度、权限、令牌）。"""


def encrypt(params: dict) -> str:
    plain = urlencode(sorted(params.items()), safe="/,")
    return base64.b64encode(AES.new(AES_KEY, AES.MODE_ECB).encrypt(pad(plain.encode(), 16))).decode()


def call(client: httpx.Client, params: dict, what: str):
    """发一次请求，返回 data；接口明确拒绝时抛 Stop，网络问题重试 3 次。"""
    body = {"data": encrypt(params)}
    for attempt in range(3):
        try:
            r = client.post(API, json=body)
            r.raise_for_status()
            j = r.json()
            code = j.get("code")
            if code == 200:
                return j.get("data")
            if code in (100000, 100002):
                raise Stop("令牌无效或已过期，重新登录网站复制 Authorization")
            if code == 100006:
                raise Stop("当日访问额度已用完（100006），明天重跑同一条命令即可续上")
            if code == 100007:
                raise Stop("账号权限不足（100007）")
            raise Stop(f"{what} 返回 {code} {j.get('msg')}")
        except (httpx.HTTPError, ValueError) as e:
            if attempt == 2:
                raise Stop(f"{what} 连续 3 次失败：{e}") from e
            wait = 5 * (attempt + 1)
            print(f"  {what} 失败（{e}），{wait}s 后重试")
            time.sleep(wait)
    raise AssertionError("unreachable")


def area_tree(client: httpx.Client, province_code: str) -> dict:
    """取地区树，返回该省的 {名称, 简称, 市: [{编码, 名称, 简称, 区县: [...]}]}。"""
    tree = call(client, {"method": "get", "url": AREA_URL}, "地区树") or []
    node = next((p for p in tree if p.get("code") == province_code), None)
    if not node:
        raise Stop(f"地区树里没有编码 {province_code}")

    def brief(n: dict) -> dict:
        return {"编码": n.get("code"), "名称": n.get("name"), "简称": n.get("shortName")}

    cities = []
    for child in node.get("values") or []:
        # 直辖市的区县直接挂在省下（level 3），此时把省自身当作唯一的“市”
        if child.get("level") == 3:
            cities = [{**brief(node), "区县": [brief(c) for c in node.get("values") or []]}]
            break
        cities.append({**brief(child), "区县": [brief(d) for d in child.get("values") or []]})
    return {**brief(node), "市": cities}


def page_params(module: int, city: dict, page: int, page_size: int, start: str, end: str) -> dict:
    return {"cityCode": city["编码"], "endNoticeDate": end, "method": "get", "moduleTypeCode": module,
            "pageNum": page, "pageSize": page_size, "startNoticeDate": start, "url": LIST_URL}


def crawl_window(client: httpx.Client, module: int, city: dict, start: str, end: str,
                 page_size: int, interval: float, max_pages: int,
                 warnings: list[str]) -> tuple[list[list], int]:
    """抓一个市在 [start, end] 这段公告日期内的全部记录，撞到封顶就二分。"""
    tag = f"{MODULES[module]}/{city['名称']} {start}~{end}"
    data = call(client, page_params(module, city, 1, page_size, start, end), tag + " 第1页") or {}
    total = data.get("total") or 0
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)

    if total >= CAP:
        if d0 >= d1:  # 单日就超过封顶，没法再切，只能记下来
            warnings.append(f"{tag} 单日 total={total} 撞封顶，该日数据可能不全")
        else:
            mid = d0 + (d1 - d0) // 2
            print(f"  {tag} total={total} 撞封顶，二分")
            time.sleep(interval)
            left, n1 = crawl_window(client, module, city, start, mid.isoformat(),
                                    page_size, interval, max_pages, warnings)
            time.sleep(interval)
            right, n2 = crawl_window(client, module, city, (mid + timedelta(days=1)).isoformat(), end,
                                     page_size, interval, max_pages, warnings)
            return left + right, n1 + n2

    records: list[list] = []
    page = 1
    while True:
        values = data.get("values") or []
        if not values:
            break
        for v in values:
            records.append([module, city["编码"],
                            (v.get(PROC_FIELD) or "").strip(),
                            (v.get(EXPORT_FIELD) or "").strip(),
                            v.get("noticeDate") or "",
                            v.get("countryName") or "",
                            v.get("cityName") or ""])
        print(f"  {tag} 第{page}页，累计 {len(records)}/{total}")
        if len(records) >= total or page >= max_pages:
            break
        page += 1
        time.sleep(interval)
        data = call(client, page_params(module, city, page, page_size, start, end),
                    f"{tag} 第{page}页") or {}
    if len(records) != total:
        raise Stop(f"{tag} 只取到 {len(records)}/{total} 条，未取全")
    return records, total


def crawl_city(client: httpx.Client, module: int, city: dict, page_size: int, interval: float,
               max_pages: int, warnings: list[str]) -> tuple[list[list], int, int]:
    """抓一个市一个模块的全部记录。返回 (记录, 条数, 城市字段对不上的条数)。"""
    records, _ = crawl_window(client, module, city, EARLIEST, date.today().isoformat(),
                              page_size, interval, max_pages, warnings)
    mismatch = sum(1 for r in records if r[6] not in (city["名称"], city["简称"]))
    return records, len(records), mismatch


def save(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # 原子替换，中断也不会留半个文件


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prompt-token", action="store_true", help="运行时隐藏输入令牌，不经过环境变量")
    p.add_argument("--province", default=DEFAULT_PROVINCE, help=f"省编码，默认 {DEFAULT_PROVINCE}（江苏）")
    p.add_argument("--cities", help="只抓这些市编码，逗号分隔；默认全省")
    p.add_argument("--modules", default="1", help=f"模块，逗号分隔，默认 1。可选：{MODULES}")
    p.add_argument("--page-size", type=int, default=500)
    p.add_argument("--interval", type=float, default=1.5, help="每次请求最小间隔秒数")
    p.add_argument("--max-pages", type=int, default=200, help="单市分页上限")
    p.add_argument("--output", type=Path, default=OUT)
    p.add_argument("--restart", action="store_true", help="忽略已有进度，从头抓")
    p.add_argument("--tree-only", action="store_true",
                   help="只取地区树存到 trees/<省编码>_<省名>.json 就退出，给 gen_city_scripts.py 用（换省第一步）")
    p.add_argument("--no-proxy", action="store_true",
                   help="忽略环境变量里的代理直连（站点在国内，走代理往往更慢或被拒）")
    args = p.parse_args()

    modules = [int(m) for m in args.modules.split(",")]
    if bad := [m for m in modules if m not in MODULES]:
        sys.exit(f"不认识的模块 {bad}，可选 {sorted(MODULES)}")

    token = getpass.getpass("令牌：") if args.prompt_token else os.environ.get("I_ESG_TOKEN", "")
    token = token.removeprefix("Bearer ").strip()
    if not token:
        sys.exit("没有令牌：设置环境变量 I_ESG_TOKEN，或者加 --prompt-token")

    state = {"省编码": args.province, "抓取日期": date.today().isoformat(), "模块": modules,
             "地区树": None, "已完成": [], "城市统计": {}, "截断警告": [], "记录": []}
    if args.output.exists() and not args.restart:
        state = json.loads(args.output.read_text(encoding="utf-8"))
        state.setdefault("截断警告", [])
        if state.get("省编码") != args.province and not args.tree_only:
            sys.exit(f"{args.output} 是省编码 {state.get('省编码')} 的进度，和 --province {args.province} 不符；"
                     "换省请用新的 --output（或先跑 gen_city_scripts.py 生成该省的分市脚本）")
        print(f"续跑：已有 {len(state['记录'])} 条，已完成 {len(state['已完成'])} 个市×模块")

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
    done = {tuple(x) for x in state["已完成"]}
    try:
        with httpx.Client(headers=headers, timeout=60, trust_env=not args.no_proxy) as client:
            if args.tree_only:
                tree = area_tree(client, args.province)
                out = Path(__file__).resolve().parent / "trees" / f"{args.province}_{tree['名称']}.json"
                out.parent.mkdir(exist_ok=True)
                save(out, {"省编码": args.province, "抓取日期": state["抓取日期"], "地区树": tree})
                n = sum(len(c["区县"]) for c in tree["市"])
                print(f"{tree['名称']}：{len(tree['市'])} 个市、{n} 个区县 → {out}")
                return
            if not state["地区树"]:
                state["地区树"] = area_tree(client, args.province)
                save(args.output, state)
            prov = state["地区树"]
            cities = prov["市"]
            if args.cities:
                want = set(args.cities.split(","))
                cities = [c for c in cities if c["编码"] in want]
                if missing := want - {c["编码"] for c in cities}:
                    sys.exit(f"{prov['名称']}下没有这些市编码：{sorted(missing)}")
            print(f"{prov['名称']}：{len(cities)} 个市，模块 {[MODULES[m] for m in modules]}")

            for module in modules:
                for city in cities:
                    if (module, city["编码"]) in done:
                        continue
                    records, total, mismatch = crawl_city(client, module, city, args.page_size,
                                                          args.interval, args.max_pages,
                                                          state["截断警告"])
                    state["记录"].extend(records)
                    state["已完成"].append([module, city["编码"]])
                    state["城市统计"][f"{module}|{city['编码']}"] = {
                        "市": city["名称"], "模块": MODULES[module],
                        "抓到": len(records), "城市字段不符": mismatch}
                    save(args.output, state)
                    note = f"，其中 {mismatch} 条城市字段不符" if mismatch else ""
                    print(f"[完成] {MODULES[module]}/{city['名称']} {len(records)} 条{note}")
    except Stop as e:
        save(args.output, state)
        sys.exit(f"\n中断：{e}\n已保存 {len(state['记录'])} 条到 {args.output}；重跑同一条命令可续上。")
    except KeyboardInterrupt:
        save(args.output, state)
        sys.exit(f"\n已中止，保存了 {len(state['记录'])} 条到 {args.output}；重跑同一条命令可续上。")

    save(args.output, state)
    print(f"\n全部完成：{len(state['记录'])} 条 → {args.output}")
    for w in state["截断警告"]:
        print(f"警告：{w}")


if __name__ == "__main__":
    main()
