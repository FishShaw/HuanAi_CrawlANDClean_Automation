"""把受理单位的身份、行政区划、开发区、上下级关系、受理入口、数据佐证合成一张主数据表。

一机构一行。解决三件事：
1. 「地区」列混了区县名/开发区归一键/层级占位/镇名四种语义，且「本级开发区」在 12 个市重复，
   不能作标识符 —— 这里拆成 admin_*（行政区划码）和 zone_*（开发区维表外键）两组。
2. 行政区划 GB 码原来只存在于运行时抓的 JSON 里，仓库无副本 —— 改读 refs/area_tree_<省>.json 快照。
3. 开发区管委会有合署/上级派出/下属部门三种身份，原来无处可存 —— 记进 admin_relation，
   依据写进 relation_evidence，未经需求方确认的不落 zone_admin_code。

unit_uid 一次分配永不变：重跑时按 match_key 对齐已有行，认不出的新分配，消失的标「已停用」不删行。

    python build_authority_master.py --province 320000
    python build_authority_master.py --approvers          # 只要交付口径的审批机构
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
from pathlib import Path

from build_entries_xlsx import load_entries
from clean_jiangsu_eia import SPLIT, Places, normalize
from clean_jiangsu_eia import core as place_core

HERE = Path(__file__).resolve().parent
REFS = HERE / "refs"

VARIANT_SEP = " / "  # 机构名本身可能含「、」，所以清洗用的是前后带空格的斜杠
LEVEL_PLACEHOLDER = {"国家", "省级", "市本级"}

COLUMNS = [
    # 身份与命名
    "unit_uid", "org_id", "entity_id",
    "name_canonical", "name_from_source", "name_variants", "badges", "name_status",
    # 行政区划归属
    "admin_level", "prov_code", "city_code", "admin_code", "admin_name", "admin_short",
    # 开发区归属
    "zone_id", "zone_name", "zone_short", "zone_level", "zone_admin_code",
    # 上下级与归属关系
    "admin_relation", "relation_evidence", "relation_confirmed",
    "parent_admin_code", "group_id", "co_occurrence_top",
    # 职能
    "role", "function_key",
    # 受理入口（entry_id 是权威，后面几列由入口表派生，勿手工改）
    "entry_id", "entry_category", "entry_url", "entry_path", "filter_rule",
    "verify_status", "verify_date",
    # 数据佐证
    "record_count", "raw_matched", "first_notice_date", "last_notice_date", "county_field_dist",
]


UID_PREFIX = {"320000": "JS", "330000": "ZJ", "340000": "AH"}


def load_tree(province: str) -> dict:
    f = REFS / f"area_tree_{province}.json"
    if not f.exists():
        raise SystemExit(f"缺少行政区划快照 {f}，先从 raw/ 导出（见 docs/江苏试点记录.md）")
    return json.loads(f.read_text(encoding="utf-8"))["地区树"]


def load_zones(province: str) -> dict[tuple[str, str], dict]:
    """开发区维表，按 (城市, 旧地区标签) 索引。"""
    f = REFS / f"zones_{province}.csv"
    if not f.exists():
        return {}
    with open(f, encoding="utf-8-sig") as fh:
        return {(r["city"], r["source_label"]): r for r in csv.DictReader(fh)}


def build_co_occurrence(raw_dir: Path, province: str) -> None:
    """从原始记录第 4 位 acceptanceMonitorDepartmentForExport（受理/监督单位组合值）
    拆出「发布单位 → 共同受理/监督单位」关系。江苏 88148 条里 22427 条含 2 个及以上单位。

    分隔符复用清洗的 SPLIT，不另写一套：按「、」硬拆会切断括号里带顿号的机构名
    （江北新区管委会那种），main 分支旧工作流就踩过。
    """
    if not raw_dir.exists():
        return
    pair: collections.Counter = collections.Counter()
    for f in sorted(raw_dir.glob(f"{province[:2]}*.json")):
        for rec in json.loads(f.read_text(encoding="utf-8"))["记录"]:
            proc = (rec[2] or "").strip()
            parts = [p.strip() for p in SPLIT.split(rec[3] or "") if p.strip()]
            if not proc or len(parts) < 2:
                continue
            for other in parts:
                if other != proc:
                    pair[(proc, other)] += 1
    if not pair:
        return
    f = REFS / f"co_occurrence_{province}.csv"
    with open(f, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["发布单位", "共同受理监督单位", "共现次数"])
        for (a, b), n in sorted(pair.items(), key=lambda x: (-x[1], x[0])):
            w.writerow([a, b, n])
    print(f"  共现关系 {f.name}：{len(pair)} 对")


def load_co_occurrence(province: str) -> dict[str, list[tuple[str, int]]]:
    """发布单位 → [(共同受理/监督单位, 次数)]，次数降序。

    来源是 acceptanceMonitorDepartmentForExport（组合值），口径与 processDepartment 不同，
    这里只当关系证据用，不参与单位清单。
    """
    f = REFS / f"co_occurrence_{province}.csv"
    out: dict[str, list[tuple[str, int]]] = collections.defaultdict(list)
    if not f.exists():
        return out
    with open(f, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            out[r["发布单位"]].append((r["共同受理监督单位"], int(r["共现次数"])))
    for v in out.values():
        v.sort(key=lambda x: -x[1])
    return out


def aggregate_raw(raw_dir: Path, province: str, alias: dict[tuple[str, str], tuple]) -> dict:
    """从原始记录聚合条数、日期区间、区县字段分布。

    别名映射必须按 (城市, 写法) 建键：「高新区行政审批局」在南通、泰州、宿迁是三个不同单位，
    只按写法建键会跨市互相覆盖。
    """
    agg: dict[tuple, dict] = collections.defaultdict(
        lambda: {"n": 0, "first": "", "last": "", "county": collections.Counter()})
    if not raw_dir.exists():
        return agg
    for f in sorted(raw_dir.glob(f"{province[:2]}*.json")):
        # 记录元组长度不定：直辖市式的市级抓取缺末位「市」字段（江苏实测 7074 条为 6 位）
        stem_city = f.stem.split("_", 1)[-1]
        for rec in json.loads(f.read_text(encoding="utf-8"))["记录"]:
            # 与清洗同一套归一：剥内设科室后缀和公告日期前缀，否则「…局政务服务窗口」认不回本局
            dept = normalize(rec[2] or "")
            date = rec[4] or ""
            county = rec[5] or "" if len(rec) > 5 else ""
            city = (rec[6] or "") if len(rec) > 6 else ""
            key = (alias.get((f"{city}市", dept)) or alias.get((city, dept))
                   or alias.get((stem_city, dept)))
            if not key:
                continue
            a = agg[key]
            a["n"] += 1
            if date:
                a["first"] = min(a["first"] or date, date)
                a["last"] = max(a["last"], date)
            a["county"][county or "(空)"] += 1
    return agg


def build_entry_registry(entries: dict, province: str, cities: set[str]) -> dict[str, str]:
    """一入口一行。江苏 70 个受理 URL 里 35 个被多机构共用（无锡那条被 23 个单位共用），
    内联进机构行等于把同一个 URL 存 23 份，栏目改版就要改 23 处。这里 URL 存一次，
    机构行只引 entry_id，其余入口列由本表派生。
    """
    f = REFS / f"entries_{province}.csv"
    curated = {}
    if f.exists():
        with open(f, encoding="utf-8-sig") as fh:
            curated = {r["entry_url"]: r for r in csv.DictReader(fh)}

    agg: dict[str, dict] = {}
    for (city, _name), row in entries.items():
        # entries/ 里多省共存（江苏 13 市 + 安徽 16 市），按本省城市过滤
        if city not in cities:
            continue
        url = (row.get("受理公示入口") or "").strip()
        if not url:
            continue
        e = agg.setdefault(url, {"cities": set(), "n": 0, "row": row})
        e["cities"].add(city)
        e["n"] += 1
        # 「推断」是单位层面的结论（这个单位大概也发在这里），不代表栏目本身的核验情况
        if e["row"].get("核验情况") == "推断" and row.get("核验情况") != "推断":
            e["row"] = row

    out, url2id = [], {}
    for i, (url, e) in enumerate(sorted(agg.items(), key=lambda x: -x[1]["n"]), 1):
        eid = curated.get(url, {}).get("entry_id") or f"{province[:4]}-E{i:03d}"
        url2id[url] = eid
        keep = curated.get(url, {})
        out.append({
            "entry_id": eid, "entry_url": url,
            "domain": re.sub(r"^https?://([^/]+).*$", r"\1", url),
            "cities": " / ".join(sorted(e["cities"])), "unit_count": e["n"],
            "entry_category": keep.get("entry_category", ""),
            "entry_path": keep.get("entry_path") or e["row"].get("入口（点击路径）", ""),
            "filter_rule": keep.get("filter_rule", ""),
            "title_bracket_meaning": keep.get("title_bracket_meaning", ""),
            "verify_status": keep.get("verify_status") or e["row"].get("核验情况", ""),
            "verify_date": keep.get("verify_date") or e["row"].get("核验日期", ""),
        })
    with open(f, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    print(f"  入口表 {f.name}：{len(out)} 个入口，"
          f"其中被多机构共用 {sum(1 for r in out if r['unit_count'] > 1)} 个")
    return url2id


def write_org_merge_candidates(units: list[dict], province: str) -> dict[tuple, str]:
    """同 (城市, 地区, 职能) 下的多行多半是同一机构的不同写法（漏字、改名、简称），
    但「苏州市张家港生态环境局 vs 张家港市安环局」这种得人确认，所以只给候选不自动并。
    """
    f = REFS / f"org_merge_{province}.csv"
    confirmed: dict[tuple, str] = {}
    if f.exists():
        with open(f, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if r.get("confirmed") == "是":
                    confirmed[(r["city"], r["area"], r["unit"])] = r["merge_into"]

    g: dict[tuple, list[dict]] = collections.defaultdict(list)
    for u in units:
        g[(u["城市"], u["地区"], u["职能"])].append(u)
    rows = []
    for (city, area, role), v in sorted(g.items()):
        if len(v) < 2:
            continue
        v.sort(key=lambda r: -int(r["记录数"]))
        for r in v[1:]:
            key = (city, area, r["单位"])
            rows.append({"city": city, "area": area, "role": role, "unit": r["单位"],
                         "record_count": r["记录数"], "merge_into": v[0]["单位"],
                         "merge_into_count": v[0]["记录数"],
                         "confirmed": "是" if key in confirmed else ""})
    with open(f, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  机构归并候选 {f.name}：{len(rows)} 行待确认，已确认 {len(confirmed)} 条")
    return confirmed


def split_badges(name: str) -> str:
    """规范名括号里的并列牌子，如江北新区一名三牌。"""
    m = re.search(r"[(（]([^()（）]+)[)）]\s*$", name)
    if not m:
        return ""
    inner = m.group(1)
    parts = [p.strip() for p in re.split(r"[、,，]", inner) if p.strip()]
    return VARIANT_SEP.join(parts) if parts else ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--province", default="320000", help="省编码，默认 320000（江苏）")
    ap.add_argument("--raw-dir", default=None, help="原始记录目录，默认当前检出的 raw/")
    ap.add_argument("--approvers", action="store_true", help="只输出交付口径的审批机构")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    tree = load_tree(args.province)
    prov_name, prov_code = tree["名称"], tree["编码"]
    city_code = {m["名称"]: m["编码"] for m in tree["市"]}
    dist = {(m["名称"], q["简称"]): q for m in tree["市"] for q in m.get("区县", [])}
    # 徐州「丰县/沛县」这类单字县：清洗把地区存成「丰」，地区树简称是「丰县」，两种写法都登记
    for (cname, short), q in list(dist.items()):
        core = re.sub(r"(市|区|县)$", "", short)
        dist.setdefault((cname, core), q)
    # 清洗输出的地区是 core(全称)：「景宁畲族自治县」→「景宁畲族」，和简称「景宁」对不上，
    # 不登记就被当成开发区（浙江丽水 10 条）
    for m in tree["市"]:
        for q in m.get("区县", []):
            dist.setdefault((m["名称"], place_core(q["名称"])), q)

    places = Places(tree)
    zones = load_zones(args.province)
    raw_dir = Path(args.raw_dir) if args.raw_dir else HERE / "raw"
    build_co_occurrence(raw_dir, args.province)
    co = load_co_occurrence(args.province)
    entries = load_entries()

    src = HERE / f"{prov_name}环评单位.csv"
    with open(src, encoding="utf-8-sig") as fh:
        units = list(csv.DictReader(fh))

    alias: dict[tuple[str, str], tuple] = {}
    for u in units:
        key = (u["城市"], u["地区"], u["单位"])
        alias[(u["城市"], u["单位"])] = key
        for v in (u["合并的其他写法"] or "").split(VARIANT_SEP):
            v = v.strip()
            if v:
                alias.setdefault((u["城市"], v), key)

    agg = aggregate_raw(raw_dir, args.province, alias)
    # 「—」（省级/国家）的入口在 000000_省级及国家.csv 里多省共用：只收本省单位清单里有的，
    # 否则江苏的入口表会混进安徽省厅、浙江省厅（2026-10-08 发现 3200-E055 就是安徽省厅）
    own_national = {u["单位"] for u in units if u["城市"] == "—"}
    entries_here = {k: v for k, v in entries.items() if k[0] != "—" or k[1] in own_national}
    url2id = build_entry_registry(entries_here, args.province, set(city_code) | {"—"})
    merges = write_org_merge_candidates(units, args.province)

    prev: dict[str, dict] = {}
    out_path = Path(args.out) if args.out else HERE / f"{prov_name}受理单位主数据.csv"
    if out_path.exists():
        with open(out_path, encoding="utf-8-sig") as fh:
            prev = {r["unit_uid"]: r for r in csv.DictReader(fh)}
    by_match = {r.get("function_key", "") + "|" + r.get("name_from_source", ""): r
                for r in prev.values()}
    # 编号前缀按省：浙江要是也从 JS0001 起，和江苏的编号一模一样，进了库就分不清
    pfx = UID_PREFIX.get(args.province, args.province[:2])
    next_seq = max((int(k[len(pfx):]) for k in prev if k[len(pfx):].isdigit()), default=0) + 1

    rows = []
    for u in units:
        city, area, name = u["城市"], u["地区"], u["单位"]
        zone = zones.get((city, area))
        cc = city_code.get(city, "")
        q = dist.get((city, area))

        if area == "国家":
            level, ac, an, ash = "国家", "", "", ""
        elif area == "省级":
            level, ac, an, ash = "省级", "", prov_name, tree["简称"]
        elif area == "市本级":
            level, ac, an, ash = "市本级", "", city, city.rstrip("市")
        elif q:
            level, ac, an, ash = "区县", q["编码"], q["名称"], q["简称"]
        else:
            level, ac, an, ash = "开发区/园区", "", "", ""

        za = (zone or {}).get("zone_admin_code", "")
        rel = (zone or {}).get("admin_relation", "")
        if not zone and level in ("市本级", "区县", "省级", "国家"):
            rel = "本级政府部门"
        parent = ""
        if rel == "合署":
            parent = cc
        elif rel == "下属部门":
            parent = za or ac

        stats = agg.get((city, area, name), {})
        counter = stats.get("county") or collections.Counter()
        fkey = f"{city}|{area}|{places.function_of(name)}"
        ent = entries.get((city, name), {})
        mk = fkey + "|" + name
        uid = by_match.get(mk, {}).get("unit_uid") or f"{pfx}{next_seq:04d}"
        if mk not in by_match:
            next_seq += 1
        keep = prev.get(uid, {})

        entry_url = ent.get("受理公示入口", "").strip()
        rows.append({
            "unit_uid": uid, "org_id": "", "entity_id": "",
            "name_canonical": keep.get("name_canonical") or "",
            "name_from_source": name,
            "name_variants": u["合并的其他写法"],
            "badges": split_badges(name),
            "name_status": keep.get("name_status") or "在用",
            "admin_level": level, "prov_code": prov_code, "city_code": cc,
            "admin_code": ac, "admin_name": an, "admin_short": ash,
            "zone_id": (zone or {}).get("zone_id", ""),
            "zone_name": (zone or {}).get("zone_name", "") or (area if zone else ""),
            "zone_short": (zone or {}).get("zone_short", ""),
            "zone_level": (zone or {}).get("zone_level", ""),
            "zone_admin_code": za,
            "admin_relation": rel,
            "relation_evidence": (zone or {}).get("evidence", ""),
            "relation_confirmed": (zone or {}).get("confirmed", "是" if rel == "本级政府部门" else "否"),
            "parent_admin_code": parent,
            "group_id": keep.get("group_id") or "",
            "co_occurrence_top": VARIANT_SEP.join(f"{n}:{c}" for n, c in co.get(name, [])[:3]),
            "role": u["职能"], "function_key": fkey,
            "entry_id": url2id.get(entry_url, ""),
            "entry_category": "", "entry_url": entry_url,
            "entry_path": ent.get("入口（点击路径）", ""),
            "filter_rule": "",
            "verify_status": ent.get("核验情况", ""),
            "verify_date": ent.get("核验日期", ""),
            # 记录数采信清洗产物：跨市归属（「灌南县环境保护局」→连云港）走的是 unique_district()
            # 全省唯一命中，这里不重实现那套解析，只用原始记录补清洗没有的日期区间与区县分布。
            "record_count": u["记录数"],
            "raw_matched": stats.get("n", 0),
            "first_notice_date": stats.get("first", ""),
            "last_notice_date": stats.get("last", "") or u["最新公告日期"],
            "county_field_dist": " / ".join(f"{k}:{v}" for k, v in counter.most_common(4)),
        })

    # org_id：默认各自独立，只有 refs/org_merge_*.csv 里 confirmed=是 的才并成一个机构
    city_name = {v: k for k, v in city_code.items()}
    for r in rows:
        r["org_id"] = r["unit_uid"]
    for r in rows:
        cn = city_name.get(r["city_code"], "")
        area = next((u["地区"] for u in units
                     if u["城市"] == cn and u["单位"] == r["name_from_source"]), "")
        tgt = merges.get((cn, area, r["name_from_source"]))
        if tgt:
            hit = next((x for x in rows
                        if x["city_code"] == r["city_code"] and x["name_from_source"] == tgt), None)
            if hit:
                r["org_id"] = hit["unit_uid"]

    # entity_id：合署的两块牌子（江北新区管委会↔浦口生态环境局）算同一实体，供统计去重；
    # 不合并行，unit_uid 与 record_count 都不动，所以「按牌子」和「按实体」两种口径都能出。
    district_org = {(r["city_code"], r["admin_code"], r["role"]): r["org_id"]
                    for r in rows if r["admin_code"]}
    for r in rows:
        r["entity_id"] = r["org_id"]
        if r["admin_relation"] == "合署" and r["zone_admin_code"] and r["relation_confirmed"] == "是":
            peer = district_org.get((r["city_code"], r["zone_admin_code"], r["role"]))
            if peer:
                r["entity_id"] = peer

    # 入口列全部由入口表派生，主表里是只读冗余，改入口请改 refs/entries_*.csv
    reg_f = REFS / f"entries_{args.province}.csv"
    if reg_f.exists():
        with open(reg_f, encoding="utf-8-sig") as fh:
            reg = {r["entry_id"]: r for r in csv.DictReader(fh)}
        for r in rows:
            e = reg.get(r["entry_id"])
            if e:
                r["entry_category"] = e.get("entry_category", "")
                r["filter_rule"] = e.get("filter_rule", "")
                r["entry_path"] = e.get("entry_path", "") or r["entry_path"]
                # 推断出来的单位和已核实单位共用栏目时，不能继承对方的「已核实」
                if r["verify_status"] != "推断":
                    r["verify_status"] = e.get("verify_status", "") or r["verify_status"]
                    r["verify_date"] = e.get("verify_date", "") or r["verify_date"]

    live = {r["unit_uid"] for r in rows}
    for uid, old in prev.items():
        if uid not in live:
            old["name_status"] = "已停用"
            rows.append(old)

    if args.approvers:
        rows = [r for r in rows if r["role"] != "其他部门"]

    # 后续步骤加的列（reconcile_master 的 crawl_note、add_portal_publisher 的 portal_publisher）
    # 不归本脚本生成，重跑时按 unit_uid 原样带过来——否则重跑一次就把它们冲掉
    extra = [c for c in dict.fromkeys(c for old in prev.values() for c in old) if c not in COLUMNS]
    for r in rows:
        old = prev.get(r["unit_uid"], {})
        for c in extra:
            r.setdefault(c, old.get(c, ""))

    rows.sort(key=lambda r: (r["city_code"], r["admin_code"] or "zzz", -int(r["record_count"] or 0)))
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS + extra, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    total = sum(int(r["record_count"] or 0) for r in rows)
    print(f"写入 {out_path.name}：{len(rows)} 行，记录数合计 {total}")
    print(f"  开发区/园区行 {sum(1 for r in rows if r['zone_id'])}，"
          f"挂上区县码 {sum(1 for r in rows if r['admin_code'])}，"
          f"有共现证据 {sum(1 for r in rows if r['co_occurrence_top'])}")


if __name__ == "__main__":
    main()
