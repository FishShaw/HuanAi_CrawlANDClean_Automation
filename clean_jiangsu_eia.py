#!/usr/bin/env python3
"""清洗：把 crawl_jiangsu_eia.py 抓到的全省记录归并成单位清单。

    python clean_jiangsu_eia.py                 # 受理/审批/发布单位列（processDepartment）
    python clean_jiangsu_eia.py --field export  # 受理/监督单位字段，拆组合值并附对照列

与南京那版的区别：地名白名单不再写死，全部从抓取时存下来的地区树生成，换省换市都不用改代码。

清洗规则（与南京版一致）：
    1. 标错地区：名称开头的省市不属于本省，删除。
    2. 不规范：代称（「我局(江宁)」）、截断残句、错字、结尾不是机构后缀的，删除。
    3. 写法不同：同一地区 + 同一职能的多种写法归并成一条，保留公告日期最新的规范写法。
    4. 非环保部门（水务局、交通运输局等）保留。

归并的地区粒度是「市 + 区县/开发区/市本级/省级/国家」。用哪个市不看名称猜，
优先认名称里写明的本省城市，认不出才用抓取时查询的那个市，这样跨市同名区县
（南京鼓楼区 / 徐州鼓楼区）不会被并到一起。

按地区 + 职能归并只依据名称写法和公告日期，不代表机构法定更名。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
IN = HERE / "江苏省环评单位_原始记录.json"
OUT_PROC = HERE / "江苏省环评单位.csv"
OUT_EXPORT = HERE / "江苏省环评单位_两字段对照.csv"

MODULES = {1: "环评受理", 2: "环评拟审批", 3: "环评审批", 5: "环评验收"}
SPLIT = re.compile(r"[,，、;；\r\n]+")  # 组合值分隔符
# 生态环境主管部门的各种写法
ECO = re.compile(r"(生态环境(局|厅|部|和水务局)|环境保护(局|部|厅)|环保(局|厅|总局))")
# 行政审批 / 政务服务口径
ADMIN = re.compile(r"(行政审批局|政务服务管理办公室|政务服务中心|行政服务中心|数据局"
                   r"|管理委员会|管委会|市民中心)")
# 机构名合法结尾。「会」覆盖管委会/委员会；「部门」是「昆山市生态环境部门」这类笼统写法；
# 开发区一类的名称本身就被当作发布主体用（「常熟高新技术产业开发区」），也算合法。
SUFFIX = re.compile(r"(局|厅|部|委|会|中心|办公室|政府|院|科|处|所|队|组|站|署|部门|生态环境)$"
                    r"|(?<!公)司$|(开发区|新区|园区|高新区|度假区|保税区|港区|示范区|工业园)$|\)$|）$")
# 建设单位被误填进单位列，公司不是审批机关
COMPANY = re.compile(r"(公司|厂|集团|中心有限|事务所)$")
# 内设科室 / 办事窗口，归并到所属单位
SECTION = re.compile(r"(环评科|固废科|审批科|行政审批服务科|环评管理科|环评窗口|窗口"
                     r"|环境影响评价管理科|环境影响评价科|行政审批科\(监督管理科\))$")
# 明显的错字 / 残句
TYPO = re.compile(r"(行审|环许|管审|政服环)")
# 开发区 / 新区 / 园区 / 度假区：地区树里没有，按名称识别
ZONE = re.compile(r"([一-龥]{0,10}?)(经济技术开发区|高新技术产业开发区|经济开发区|工业园区"
                  r"|综合保税区|合作示范区|示范区|经开区|高新区|开发区|度假区|保税区|新区"
                  r"|工业园|科技园|产业园|园区|港区)")
ZONE_KIND = {"经济技术开发区": "开发区", "经济开发区": "开发区", "经开区": "开发区", "开发区": "开发区",
             "高新技术产业开发区": "高新区", "高新区": "高新区", "新区": "新区",
             "工业园区": "园区", "园区": "园区", "工业园": "园区", "科技园": "园区", "产业园": "园区",
             "度假区": "度假区", "综合保税区": "保税区", "保税区": "保税区", "港区": "港区",
             "合作示范区": "示范区", "示范区": "示范区"}
# 地区前缀：形如「开封市」「安徽省」。只认纯中文，避免把「我局(市级)」当成地区。
PLACE_PREFIX = re.compile(r"^([一-龥]{2,6}?)(省|市|自治区|特别行政区)")
# 必须从头匹配：否则「苏州太湖国家旅游度假区」里的「国家」会被当成国家级机构。
# 「生态环境部门」是地方部门的泛称，不能当成生态环境部，所以排除「部门」。
NATION = re.compile(r"^(中华人民共和国|国家环保|国家生态环境)|^(环境保护部|生态环境部)(?!门)")
# 公告原文里混进来的日期前缀，如「2023年4月21日苏州市生态环境局」
DATE_PREFIX = re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日")
# 规范的机构名结尾：归并时优先拿这类做代表名，避免「昆山市生态环境部门」
# 这种笼统写法压过正规的「苏州市昆山生态环境局」
PROPER = re.compile(r"(局|厅|部|委员会|管委会|中心|办公室|政府|院|司|署)$|\)$|）$")
# 镇 / 街道级机构：地区树只到区县，镇一级只能从名称里认
# 「住房和城乡建设局」里的「城乡」不是乡级机构，排掉
TOWN = re.compile(r"[一-龥]{2,6}?(镇|街道|(?<!城)乡)")
# 只有纯中文（可带括号）的名称才尝试截断回收，带字母数字或分隔符的是乱码不是截断
TRUNCATABLE = re.compile(r"^[一-龥()（）]+$")
# 派驻政务服务中心 / 市民中心的窗口式写法，名称里往往没有地名（安徽大量出现：
# 「行政服务中心环保局」「政务服务中心生态环境分局」「驻合肥市政务服务管理局生态环境窗口」）。
# 这类才允许拿记录的区县字段当归属证据——不含这些线索的裸名（「生态环境分局」）仍然按认不出地区删除。
WINDOW = re.compile(r"(政务服务|行政服务|市民服务|服务中心|大厦)")
# 公告落款里的派驻前缀，判断地区前缀时要先去掉，否则「驻合肥市…」会被当成「驻合肥」这个外地地名
STATION = re.compile(r"^驻")
# 地区树里没有、但确实是一级行政管理区的名字，按省编码登记（需求方确认后再加）。
# 安徽：毛集实验区 = 淮南市毛集社会发展综合实验区，区县字段还常被标成凤台/大通，只能按名称认。
EXTRA_ZONES = {"340000": ("毛集实验区",)}


def core(place: str) -> str:
    """去掉行政级别后缀，「溧水区」→「溧水」，「南京市」→「南京」。"""
    return re.sub(r"(省|市|区|县|自治区|自治县|自治州)$", "", place or "")


class Places:
    """从地区树生成的地名索引，全部按省来，不含任何写死的地名。"""

    def __init__(self, tree: dict):
        self.code = tree["编码"]
        self.extra_zones = EXTRA_ZONES.get(self.code, ())
        self.prov = tree["名称"]
        self.prov_core = core(self.prov)
        self.cities = tree["市"]
        self.by_code = {c["编码"]: c for c in self.cities}
        # 允许出现的地名：本省、本省各市、本省各区县
        self.allowed = {self.prov_core, core(tree.get("简称") or "")}
        for city in self.cities:
            self.allowed |= {core(city["名称"]), core(city.get("简称") or "")}
            for d in city["区县"]:
                self.allowed |= {core(d["名称"]), core(d.get("简称") or "")}
        self.allowed.discard("")
        # 去掉地名后剩下的部分用作「其他部门」的职能键
        words = sorted(self.allowed, key=len, reverse=True)
        self.strip_re = re.compile("|".join(re.escape(w) + "[省市区县]?" for w in words))
        # 开发区归一时只剥省名和市名：区县名必须留着，否则「江宁开发区」（区级）
        # 和「南京经济技术开发区」（市级）会变成同一个键被错并。
        self.city_words = {self.prov_core, core(tree.get("简称") or "")}
        for city in self.cities:
            self.city_words |= {core(city["名称"]), core(city.get("简称") or "")}
        self.city_words.discard("")

    def city_in(self, name: str) -> dict | None:
        """名称里写明的本省城市。"""
        for city in self.cities:
            if core(city["名称"]) and core(city["名称"]) in name:
                return city
        return None

    def strip_city_prefix(self, name: str, city: dict) -> str:
        """去掉名称开头的市名，如「淮安市生态环境局」→「生态环境局」。

        必须先去：淮安市下辖淮安区，core(「淮安区」)=「淮安」，而每个「淮安市…」的名字
        都含「淮安」，不去掉市名前缀就会把全市单位都当成淮安区，collapse 成一条。
        """
        for w in sorted({core(city["名称"]), core(city.get("简称") or "")}, key=len, reverse=True):
            if w and name.startswith(w):
                return re.sub(r"^市", "", name[len(w):])
        return name

    def district_in(self, name: str, city: dict) -> str | None:
        """名称里写明的该市区县，返回去掉「区/县」的核心名。"""
        rest = self.strip_city_prefix(name, city)
        for d in sorted(city["区县"], key=lambda d: -len(d["名称"])):
            short = core(d["名称"])
            if not short:
                continue
            # 单字区县名（马鞍山和县、徐州丰县）必须连着「县/区/市」才算，否则
            # 「住房和城乡建设局」里的「和」会被当成和县
            if len(short) == 1:
                if any(short + suf in rest for suf in ("县", "区", "市")):
                    return short
            elif short in rest:
                return short
        return None

    def zone_in(self, name: str, district: str = "") -> str | None:
        """开发区 / 新区 / 园区 / 度假区，归一成「江宁开发区」「吴中度假区」这种键。

        以名称里的开发区名为准；只有名称里没写（「港区行政审批局」「度假区行政审批局」）
        才退而用记录的区县字段定位。不能反过来一律用区县——江北新区横跨浦口/六合/栖霞，
        按区县分会把同一个管委会拆成好几条。
        """
        for z in self.extra_zones:
            if z in name:
                return z
        m = ZONE.search(name)
        if not m:
            return None
        kind = ZONE_KIND[m.group(2)]
        raw_head, head = m.group(1), m.group(1)
        # 开发区名是全名里搜出来的，头部常粘着机构词：「淮安市生态环境局开发区分局」
        # 的头是「生态环境局」，不是地名。从最后一个机构字之后截断。
        head = re.split(r"[局厅委办部科处所站心]", head)[-1]
        # 切完只剩一个字的是机构词的尾巴（「…管委会高新区」剩「会」），不是地名；
        # 这种情况按区县字段定位，别当成市本级——市本级只留给「南京经济技术开发区」这类整头是市名的
        split_leftover = len(head) == 1 and head != raw_head
        if split_leftover:
            head = ""
        for word in sorted(self.city_words, key=len, reverse=True):
            head = head.replace(word, "")
        # 要反复剥：「安徽省宣城市广德县…」去掉省名市名后剩「省市广德县」，只剥一次会留下「市广德」
        head = re.sub(r"^(省|市)+", "", head)
        head = re.sub(r"[市区县]$", "", head)  # 「张家港市开发区」和「张家港开发区」是一个
        # 单字头是名称被截断的残留（「州经济开发区」少了「常」），不可信，改用区县
        if len(head) > 1:
            return head + kind
        # 名称里写了省市名（「南京经济技术开发区」），那就是市级的，不要再按区县细分
        if raw_head and not head and not split_leftover:
            return "本级" + kind
        return (core(district) or head or "本级") + kind

    def is_misplaced(self, name: str) -> bool:
        """标错地区：开头的省市不属于本省。"""
        m = PLACE_PREFIX.match(STATION.sub("", name))
        return bool(m) and core(m.group(1)) not in self.allowed

    def unique_district(self, name: str) -> tuple[str, str] | None:
        """全省范围找区县名，只有唯一命中才认——「鼓楼」南京徐州都有，宁可不认。"""
        hits = {(c["名称"], d) for c in self.cities if (d := self.district_in(name, c))}
        return next(iter(hits)) if len(hits) == 1 else None

    def area_of(self, name: str, queried_city_code: str, district: str = "") -> tuple[str, str] | None:
        """返回 (市, 区域)；认不出地区返回 None。district 是该写法记录里最常见的区县。"""
        if NATION.search(name):
            return ("—", "国家")
        named_city = self.city_in(name)
        city = named_city or self.by_code.get(queried_city_code)
        if not city:
            return None
        zone = self.zone_in(name, district)
        if zone:
            return (city["名称"], zone)
        named_district = self.district_in(name, city)  # 别覆盖 district：下面的窗口规则还要用它
        if named_district:
            return (city["名称"], named_district)
        # 名称里的区县不属于查询市（被站点错标到别的市名下），全省唯一命中才认
        elsewhere = self.unique_district(name)
        if elsewhere:
            return elsewhere
        # 名称里写明了某个市就归那个市，别被同时出现的省名带到省级去
        # （「江苏省南通市数据局」是南通的，不是省厅）
        if named_city:
            return (named_city["名称"], "市本级")
        if self.prov_core in name:
            return ("—", "省级")
        if core(city["名称"]) in name or core(city.get("简称") or "") in name:
            return (city["名称"], "市本级")
        # 镇 / 街道一级：地区树里没有，按名称认，挂在所属市下自成一条
        town = TOWN.search(name)
        if town:
            return (city["名称"], town.group(0))
        # 派驻窗口：名称里没有地名，但区县字段是证据，且该区县确属本市
        if WINDOW.search(name) and district:
            d = core(district)
            if any(core(x["名称"]) == d or core(x.get("简称") or "") == d for x in city["区县"]):
                return (city["名称"], d)
        return None

    def function_of(self, name: str) -> str:
        """职能键：生态环境口、行政审批口，或者具体的委办局名称。"""
        if ECO.search(name):
            return "生态环境"
        # 「蚌埠市燕山路管委会高新区生态环境分局」是环保派出分局，不能因为名字里有「管委会」
        # 就并进该开发区的审批口
        if ROLE_ECO.search(name) and ADMIN.search(name):
            return "生态环境"
        if ADMIN.search(name):
            return "行政审批"
        rest = self.strip_re.sub("", name)
        # 名称本身就是一个开发区/园区（「常熟高新技术产业开发区」「张家港市经济技术开发区(杨舍镇)」），
        # 发布主体即其管委会，归到行政审批口，好和同一个开发区的管委会/政务服务中心并到一起。
        if not ZONE.sub("", re.sub(r"[（(].*?[）)]", "", rest)).strip():
            return "行政审批"
        return rest or name

    def is_irregular(self, name: str, queried_city_code: str, district: str = "") -> bool:
        if not name or name in {"（空）", "(空)", "-", "无"}:
            return True
        if "我局" in name:  # 公告原文里的自称
            return True
        if re.search(r"[;；]", name):  # 分隔符残留
            return True
        if not SUFFIX.search(name) or COMPANY.search(name):  # 不是机构名，或者是企业
            return True
        if TYPO.search(name):  # 错字
            return True
        return self.area_of(name, queried_city_code, district) is None  # 认不出地区

    def drop_reason(self, name: str, queried_city_code: str, district: str = "") -> str | None:
        if self.is_misplaced(name):
            return "标错地区"
        if self.is_irregular(name, queried_city_code, district):
            return "不规范"
        return None


def normalize(raw: str) -> str:
    return SECTION.sub("", DATE_PREFIX.sub("", (raw or "").strip()).strip())


def canonical_zones(counts: dict[tuple[str, str], int]) -> dict[tuple[str, str], tuple[str, str]]:
    """同市同类型的开发区之间，名字互相包含时并成一个，条数多的一方作准。

    方向按条数而不是按长短：「州国家高新区」是「常州国家高新区」被截掉一个字的残留，
    只有 1 条，不能让它把正主吸并过去。
    """
    kinds = sorted(set(ZONE_KIND.values()), key=len, reverse=True)
    parsed: dict[tuple[str, str], tuple[str, str]] = {}  # area -> (kind, head)
    for area in counts:
        for kind in kinds:
            if area[1].endswith(kind):
                parsed[area] = (kind, area[1][: -len(kind)])
                break
    out = {}
    for area, (kind, head) in parsed.items():
        if not head or head == "本级":
            continue
        others = [a for a, (k, h) in parsed.items()
                  if a != area and a[0] == area[0] and k == kind and h and h != head
                  and (h in head or head in h)
                  and counts[a] > counts[area]]
        if len(others) == 1:
            out[area] = others[0]
    return out


def clean(records: list[tuple[str, str, str, str]], places: Places) -> list[dict]:
    """records 是 (单位原文, 公告日期, 查询市编码, 区县)。"""
    # 1) 先按写法聚合，顺便统计每个写法最常出现的区县，后面定位开发区要用
    agg: dict[tuple[str, str], dict] = {}
    for raw, day, city_code, country in records:
        name = normalize(raw)
        if not name:
            continue
        s = agg.setdefault((name, city_code),
                           {"count": 0, "date": "", "countries": Counter()})
        s["count"] += 1
        s["date"] = max(s["date"], day or "")
        if country:
            s["countries"][country] += 1

    def district_of(s: dict) -> str:
        return s["countries"].most_common(1)[0][0] if s["countries"] else ""

    # 2) 分成可用的和该删的
    stats, dropped = {}, {}
    for key, s in agg.items():
        name, city_code = key
        district = district_of(s)
        s["district"] = district
        if places.drop_reason(name, city_code, district):
            dropped[key] = s
        else:
            s["area"] = places.area_of(name, city_code, district)
            stats[key] = s

    # 同一个市、同一类型的开发区，若一个的名字是另一个的一部分且只对得上一个，
    # 视为同一个区：「国家旅游度假区」并入「太湖国家旅游度假区」。
    area_counts: Counter[tuple[str, str]] = Counter()
    for s in stats.values():
        area_counts[s["area"]] += s["count"]
    canon = canonical_zones(area_counts)
    for s in stats.values():
        s["area"] = canon.get(s["area"], s["area"])

    groups: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
    for key, s in stats.items():
        groups[(*s["area"], places.function_of(key[0]))].append(key)

    # 3) 截断名回收：同一市同一区县内，若它只能对应唯一一组完整写法，就并进去。
    #    「苏州市生态环境」→「苏州市生态环境局(...)」、「安环局」→「张家港市安环局」。
    #    对应不唯一（「会行政审批局」）或没有区县佐证的，仍然删掉，不猜。
    name_group = {key: g for g, keys in groups.items() for key in keys}
    for key, s in list(dropped.items()):
        name, city_code = key
        district = s["district"]
        # 没有区县佐证就没法判断归属，宁可删掉
        if len(name) < 3 or not district or not TRUNCATABLE.search(name):
            continue
        # 只跟本来就成组的写法比：本轮刚回收进 stats 的名字还没有分组，拿它当参照会 KeyError，
        # 也会让回收结果取决于遍历顺序
        hits = {g for k, g in name_group.items()
                if k[1] == city_code and stats[k]["district"] == district
                and k[0] != name and (k[0].startswith(name) or k[0].endswith(name))}
        if len(hits) == 1:
            groups[hits.pop()].append(key)
            stats[key] = s
            del dropped[key]

    out = []
    for (city, area, _func), keys in groups.items():
        # 先看是不是规范写法，再比公告日期、条数、长度：同组里有正规机构名时不让笼统写法当代表
        ranked = sorted(keys, key=lambda k: (bool(PROPER.search(k[0])), stats[k]["date"],
                                             stats[k]["count"], len(k[0])), reverse=True)
        out.append({
            "城市": city,
            "地区": area,
            "单位": ranked[0][0],
            "职能": role_of(ranked[0][0]),
            "最新公告日期": max(stats[k]["date"] for k in keys),
            "记录数": sum(stats[k]["count"] for k in keys),
            # 机构名里本身可能有「、」，这里用斜杠分隔
            # 同一个写法可能来自不同市的查询，去重，也别把代表名自己列进来
            "合并的其他写法": " / ".join(dict.fromkeys(
                k[0] for k in ranked[1:] if k[0] != ranked[0][0])),
        })
    out.sort(key=lambda r: (r["城市"], -r["记录数"], r["单位"]))
    return out, dropped


# 环评审批机构口径（2026-09-17 定）：只认生态环境部门、行政审批/数据/政务服务部门、开发区管委会。
# 交通、水务、司法、人民政府等只是公告发布渠道，不是审批机关。
# 故意比 ECO/ADMIN 宽：那两个是归并分组用的，放宽会改变合并结果；这里只打标签。
# 「生态局」「生态环局」「生态境局」是源数据漏字，「安环局」是张家港的安全环保合署机构。
ROLE_ECO = re.compile(r"生态环境|环境保护|环保|生态局|生态环局|生态境局|安环局|环境监察")
# 「改革创新局」是安徽自贸试验区片区、部分高新区承接审批职能的机构
ROLE_ADMIN = re.compile(r"行政审批|审批局|审批大厅|数据局|数据管理局|政务服务|政务中心|行政服务"
                        r"|一站式服务|改革创新局|数据资源")
ROLE_ZONE = re.compile(r"管理委员会|管委会")


def role_of(name: str) -> str:
    """职能标签：生态环境 / 行政审批 / 开发区管委会 / 其他部门。"""
    # 「经开区分局」「工业园区分局」是公告里对生态环境局派出分局的简写；
    # 前面带了别的局名的（「泰州市水利局医药高新区分局」）不算
    if ROLE_ECO.search(name) or re.fullmatch(r"[^局]*(开发区|经开区|园区|高新区|新区)分局", name):
        return "生态环境"
    if ROLE_ADMIN.search(name):
        return "行政审批"
    # 开发区名本身当发布主体用（「淮安工业园区」），即其管委会
    if ROLE_ZONE.search(name) or ZONE.fullmatch(name) or re.fullmatch(r"[一-龥]{0,8}" + ZONE.pattern, name):
        return "开发区管委会"
    return "其他部门"


def names_of(row: dict) -> set[str]:
    return {row["单位"], *(n for n in row["合并的其他写法"].split(" / ") if n)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--field", choices=["proc", "export"], default="proc",
                   help="proc=列表页单位列（默认）；export=受理/监督单位字段，拆组合值并附对照列")
    p.add_argument("--input", type=Path, default=IN)
    p.add_argument("--output", type=Path, help="输出 CSV，默认按 --field 取名")
    p.add_argument("--module", type=int, help=f"只清洗某个模块，默认全部。可选 {sorted(MODULES)}")
    args = p.parse_args()

    if not args.input.exists():
        sys.exit(f"没有 {args.input.name}，先运行 crawl_jiangsu_eia.py")
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    places = Places(raw["地区树"])
    rows_in = [r for r in raw["记录"] if args.module is None or r[0] == args.module]
    if not rows_in:
        sys.exit(f"原始记录里没有模块 {args.module} 的数据")

    # 每条记录是 [模块, 查询市编码, processDepartment, 组合单位字段, 公告日期, 区县, 市]
    proc_recs = [(r[2], r[4], r[1], r[5]) for r in rows_in]
    if args.field == "proc":
        recs, label, out_path = proc_recs, "单位", args.output or OUT_PROC
    else:
        recs = [(s.strip(), r[4], r[1], r[5]) for r in rows_in
                for s in SPLIT.split(r[3] or "") if s.strip()]
        label, out_path = "受理/监督单位", args.output or OUT_EXPORT

    rows, dropped = clean(recs, places)
    columns = ["城市", "地区", label, "职能", "最新公告日期", "记录数", "合并的其他写法"]
    if args.field == "export":
        proc_names = {n for row in clean(proc_recs, places)[0] for n in names_of(row)}
        for row in rows:
            row["单位列是否出现"] = "是" if names_of(row) & proc_names else "否"
        columns.insert(5, "单位列是否出现")

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows({**row, label: row["单位"]} for row in rows)

    kept = sum(r["记录数"] for r in rows)
    cities = len({r["城市"] for r in rows})
    print(f"{places.prov}：原始 {len(recs)} 条 → 保留 {kept} 条、{len(rows)} 个单位、{cities} 个地区 → {out_path}")
    for city in sorted({r["城市"] for r in rows}):
        n = sum(1 for r in rows if r["城市"] == city)
        print(f"  {city}: {n} 个")

    empty = sum(1 for r in recs if not normalize(r[0]))
    by_reason: dict[str, list] = defaultdict(list)
    for (name, city_code), s in dropped.items():
        reason = places.drop_reason(name, city_code, s["district"]) or "不规范"
        by_reason[reason].append((name, s["count"]))
    if empty:
        print(f"\n删除（单位为空）{empty} 条")
    for reason, items in by_reason.items():
        total = sum(c for _, c in items)
        print(f"\n删除（{reason}）{total} 条、{len(items)} 种写法，条数前 20：")
        for n, c in sorted(items, key=lambda kv: -kv[1])[:20]:
            print(f"  {n}  ({c}条)")


if __name__ == "__main__":
    main()
