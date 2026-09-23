"""把一条受理公示解析成结构化字段：是不是辐射、谁批的、审批地在哪、项目地在哪。

为什么要这个模块：南京市局那个栏目 300 条实测下来，标题括号里有三种东西混着——
166 条是发文机关（玄武/江宁…）、76 条是业务类别（辐射）、34 条是项目属地（市局代发时标项目在哪个区）。
只取括号会造出一个叫「辐射」的区县；而「审批地」和「项目地」在 34/200 的公告里根本不是一回事
（高淳 29 条全部由市局代发，项目在高淳）。所以这两件事必须分开解析，且顺序是先判辐射再判地区。

地名一律走 clean_jiangsu_eia.Places（从地区树生成），不在这里写死任何地名。
"""
from __future__ import annotations

import re

from clean_jiangsu_eia import core, normalize

# 辐射/核技术类的判定词。输变电、千伏是电磁辐射类项目最常见的写法（南京 76 条里大量是这类）
RADIATION = re.compile(r"辐射|核技术|核与辐射|输变电|送出工程|[0-9]+千伏|[0-9]+kV|探伤|放射|同位素|射线|电磁")
# 标题结尾的括号
BRACKET = re.compile(r"[（(]([^（）()]{1,12})[）)]\s*$")
# 「文章来源：XXX  发布时间：…」里把来源摘出来
SOURCE = re.compile(r"(?:文章来源|来源)[：:]\s*([^\s　<]+)")
# 表示「市本级」的括号写法，不是地名
LEVEL_WORDS = {"市级", "市本级", "本级", "市直"}


def parse_source(text: str) -> str:
    """从详情页正文里取发文机关。取不到返回空串。"""
    m = SOURCE.search(text or "")
    if not m:
        return ""
    return re.split(r"发布时间|发布日期|&nbsp;", m.group(1))[0].strip()


def bracket_of(title: str) -> str:
    m = BRACKET.search(title or "")
    return m.group(1).strip() if m else ""


def is_radiation(title: str, bracket: str = "") -> bool:
    """先于地区判断。括号里是「辐射」时它是业务类别，不是地名。"""
    return bool(RADIATION.search(title or "") or RADIATION.search(bracket or ""))


def district_of_address(addr: str, city: dict, places) -> str:
    """从建设地点原文里判区县。地址常带「江苏省南京市…」前缀，先剥省名再交给 Places。"""
    if not addr:
        return ""
    s = re.sub(r"^" + re.escape(places.prov_core) + r"省?", "", addr.strip())
    d = places.district_in(s, city)
    if d:
        return d
    z = places.zone_in(s)  # 江北新区这类没有区县码的片区
    return z or ""


def classify(title: str, source: str, locations: list[str], city: dict, places,
             channel_org: str = "") -> dict:
    """一条公示 → 结构化结果。

    审批地（approval_district）来自发文机关，项目地（project_districts）来自建设地点，
    两者可以不同，这正是需要分开存的原因。

    channel_org 是栏目归属机构，用于补全不含地名的来源写法——常州市局页面的「来源：生态环境局」
    就没有市名，单看页面无法判断是哪个市的哪个局，只能靠栏目归属补。这是页面上拿不到、
    必须由爬虫表提供的唯一一条机构信息。
    """
    bracket = bracket_of(title)
    radiation = is_radiation(title, bracket)

    src = normalize(source or "")
    approval_area = places.area_of(src, city["编码"], "") if src else None
    if not approval_area and channel_org:
        src = normalize(channel_org)
        approval_area = places.area_of(src, city["编码"], "")
    approval_district = approval_area[1] if approval_area else ""

    projects = [district_of_address(a, city, places) for a in (locations or [])]

    # 括号语义：先辐射，再看它是不是本市区县，再比对发文机关
    if radiation:
        meaning = "业务类别"
    elif core(bracket) in LEVEL_WORDS or bracket in LEVEL_WORDS:
        meaning = "层级"
    elif not bracket:
        meaning = "无"
    else:
        bd = places.district_in(bracket, city) or ""
        if not bd:
            meaning = "非地名"
        elif approval_district == bd:
            meaning = "发文机关"
        else:
            meaning = "项目属地"

    return {
        "bracket": bracket,
        "bracket_meaning": meaning,
        "radiation": radiation,
        "approval_org": src,
        "approval_district": approval_district,
        "project_districts": projects,
        "n_projects": len(locations or []),
    }
