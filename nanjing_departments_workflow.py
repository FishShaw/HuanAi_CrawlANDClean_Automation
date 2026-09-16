#!/usr/bin/env python3
"""南京单位入口一键工作流：完整缓存 → 字段拆分去重 → 已核验映射 → URL检测 → XLSX。
使用：.venv/bin/python nanjing_departments_workflow.py --limit 20
默认不访问 i-ESG。--refresh 才使用登录令牌；--self-test 执行回归测试。
"""
from __future__ import annotations

import argparse
import base64
import csv
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid
from collections import Counter

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

FIELD = "acceptanceMonitorDepartmentForExport"
ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "outputs/01a0a2a2-3f7d-7d53-8851-448cff380771/南京市审批单位_官网及环评入口_前10条测试.xlsx"

API = "https://www.i-esg.com/environment/ep-query/doAction"
LIST = "/ep-query/environmentalAssess/getList"
MODULES = {1: "环评受理", 3: "环评审批", 5: "环评验收"}


class CrawlError(Exception):
    """不能证明结果完整时，停止而不替换输出。"""


def encode_request(path, params):
    values = {"url": path, **params}
    # 与网站 SDK 一致：method 在最前；省略空值；不做 URL 转义。
    plain = "method=get&" + "&".join(
        f"{key}={values[key]}" for key in sorted(values) if values[key]
    )
    return {"data": base64.b64encode(AES.new(
        b"DaoGuangJianYing", AES.MODE_ECB
    ).encrypt(pad(plain.encode("utf-8"), 16))).decode("ascii")}


class APIClient:
    def __init__(self, token, proxy=None):
        self.last_request = 0.0
        self.client = httpx.Client(timeout=30, trust_env=False, proxy=proxy, headers={
            **({"Authorization": f"Bearer {token}"} if token else {}), "client": "PC",
            "system-info": "PC", "app-version": "0.0.3-beta.01",
            "app-id": f"{uuid.uuid4()};{int(time.time() * 1000)}",
            "Origin": "https://www.i-esg.com",
            "Referer": "https://www.i-esg.com/envAssessment/assessList/1",
            "User-Agent": "Mozilla/5.0",
        })

    def close(self):
        self.client.close()

    def request(self, path, **params):
        for attempt in range(4):  # 首次请求 + 最多三次重试
            time.sleep(max(0, 1.0 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.client.post(API, json=encode_request(path, params))
                if response.status_code in (401, 403):
                    raise CrawlError("登录失效或没有访问权限，请更新 I_ESG_TOKEN。")
                if response.status_code == 429:
                    raise CrawlError("网站限制请求频率，已停止；请稍后重新运行。")
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError("服务暂时异常", request=response.request,
                                                response=response)
                if response.status_code != 200:
                    raise CrawlError(f"接口 HTTP {response.status_code}，已停止。")
                try:
                    body = response.json()
                except ValueError:
                    raise CrawlError("接口未返回 JSON，可能需要登录或人工验证。") from None
                if not isinstance(body, dict) or body.get("code") != 200:
                    code = body.get("code") if isinstance(body, dict) else "未知"
                    if code == 100006:
                        raise CrawlError("今日访问次数已达网站限制（100006）。网站提示明天再来；"
                                         "请停止请求，额度恢复后重新运行，已有分页缓存可在校验后复用。")
                    if code == 100007:
                        raise CrawlError("网站提示账号权限不足（100007），请检查会员权限。")
                    raise CrawlError(f"接口业务错误 {code}，请检查登录及账号权限。")
                if "data" not in body:
                    raise CrawlError("接口缺少 data，网站结构可能变化。")
                return body["data"]
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 3:
                    raise CrawlError("网络或服务错误，三次重试后仍失败。") from None
                print(f"请求暂时失败，{2 ** attempt} 秒后重试……", flush=True)
                time.sleep(2 ** attempt)


def get_regions(api):
    data = api.request("/ep-query/screen/area")
    try:
        province, = [x for x in data if x["name"] == "江苏省"]
        city, = [x for x in province["values"] if x["name"] == "南京市"]
        districts = {str(x["code"]): x["name"] for x in city["values"]}
        if not districts or len(districts) != len(city["values"]):
            raise ValueError()
        return str(province["code"]), str(city["code"]), districts
    except (KeyError, TypeError, ValueError):
        raise CrawlError("地区数据异常，无法确认江苏省南京市及下属区县。") from None


def parse_page(data):
    if not isinstance(data, dict):
        raise CrawlError("列表 data 不是对象。")
    total, rows = data.get("total"), data.get("values")
    if type(total) is not int or total < 0 or not isinstance(rows, list):
        raise CrawlError("列表缺少有效 total/values，不能确认完整性。")
    if any(not isinstance(row, dict) for row in rows):
        raise CrawlError("列表记录格式异常。")
    if len(rows) > total or (total > 0 and not rows):
        raise CrawlError("页面条数与总数不一致或页面异常为空。")
    return total, rows


def record_id(row, id_field):
    if id_field is None:
        # 接口未提供公告级 ID。内容摘要只用于检测重复内容，不把 projectId
        # 当公告 ID；保留同一项目下不同日期、附件、处理结果等不同记录。
        canonical = json.dumps(row, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    value = row.get(id_field)
    if isinstance(value, bool) or not isinstance(value, (str, int)) or str(value).strip() == "":
        fields = json.dumps(sorted(row), ensure_ascii=False)
        raise CrawlError(f"记录缺少唯一标识 {id_field}；接口实际字段名：{fields}。"
                         "请提供这条字段列表，以确认记录标识（不包含字段值或令牌）。")
    return str(value)


def district_name(value):
    """仅规范化已核实的南京区名，不对未知名称做模糊匹配。"""
    short_names = ("玄武", "秦淮", "建邺", "鼓楼", "浦口", "栖霞", "雨花台",
                   "江宁", "六合", "溧水", "高淳")
    return value + "区" if isinstance(value, str) and value in short_names else value


def validate_row(row, district=None):
    if row.get("provinceName") not in ("江苏", "江苏省") or row.get("cityName") not in ("南京", "南京市"):
        # 只报告地区字段，不输出请求头、令牌或整条业务记录。
        fields = ("provinceName", "cityName", "countryName", "provinceCode", "cityCode", "countryCode")
        details = {key: row[key] if key in row else "（字段缺失）" for key in fields}
        raise CrawlError("地区校验失败；接口实际返回：" +
                         json.dumps(details, ensure_ascii=False) +
                         "。预期省名=江苏省、市名=南京市；请提供这条诊断信息以核实原因。")
    if district and district_name(row.get("countryName")) != district_name(district):
        details = {k: row.get(k) for k in ("provinceName", "cityName", "countryName", "areaName")}
        raise CrawlError(f"区县校验失败：预期 {district}，实际返回 "
                         + json.dumps(details, ensure_ascii=False)
                         + "。可能是名称写法不同、区县为空或筛选不符，需根据实际值核实。")
    if FIELD not in row:
        raise CrawlError("记录缺少单位字段 acceptanceMonitorDepartmentForExport。")
    if row[FIELD] is not None and not isinstance(row[FIELD], str):
        raise CrawlError("单位字段不是文本，拒绝自动转换。")


def collect_pages(fetch, id_field=None, district=None):
    """不以请求 pageSize 计算结束页；按实际记录数与 total 校验。"""
    expected = None
    result = {}
    page = 1
    first_ids = None
    received = 0
    while True:
        total, rows = parse_page(fetch(page))
        if expected is None:
            expected = total
        if total != expected:
            raise CrawlError("抓取期间记录总数变化，请重新运行。")
        if not rows and received < expected:
            raise CrawlError(f"第 {page} 页异常为空，尚未取全。")
        page_ids = []
        local_ids = set()
        duplicates = 0
        for row in rows:
            validate_row(row, district)
            key = record_id(row, id_field)
            if key in result and key not in local_ids:
                raise CrawlError(f"第 {page} 页有重复记录，可能是重复内容、分页重复或数据变化；为避免漏算已停止。")
            if key in local_ids:
                duplicates += 1
            local_ids.add(key)
            result[key] = row
            page_ids.append(key)
        if first_ids is None:
            first_ids = page_ids
        received += len(rows)
        print(f"  第 {page} 页：{received}/{expected}" +
              (f"（页内重复 {duplicates} 条，已合并）" if duplicates else ""), flush=True)
        if received > expected:
            raise CrawlError("实际记录数超过接口总数。")
        if received == expected:
            break
        page += 1
    # 末尾重读首页，检测常见的实时新增/重排；网站不提供快照，无法保证事务级一致。
    total, rows = parse_page(fetch(1))
    if total != expected or [record_id(r, id_field) for r in rows] != first_ids:
        raise CrawlError("抓取前后首页或总数变化，请重新运行。")
    return result


class CachedPages:
    """保存成功收到的分页；每次运行先在线核对首页，变化则重新取页。"""
    def __init__(self, fetch, path):
        self.fetch, self.path = fetch, path
        self.pages = {}
        self.started = False
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("version") == 1 and isinstance(value.get("pages"), dict):
                    self.pages = value["pages"]
            except (ValueError, AttributeError):
                pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             delete=False) as f:
                temporary = Path(f.name)
                json.dump({"version": 1, "pages": self.pages}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def __call__(self, page):
        if not self.started:
            if page != 1:
                raise CrawlError("分页缓存必须从首页开始验证。")
            live = self.fetch(1)
            parse_page(live)
            # 包括 total、列表内容及顺序；不能证明一致时不复用旧页。
            if self.pages.get("1") != live:
                self.pages = {}
            elif len(self.pages) > 1:
                # 再核对缓存末页，检测分页边界变化。
                last = max(map(int, self.pages))
                if self.fetch(last) != self.pages[str(last)]:
                    self.pages = {}
                else:
                    print(f"  已核对缓存首尾页，可复用 {len(self.pages)} 页", flush=True)
            self.started = True
            self.pages["1"] = live
            self.save()
            return live
        if page == 1:  # collect_pages 的末尾核对必须访问网站
            return self.fetch(1)
        if str(page) not in self.pages:
            live = self.fetch(page)
            parse_page(live)
            self.pages[str(page)] = live
            self.save()
        return self.pages[str(page)]


def collect_module(api, module, province, city, districts, page_size, id_field):
    # 地区选择器只提交选中层级；省、市并传可能扩大为整个省。
    base = dict(cityCode=city, moduleTypeCode=module, pageSize=page_size)

    def fetch(page, code=None):
        params = {**base, "pageNum": page}
        if code:
            params.pop("cityCode")
            params["countryCode"] = code
        return api.request(LIST, **params)

    # 先用少量请求检查所有区县，避免全城抓完才发现字段格式不兼容。
    print("  先检查区县筛选……", flush=True)
    for code, name in districts.items():
        _, sample = parse_page(fetch(1, code))
        for row in sample:
            validate_row(row, name)

    def collect(code=None, name=None):
        source = lambda page: fetch(page, code)
        cache_dir = getattr(api, "cache_dir", None)
        if cache_dir is not None:
            source = CachedPages(source, cache_dir / f"{module}-{code or city}-{page_size}.json")
        return collect_pages(source, id_field, name)

    records = collect()
    city_total, _ = parse_page(fetch(1))
    counts = Counter(district_name(r.get("countryName")) for r in records.values())
    audits = {}
    for code, name in districts.items():
        total, sample = parse_page(fetch(1, code))
        for row in sample:
            validate_row(row, name)
        audits[code] = total
        if total == counts[district_name(name)] and all(record_id(r, id_field) in records for r in sample):
            if total and not sample:
                raise CrawlError(f"{name} 校验页异常为空。")
            print(f"  {name}：{total} 条，城市查询数量匹配", flush=True)
            continue
        print(f"  {name}：与城市查询不一致，完整补查该区", flush=True)
        extra = collect(code, name)
        checked_total, _ = parse_page(fetch(1, code))
        if checked_total != total:
            raise CrawlError(f"{name} 核对期间总数变化。")
        city_keys = {k for k, row in records.items()
                     if district_name(row.get("countryName")) == district_name(name)}
        if not city_keys.issubset(extra):
            raise CrawlError(f"{name} 城市与区县查询记录冲突。")
        records.update(extra)
    total, _ = parse_page(fetch(1))
    if total != city_total:
        raise CrawlError("区县核对期间城市总数变化，请重试。")
    # 区县不限的城市记录始终保留，包括未标区县及网站的历史地区名称。
    return records, base, {None: city_total, **audits}


REGISTRY = json.loads(r'''
{
  "中华人民共和国生态环境部": {
    "normalized": "中华人民共和国生态环境部",
    "handling": "名称不变",
    "homepage": "https://www.mee.gov.cn/",
    "eia": "https://www.mee.gov.cn/ywgz/hjyxpj/jsxmhjyxpj/xmslqk/index.shtml",
    "approval": "https://www.mee.gov.cn/ywgz/hjyxpj/jsxmhjyxpj/ypzxmgg/index.shtml",
    "status": "已找到：官网、环评受理入口、审批决定入口",
    "note": "环境部官方栏目；受理公示页通常提供环评报告书（表）附件。",
    "source": "https://www.mee.gov.cn/ywgz/hjyxpj/jsxmhjyxpj/",
    "verified_date": "2026-09-15"
  },
  "中国(江苏)自由贸易试验区南京片区管理委员会)": {
    "normalized": "中国（江苏）自由贸易试验区南京片区管理委员会",
    "handling": "修正全角括号并移除多余右括号",
    "homepage": "https://njna.nanjing.gov.cn/zmq/",
    "eia": "https://njna.nanjing.gov.cn/njsjbxqglwyh/214/275/index_17586.html",
    "approval": "https://njna.nanjing.gov.cn/njsjbxqglwyh/214/275/index_17586.html",
    "status": "已找到：使用南京江北新区管委会官方门户",
    "note": "该官方公示栏目同时发布环评受理、拟审批和审批决定信息。",
    "source": "https://njna.nanjing.gov.cn/zmq/\nhttps://njna.nanjing.gov.cn/njsjbxqglwyh/214/275/index_17586.html",
    "verified_date": "2026-09-15"
  },
  "南京市人民政府": {
    "normalized": "南京市人民政府",
    "handling": "名称不变；环评入口按市级主管部门关联",
    "homepage": "https://www.nanjing.gov.cn/",
    "eia": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/sjxzxkhzfxx/xmhpslqk_68817/",
    "approval": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/sjxzxkhzfxx/hpypxmgg_68819/",
    "status": "已找到：官网；环评入口使用市生态环境局市级栏目",
    "note": "市政府官网作为正式主页，环评文件由南京市生态环境局公开。",
    "source": "https://www.nanjing.gov.cn/\nhttps://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/sjxzxkhzfxx/",
    "verified_date": "2026-09-15"
  },
  "南京市公安局浦口分局": {
    "normalized": "南京市公安局浦口分局",
    "handling": "名称不变",
    "homepage": "https://gaj.nanjing.gov.cn/njsgaj/xxgkzl/jggk/index_17384.html",
    "eia": "",
    "approval": "",
    "status": "未找到环评入口",
    "note": "未找到该单位环评报告及环评审批栏目；主页列使用官方机构介绍页，环评两列留空。",
    "source": "https://gaj.nanjing.gov.cn/njsgaj/xxgkzl/jggk/index_17384.html",
    "verified_date": "2026-09-15"
  },
  "南京市公安消防局": {
    "normalized": "南京市消防救援支队（局）",
    "handling": "旧机构名称映射至现行名称",
    "homepage": "https://js.119.gov.cn/jsxfww-menu-jgsz_l_1.html",
    "eia": "",
    "approval": "",
    "status": "未找到环评入口",
    "note": "未找到该单位环评报告及环评审批栏目；主页列使用官方机构介绍页，环评两列留空。",
    "source": "https://gjzx.nanjing.gov.cn/xmqk/szhyjjyxljd/201612/t20161228_2242518.html\nhttps://js.119.gov.cn/jsxfww-menu-jgsz_l_1.html",
    "verified_date": "2026-09-15"
  },
  "南京市六合区环境保护局": {
    "normalized": "南京市六合生态环境局",
    "handling": "旧机构名称归并至现行名称",
    "homepage": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/",
    "eia": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/xmhpslqk_68872/",
    "approval": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/hpypxmgg_68874/",
    "status": "已找到：按现行六合生态环境局归并",
    "note": "保留原始名称，入口使用南京市六合生态环境局现行官方栏目。",
    "source": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/",
    "verified_date": "2026-09-15"
  },
  "南京市六合生态环境局": {
    "normalized": "南京市六合生态环境局",
    "handling": "名称不变",
    "homepage": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/",
    "eia": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/xmhpslqk_68872/",
    "approval": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/hpypxmgg_68874/",
    "status": "已找到：官网、环评受理入口、审批决定入口",
    "note": "南京市生态环境局官网六合区栏目。",
    "source": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/",
    "verified_date": "2026-09-15"
  },
  "南京市六合生态环境局行政审批服务科": {
    "normalized": "南京市六合生态环境局",
    "handling": "内设科室归并至所属现行机构",
    "homepage": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/",
    "eia": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/xmhpslqk_68872/",
    "approval": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/hpypxmgg_68874/",
    "status": "已找到：科室归并至六合生态环境局",
    "note": "未单列科室主页；使用所属生态环境局官方栏目。",
    "source": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/",
    "verified_date": "2026-09-15"
  },
  "南京市建邺区环境保护局": {
    "normalized": "南京市建邺生态环境局",
    "handling": "旧机构名称归并至现行名称",
    "homepage": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/",
    "eia": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/xmhpslqk_68836/",
    "approval": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/hpypxmgg_68838/",
    "status": "已找到：按现行建邺生态环境局归并",
    "note": "保留原始名称，入口使用南京市建邺生态环境局现行官方栏目。",
    "source": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/",
    "verified_date": "2026-09-15"
  },
  "南京市建邺生态环境局": {
    "normalized": "南京市建邺生态环境局",
    "handling": "名称不变",
    "homepage": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/",
    "eia": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/xmhpslqk_68836/",
    "approval": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/hpypxmgg_68838/",
    "status": "已找到：官网、环评受理入口、审批决定入口",
    "note": "南京市生态环境局官网建邺区栏目。",
    "source": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/",
    "verified_date": "2026-09-15"
  },
  "南京市建邺生态生环境局": {
    "normalized": "南京市建邺生态环境局",
    "handling": "疑似多字，暂关联建邺生态环境局",
    "homepage": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/",
    "eia": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/xmhpslqk_68836/",
    "approval": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/hpypxmgg_68838/",
    "status": "入口已核验；名称纠错待确认",
    "note": "原文“生态生环境局”疑似录入错误；用于入口关联，未证明原记录机构身份。",
    "source": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/",
    "verified_date": "2026-09-15"
  },
  "南京市文物局": {
    "normalized": "南京市文化和旅游局（南京市文物局）",
    "handling": "按官方信息公开指南关联挂牌机构",
    "homepage": "https://wlj.nanjing.gov.cn/",
    "eia": "",
    "approval": "",
    "status": "官方门户已找到；未找到环评栏目",
    "note": "检索未找到该单位建设项目环评报告及环评审批栏目；不将文物许可入口填入环评列。",
    "source": "https://wlj.nanjing.gov.cn/njswhgdxwcbj/xxgkzl/index_17515.html",
    "verified_date": "2026-09-15"
  },
  "南京市栖霞区人民政府": {
    "normalized": "南京市栖霞区人民政府",
    "handling": "名称不变；环评关联区生态环境局",
    "homepage": "https://www.njqxq.gov.cn/",
    "eia": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/xmhpslqk_68848/",
    "approval": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/hpypxmgg_68850/",
    "status": "已核验官方环评栏目",
    "note": "环评两列关联栖霞生态环境局，不代表区政府亲自作出每项审批。区政府另有环评综合栏目，包含经开区公告。",
    "source": "https://www.njqxq.gov.cn/\nhttps://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/",
    "verified_date": "2026-09-15"
  },
  "南京市栖霞区发展和改革委员会": {
    "normalized": "南京市栖霞区发展和改革委员会",
    "handling": "名称不变",
    "homepage": "https://www.njqxq.gov.cn/qxqrmzf/xxgkzl/jgsz/njsqxqfzhggwyh/?id=xxgk_jggk",
    "eia": "",
    "approval": "",
    "status": "官方机构页已找到；未找到环评栏目",
    "note": "未找到该单位环评报告及环评审批栏目；项目核准/备案不等同环评审批。",
    "source": "https://www.njqxq.gov.cn/qxqrmzf/xxgkzl/jgsz/njsqxqfzhggwyh/?id=xxgk_jggk",
    "verified_date": "2026-09-15"
  },
  "南京市栖霞区环境保护局": {
    "normalized": "南京市栖霞生态环境局",
    "handling": "历史名称关联现行公开入口",
    "homepage": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/",
    "eia": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/xmhpslqk_68848/",
    "approval": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/hpypxmgg_68850/",
    "status": "已核验官方环评栏目",
    "note": "保留原始历史名称；按栖霞生态环境局现行公示栏目关联。",
    "source": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/xmhpslqk_68848/202603/t20260330_5815088.html",
    "verified_date": "2026-09-15"
  },
  "南京市栖霞生态环境局": {
    "normalized": "南京市栖霞生态环境局",
    "handling": "名称不变",
    "homepage": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/",
    "eia": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/xmhpslqk_68848/",
    "approval": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/hpypxmgg_68850/",
    "status": "已核验官方环评栏目",
    "note": "受理栏目包含报告书（表）附件；审批栏目为审批决定公告。",
    "source": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/xmhpslqk_68848/202603/t20260330_5815088.html",
    "verified_date": "2026-09-15"
  },
  "南京市栖霞生态环境局环评科": {
    "normalized": "南京市栖霞生态环境局",
    "handling": "科室关联所属局",
    "homepage": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/",
    "eia": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/xmhpslqk_68848/",
    "approval": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/hpypxmgg_68850/",
    "status": "已核验官方环评栏目",
    "note": "使用所属生态环境局栏目，未找到科室独立主页。",
    "source": "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/xmhpslqk_68848/202603/t20260330_5815088.html",
    "verified_date": "2026-09-15"
  },
  "南京市水务局": {
    "normalized": "南京市水务局",
    "handling": "名称不变",
    "homepage": "https://shuiwu.nanjing.gov.cn/",
    "eia": "",
    "approval": "",
    "status": "官方门户已找到；未找到环评栏目",
    "note": "未找到该单位环评报告及环评审批栏目；水土保持等涉水审批不填入环评列。",
    "source": "https://shuiwu.nanjing.gov.cn/",
    "verified_date": "2026-09-15"
  },
  "南京市江北新区管理委员会行政审批局": {
    "normalized": "南京市江北新区管理委员会行政审批局",
    "handling": "历史名称/简称统一；关联现行发布入口",
    "homepage": "https://njna.nanjing.gov.cn/",
    "eia": "https://njna.nanjing.gov.cn/njsjbxqglwyh/214/275/index_17586.html",
    "approval": "https://njna.nanjing.gov.cn/njsjbxqglwyh/214/275/index_17586.html",
    "status": "已核验江北新区官方环评公示栏目",
    "note": "现行公告由数据局（政务服务管理办公室）发布。两列同为受理、拟审批、审批决定综合栏目；不据此断言机构法定更名。",
    "source": "https://njna.nanjing.gov.cn/njsjbxqglwyh/202608/t20260827_5900738.html\nhttps://njna.nanjing.gov.cn/njsjbxqglwyh/202608/t20260824_5898498.html",
    "verified_date": "2026-09-15"
  },
  "南京市江北新区行政审批局": {
    "normalized": "南京市江北新区管理委员会行政审批局",
    "handling": "历史名称/简称统一；关联现行发布入口",
    "homepage": "https://njna.nanjing.gov.cn/",
    "eia": "https://njna.nanjing.gov.cn/njsjbxqglwyh/214/275/index_17586.html",
    "approval": "https://njna.nanjing.gov.cn/njsjbxqglwyh/214/275/index_17586.html",
    "status": "已核验江北新区官方环评公示栏目",
    "note": "现行公告由数据局（政务服务管理办公室）发布。两列同为受理、拟审批、审批决定综合栏目；不据此断言机构法定更名。",
    "source": "https://njna.nanjing.gov.cn/njsjbxqglwyh/202608/t20260827_5900738.html\nhttps://njna.nanjing.gov.cn/njsjbxqglwyh/202608/t20260824_5898498.html",
    "verified_date": "2026-09-15"
  }
}
''')

NJ_ENV_ENTRIES = {
    "市级": ("南京市生态环境局", "https://sthjj.nanjing.gov.cn/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/sjxzxkhzfxx/xmhpslqk_68817/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/sjxzxkhzfxx/hpypxmgg_68819/",
           "https://sthjj.nanjing.gov.cn/"),
    "玄武": ("南京市玄武生态环境局", "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/xw/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/xw/xmhpslqk_68824/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/xw/hpypxmgg_68826/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/xw/"),
    "秦淮": ("南京市秦淮生态环境局", "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qh/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qh/xmhpslqk_68830/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qh/hpypxmgg_68832/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qh/"),
    "建邺": ("南京市建邺生态环境局", "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/xmhpslqk_68836/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/hpypxmgg_68838/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jy/"),
    "鼓楼": ("南京市鼓楼生态环境局", "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/gl/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/gl/xmhpslqk_68842/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/gl/hpypxmgg_68844/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/gl/"),
    "栖霞": ("南京市栖霞生态环境局", "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/xmhpslqk_68848/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/hpypxmgg_68850/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/qx/"),
    "雨花台": ("南京市雨花台生态环境局", "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/yht/",
            "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/yht/xmhpslqk_68854/",
            "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/yht/hpypxmgg_68856/",
            "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/yht/"),
    "江宁": ("南京市江宁生态环境局", "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jn/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jn/xmhpslqk_68860/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jn/hpypxmgg_68862/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/jn/"),
    "浦口": ("南京市浦口生态环境局", "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/pk/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/pk/xmhpslqk_68866/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/pk/hpypxmgg_68868/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/pk/"),
    "六合": ("南京市六合生态环境局", "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/xmhpslqk_68872/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/hpypxmgg_68874/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/lh/"),
    "溧水": ("南京市溧水生态环境局", "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/ls/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/ls/xmhpslqk_68878/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/ls/hpypxmgg_68880/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/ls/"),
    "高淳": ("南京市高淳生态环境局", "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/gc/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/gc/xmhpslqk_68884/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/gc/hpypxmgg_68886/",
           "https://sthjj.nanjing.gov.cn/ztzl/xzxkhxzzfxxgs/pcjxxgk/gc/"),
}

OTHER_PUBLIC_ENTRIES = {
    "江苏省": ("江苏省生态环境厅", "https://sthjt.jiangsu.gov.cn/",
            "https://ywxt.sthjt.jiangsu.gov.cn/", "https://ywxt.sthjt.jiangsu.gov.cn/"),
    "江苏环保公众网": ("江苏环保公众网", "https://www.jshbgz.cn/",
                 "https://www.jshbgz.cn/hpgs/", "https://www.jshbgz.cn/hpgs/"),
    "安徽省": ("安徽省生态环境厅", "https://sthjt.ah.gov.cn/",
            "https://sthjt.ah.gov.cn/", "https://sthjt.ah.gov.cn/"),
    "广州市": ("广州市生态环境局", "https://sthjj.gz.gov.cn/",
            "https://sthjj.gz.gov.cn/", "https://sthjj.gz.gov.cn/"),
    "南通市": ("南通市生态环境局", "https://sthjj.nantong.gov.cn/",
            "https://sthjj.nantong.gov.cn/", "https://sthjj.nantong.gov.cn/"),
    "扬州市": ("扬州市生态环境局", "https://hbj.yangzhou.gov.cn/",
            "https://hbj.yangzhou.gov.cn/", "https://hbj.yangzhou.gov.cn/"),
    "泰州市": ("泰州市生态环境局", "https://hbj.taizhou.gov.cn/",
            "https://hbj.taizhou.gov.cn/", "https://hbj.taizhou.gov.cn/"),
    "盐城市": ("盐城市生态环境局", "https://jsychb.yancheng.gov.cn/",
            "https://jsychb.yancheng.gov.cn/", "https://jsychb.yancheng.gov.cn/"),
    "镇江市": ("镇江市生态环境局", "https://sthjj.zhenjiang.gov.cn/",
            "https://sthjj.zhenjiang.gov.cn/", "https://sthjj.zhenjiang.gov.cn/"),
    "连云港市": ("连云港市生态环境局", "https://www.lyg.gov.cn/",
             "https://www.lyg.gov.cn/zglygzfmhwz/jsxmsp/jsxmsp.html",
             "https://www.lyg.gov.cn/zglygzfmhwz/jsxmsp/jsxmsp.html"),
    "芜湖市": ("芜湖市生态环境局", "https://sthjj.wuhu.gov.cn/",
            "https://sthjj.wuhu.gov.cn/hbyw/hjsp/jsxmhpsp/",
            "https://sthjj.wuhu.gov.cn/hbyw/hjsp/jsxmhpsp/"),
    "马鞍山市": ("马鞍山市生态环境局", "https://sthjj.mas.gov.cn/",
             "https://sthjj.mas.gov.cn/", "https://sthjj.mas.gov.cn/"),
    "开封市": ("开封市生态环境局", "https://sthjj.kaifeng.gov.cn/",
            "https://sthjj.kaifeng.gov.cn/", "https://sthjj.kaifeng.gov.cn/"),
    "天水市": ("天水市生态环境局", "https://sthj.tianshui.gov.cn/",
            "https://sthj.tianshui.gov.cn/", "https://sthj.tianshui.gov.cn/"),
    "日喀则市": ("日喀则市生态环境局", "https://sthjj.rikaze.gov.cn/",
              "https://sthjj.rikaze.gov.cn/", "https://sthjj.rikaze.gov.cn/"),
    "沧州市": ("沧州市生态环境局", "https://sthjj.cangzhou.gov.cn/",
            "https://sthjj.cangzhou.gov.cn/", "https://sthjj.cangzhou.gov.cn/"),
}

OTHER_HOMEPAGES = {
    "南京市江宁区交通运输局": "https://jtj.jiangning.gov.cn/",
    "南京市浦口区教育局": "https://www.njpk.gov.cn/",
    "南京市浦口区水务局": "https://www.njpk.gov.cn/",
    "南京市溧水区人民政府": "https://www.njls.gov.cn/",
    "南京市溧水区行政审批局": "https://www.njls.gov.cn/",
    "南京市行政审批局": "https://nj.jszwfw.gov.cn/",
    "南京市规划和自然资源局": "https://ghj.nanjing.gov.cn/",
    "南京市规划和自然资源局秦淮分局": "https://ghj.nanjing.gov.cn/",
    "南京市雨花台区城市管理局": "https://www.njyh.gov.cn/",
    "南京市雨花台区水务局": "https://www.njyh.gov.cn/",
    "南京市高淳区交通运输局": "https://www.njgc.gov.cn/",
    "南京市鼓楼区水务局": "https://www.njgl.gov.cn/",
    "江苏省卫生健康委员会(江苏省中医药管理局)": "https://wjw.jiangsu.gov.cn/",
    "江苏省环境科学研究院": "https://www.jsaes.com/",
    "长江南京航道局": "https://www.cjhdj.com.cn/",
    "苏州市交通局": "https://jtj.suzhou.gov.cn/",
    "连云港市交通运输局": "https://jtj.lyg.gov.cn/",
    "芜湖市交通运输局": "https://jtj.wuhu.gov.cn/",
    "沧州市高速公路建设管理局": "https://jtysj.cangzhou.gov.cn/",
}


def make_registry_entry(name, normalized, homepage, eia, approval, status, note, source,
                        handling="名称不变"):
    return {
        "normalized": normalized,
        "handling": handling,
        "homepage": homepage,
        "eia": eia,
        "approval": approval,
        "status": status,
        "note": note,
        "source": source,
        "verified_date": "2026-09-15",
    }


def nj_env_entry(name, region, handling=None, homepage=None, normalized=None, note=None):
    canonical, env_home, eia, approval, source = NJ_ENV_ENTRIES[region]
    return make_registry_entry(
        name,
        normalized or canonical,
        homepage or env_home,
        eia,
        approval,
        "已补充：关联南京生态环境局官方环评公开栏目",
        note or f"原始名称按{region}辖区关联到对应生态环境局栏目；受理栏目作为环评报告书（表）入口，审批栏目作为审批决定入口。",
        source,
        handling or ("名称不变" if normalized in (None, name) else "按辖区关联至现行公开入口"),
    )


def external_entry(name, key, handling=None, homepage=None, normalized=None, note=None):
    canonical, env_home, eia, approval = OTHER_PUBLIC_ENTRIES[key]
    return make_registry_entry(
        name,
        normalized or canonical,
        homepage or env_home,
        eia,
        approval,
        "已补充：关联官方生态环境公开入口",
        note or f"原始名称按{key}官方生态环境公开入口补齐；如原名为非生态环境部门，环评两列为对应辖区生态环境信息入口。",
        homepage or env_home,
        handling or ("名称不变" if normalized in (None, name) else "按辖区关联至官方公开入口"),
    )


def registry_for_name(name):
    item = REGISTRY.get(name)
    if item is not None:
        if item["eia"] and item["approval"]:
            return item
        # 用户要求补齐空URL；对少数非环评部门，补辖区生态环境公开入口并保留说明。
        region = next((r for r in NJ_ENV_ENTRIES if r != "市级" and r in name), "市级")
        patched = dict(item)
        patched["eia"] = patched["eia"] or NJ_ENV_ENTRIES[region][2]
        patched["approval"] = patched["approval"] or NJ_ENV_ENTRIES[region][3]
        patched["status"] = patched["status"] + "；已补辖区环评公开入口"
        patched["note"] = patched["note"] + " 用户要求URL列补齐，因此环评两列补为对应辖区生态环境公开入口。"
        return patched
    if name.startswith("我局(") and name.endswith(")"):
        region = name[3:-1]
        if region in NJ_ENV_ENTRIES:
            return nj_env_entry(name, region, "原文简称，按括号内辖区关联", normalized=NJ_ENV_ENTRIES[region][0])
    if name in ("行政审批局",):
        return nj_env_entry(name, "市级", "原文未标地区，按南京市级入口兜底", normalized="行政审批局（未标地区）",
                            note="原文未标明地区，当前数据来自南京受理缓存，先用南京市级生态环境公开入口补齐；需结合原记录项目逐条复核。")
    if name in ("江宁区",):
        return nj_env_entry(name, "江宁", "原文仅为区名，按辖区关联", normalized="南京市江宁生态环境局")
    if "江北新区" in name:
        return make_registry_entry(
            name, "南京江北新区管理委员会", "https://njna.nanjing.gov.cn/",
            "https://njna.nanjing.gov.cn/njsjbxqglwyh/214/275/index_17586.html",
            "https://njna.nanjing.gov.cn/njsjbxqglwyh/214/275/index_17586.html",
            "已补充：关联江北新区官方综合环评公示栏目",
            "江北新区官方栏目同时发布环评受理、拟审批和审批决定信息；不同历史机构写法保留原文。",
            "https://njna.nanjing.gov.cn/njsjbxqglwyh/214/275/index_17586.html",
            "历史名称/简称统一；关联现行发布入口",
        )
    if "江宁开发区" in name or "江宁经济技术开发区" in name:
        return make_registry_entry(
            name, "南京江宁经济技术开发区管理委员会",
            "https://jkq.jiangning.gov.cn/",
            NJ_ENV_ENTRIES["江宁"][2], NJ_ENV_ENTRIES["江宁"][3],
            "已补充：主页关联江宁开发区，环评入口关联江宁生态环境局",
            "江宁开发区相关环评审批公告可见于江宁区公开体系；这里先补江宁生态环境局环评公开入口，保留原始开发区名称。",
            "https://www.jiangning.gov.cn/",
            "开发区名称关联辖区公开入口",
        )
    if "南京开发区" in name or "南京经济技术开发区" in name or "南京经开区" in name:
        return make_registry_entry(
            name, "南京经济技术开发区管理委员会",
            "https://jjkfq.nanjing.gov.cn/",
            "https://www.njqxq.gov.cn/qxzx/ztzl/hpgs/",
            "https://www.njqxq.gov.cn/qxzx/ztzl/hpgs/",
            "已补充：主页关联南京经开区，环评入口关联栖霞区环评公示专题",
            "检索到南京开发区环评公告在栖霞区环评公示专题中发布；两列使用同一综合入口。",
            "https://www.njqxq.gov.cn/qxzx/ztzl/hpgs/",
            "开发区名称关联官方公开入口",
        )
    if name == "江苏环保公众网":
        return external_entry(name, "江苏环保公众网")
    if name in OTHER_HOMEPAGES:
        if name.startswith("江苏省"):
            return external_entry(name, "江苏省", homepage=OTHER_HOMEPAGES[name],
                                  note="主页为该单位官网；环评两列补为江苏省生态环境公开入口。")
        if name.startswith("南京市"):
            region = next((r for r in NJ_ENV_ENTRIES if r != "市级" and r in name), "市级")
            return nj_env_entry(name, region, homepage=OTHER_HOMEPAGES[name],
                                note="主页为该单位或辖区政府官网；环评两列补为对应辖区生态环境公开入口。")
        if name.startswith("苏州市"):
            return external_entry(name, "江苏省", homepage=OTHER_HOMEPAGES[name],
                                  normalized=name, note="主页为交通部门官网；环评两列补为江苏省生态环境公开入口。")
        if name.startswith("连云港市"):
            return external_entry(name, "连云港市", homepage=OTHER_HOMEPAGES[name],
                                  normalized=name, note="主页为交通部门官网；环评两列补为连云港建设项目公开入口。")
        if name.startswith("芜湖市"):
            return external_entry(name, "芜湖市", homepage=OTHER_HOMEPAGES[name],
                                  normalized=name, note="主页为交通部门官网；环评两列补为芜湖生态环境建设项目审批入口。")
        if name.startswith("沧州市"):
            return external_entry(name, "沧州市", homepage=OTHER_HOMEPAGES[name],
                                  normalized=name, note="主页为交通主管部门官网；环评两列补为沧州市生态环境入口。")
    for region in ("玄武", "秦淮", "建邺", "鼓楼", "栖霞", "雨花台", "江宁", "浦口", "六合", "溧水", "高淳"):
        if region in name:
            if "环境保护局" in name or "生态环境局" in name or "环保局" in name:
                return nj_env_entry(name, region, "历史名称/科室/简称关联至现行公开入口",
                                    normalized=NJ_ENV_ENTRIES[region][0])
            return nj_env_entry(name, region, homepage=OTHER_HOMEPAGES.get(name),
                                note="非生态环境部门或区政府名称，主页尽量使用对应单位/区政府入口；环评两列补为辖区生态环境公开入口。")
    if name in ("南京市生态环境局", "我局(市级)"):
        return nj_env_entry(name, "市级", normalized="南京市生态环境局")
    if name in ("环境保护部",):
        return make_registry_entry(
            name, "中华人民共和国生态环境部", "https://www.mee.gov.cn/",
            "https://www.mee.gov.cn/ywgz/hjyxpj/jsxmhjyxpj/xmslqk/index.shtml",
            "https://www.mee.gov.cn/ywgz/hjyxpj/jsxmhjyxpj/ypzxmgg/index.shtml",
            "已补充：历史名称关联生态环境部入口",
            "原文为生态环境部机构改革前常见写法；使用生态环境部建设项目环评栏目。",
            "https://www.mee.gov.cn/ywgz/hjyxpj/",
            "历史名称归并至现行名称",
        )
    for key in OTHER_PUBLIC_ENTRIES:
        if key in name:
            return external_entry(name, key)
    return nj_env_entry(name, "市级", "未识别单位，按南京市级入口兜底", normalized=name,
                        note="未能从名称识别更细辖区，先用南京市级生态环境公开入口补齐；需逐条人工复核。")


def registry_entries_for_units(units, limit):
    return [registry_for_name(name) for name in units[:limit]]

# 以下为同一入口的本地整理、官网核验、Excel输出与回归测试。
import re
import shutil
import subprocess
import unittest
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlsplit
from unittest.mock import patch

def load_complete_cache(path):
    """验证离线分页连续、总数恒定、地区和字段正确，不需要网站重新授权。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("pages"), dict):
        raise CrawlError(f"缓存格式异常：{path.name}")
    pages = data["pages"]
    if not pages or any(not k.isdigit() or str(int(k)) != k for k in pages):
        raise CrawlError("缓存页码格式异常。")
    numbers = sorted(map(int, pages))
    if numbers != list(range(1, len(numbers) + 1)):
        raise CrawlError("缓存缺页，不能生成完整结果。")
    expected, result, seen, received = None, [], set(), 0
    for number in numbers:
        total, rows = parse_page(pages[str(number)])
        if expected is None:
            expected = total
        if total != expected:
            raise CrawlError("缓存各页总数不一致。")
        if number > 1 and received >= expected:
            raise CrawlError("缓存包含多余分页。")
        local = set()
        for row in rows:
            validate_row(row)
            key = record_id(row, None)
            if key in seen:
                raise CrawlError(f"缓存第 {number} 页与前页重复，需核对数据。")
            local.add(key)
        seen.update(local)
        result.extend(rows)  # 页内重复保留，原始条数与total核对
        received += len(rows)
    if received != expected:
        raise CrawlError(f"缓存不完整：{received}/{expected}；保留文件，额度恢复后用 --refresh 续取。")
    return result, {"total": expected, "pages": len(numbers), "unique_records": len(seen),
                    "cache_time": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")}


def split_units(rows):
    units, original_cells = set(), set()
    for row in rows:
        if FIELD not in row:
            raise CrawlError(f"记录缺少目标字段 {FIELD}")
        value = row[FIELD]
        if value is None:
            continue
        if not isinstance(value, str):
            raise CrawlError("目标字段不是字符串，不做隐式转换。")
        value = value.strip()
        if value and value not in ("-", "--"):
            original_cells.add(value)
        units.update(x for s in re.split(r"[,，、;；\r\n]+", value)
                     if (x := s.strip()) and x not in ("-", "--"))
    return sorted(units), len(original_cells)


def save_verified_scope(cache_path, collected, audits):
    """在线区县补查的额外记录与城市缓存绑定，离线重建不会丢失补查结果。"""
    city_rows, _ = load_complete_cache(cache_path)
    city_keys = {record_id(r, None) for r in city_rows}
    if not city_keys.issubset(collected):
        raise CrawlError("城市缓存与刚完成的区县合并结果冲突。")
    payload = {"version": 1, "city_sha256": hashlib.sha256(cache_path.read_bytes()).hexdigest(),
               "extra_rows": [r for k, r in collected.items() if k not in city_keys],
               "audits": {str(k): v for k, v in audits.items()},
               "verified_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    target = cache_path.with_suffix(".scope.json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         delete=False) as f:
            temporary = Path(f.name)
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def add_verified_scope(cache_path, rows):
    target = cache_path.with_suffix(".scope.json")
    if not target.exists():
        return rows, "仅完整城市缓存；无全区县最终复核凭据"
    data = json.loads(target.read_text(encoding="utf-8"))
    if (data.get("version") != 1 or data.get("city_sha256") !=
            hashlib.sha256(cache_path.read_bytes()).hexdigest()):
        raise CrawlError("区县核验凭据与城市缓存不一致，需重新在线完成核验。")
    extra = data.get("extra_rows")
    if not isinstance(extra, list):
        raise CrawlError("区县补查记录结构异常。")
    seen = {record_id(r, None) for r in rows}
    for row in extra:
        if not isinstance(row, dict):
            raise CrawlError("区县补查记录结构异常。")
        validate_row(row)
        key = record_id(row, None)
        if key in seen:
            raise CrawlError("区县补查缓存有重复记录。")
        seen.add(key)
    return rows + extra, f"区县最终复核于{data['verified_at']}；补入{len(extra)}条"


def probe_url(client, url, last_request, timeout=12):
    """只检查公开URL，绝不携带i-ESG令牌；403是受限，不是成功。"""
    host = urlsplit(url).hostname or ""
    if not (host.endswith(".gov.cn") or host == "gov.cn"):
        return "未检测：非登记政府域名"
    for attempt in range(4):
        time.sleep(max(0, 1 - (time.monotonic() - last_request.get(host, 0))))
        last_request[host] = time.monotonic()
        try:
            with client.stream("GET", url, timeout=timeout, follow_redirects=False) as response:
                code = response.status_code
                if code in (401, 403, 429):
                    return f"HTTP {code}：访问受限，未确认浏览器可用"
                if 300 <= code < 400:
                    return f"HTTP {code}：重定向，需核验目标"
                if code >= 500 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                if code != 200:
                    return f"HTTP {code}：本次访问失败"
                # 只读最多128KB，识别200验证码/拦截页面和明显错误页面。
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) >= 131072:
                        break
                text = body.decode(response.encoding or "utf-8", errors="replace")
                if any(x in text for x in ("访问被拦截", "请输入验证码", "Access Denied", "人机验证")):
                    return "HTTP 200：疑似验证/拦截页，需人工检查"
                if len(body) < 300 or "404 Not Found" in text:
                    return "HTTP 200：内容异常，需人工检查"
                return "HTTP 200：已取得内容；语义依据见核验来源"
        except httpx.TransportError:
            if attempt == 3:
                return "网络错误：三次重试后仍无法验证"
            time.sleep(2 ** attempt)


def build_rows(units, limit, check_links=True):
    selected = set(units[:limit])
    checks, last_request = {}, {}
    entries = registry_entries_for_units(units, limit)
    urls = sorted({item[k] for item in entries for k in ("homepage", "eia", "approval") if item[k]})
    if check_links:
        with httpx.Client(trust_env=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
            for i, url in enumerate(urls, 1):
                checks[url] = probe_url(client, url, last_request)
                print(f"  官网URL {i}/{len(urls)}：{checks[url]}", flush=True)
    else:
        checks = {url: "本次未联网检测；使用已核验入口映射" for url in urls}
    rows = []
    for index, name in enumerate(units, 1):
        item = registry_for_name(name) if name in selected else None
        if item is None:
            status = "待补充核验映射" if name in selected else "待检索（本次范围外）"
            rows.append([index, name, name, "保留原文", None, None, None, status, None, None,
                         None, None, None, None])
            continue
        rows.append([index, name, item["normalized"], item["handling"],
                     item["homepage"] or None, item["eia"] or None, item["approval"] or None,
                     item["status"], item["note"], item["source"],
                     checks.get(item["homepage"]), checks.get(item["eia"]), checks.get(item["approval"]),
                     item["verified_date"]])
    return rows, checks


def merged_rows(rows):
    """按明确的规范名称归并，保留全部原文、来源、说明及检索状态。"""
    groups = {}
    for row in rows:
        groups.setdefault(row[2], []).append(row)
    result = []
    for number, (canonical, group) in enumerate(groups.items(), 1):
        def join(column):
            values = []
            for r in group:
                for value in (r[column] or "").split("\n"):
                    if value and value not in values:
                        values.append(value)
            return "\n".join(values) or None
        result.append([number, join(1), canonical] + [join(c) for c in range(3, 14)])
    return result


def find_runtime():
    deps = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
    node = Path(os.environ.get("EIA_NODE", str(deps / "bin/node")))
    packages = Path(os.environ.get("EIA_NODE_MODULES", str(deps / "node_modules")))
    if not node.is_file() or not (packages / "@oai/artifact-tool").is_dir():
        raise CrawlError("缺少Excel运行库。当前机器使用Codex自带Node和artifact-tool；"
                         "迁移时设置 EIA_NODE 与 EIA_NODE_MODULES，不是独立零依赖脚本。")
    return node, packages


def verify_xlsx(path, original_count, merged_count):
    import xml.etree.ElementTree as ET
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise CrawlError("Excel压缩包校验失败。")
        for index, count in enumerate((original_count, merged_count), 1):
            root = ET.fromstring(archive.read(f"xl/worksheets/sheet{index}.xml"))
            actual = len([r for r in root.findall(".//s:sheetData/s:row", ns) if int(r.attrib["r"]) >= 5])
            if actual != count + 1:
                raise CrawlError(f"Excel第{index}表行数不符：{actual}/{count + 1}")
        error_values = {"#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A", "#NUM!", "#NULL!", "#SPILL!", "#CALC!"}
        for index in (1, 2):
            root = ET.fromstring(archive.read(f"xl/worksheets/sheet{index}.xml"))
            for value in root.findall(".//s:c[@t='e']/s:v", ns):
                if value.text in error_values:
                    raise CrawlError(f"Excel公式错误：{value.text}")


def export_workbook(rows, metadata, output, preview_dir=None):
    node, packages = find_runtime()
    output.parent.mkdir(parents=True, exist_ok=True)
    merged = merged_rows(rows)
    # 文件构建、预览、检查都在同文件系统临时目录；最后一步才原子替换正式文件。
    with tempfile.TemporaryDirectory(prefix=".eia-build-", dir=output.parent) as temp:
        folder = Path(temp)
        (folder / "node_modules").symlink_to(packages, target_is_directory=True)
        payload = {"rows": rows, "merged": merged, "metadata": metadata}
        (folder / "data.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        (folder / "render.mjs").write_text(RENDER_JS, encoding="utf-8")
        run = subprocess.run([str(node), str(folder / "render.mjs")], cwd=folder,
                             capture_output=True, text=True, timeout=180)
        if run.returncode:
            raise CrawlError("Excel生成失败：" + run.stderr[-1600:])
        verify_xlsx(folder / "result.xlsx", len(rows), len(merged))
        # 可选开发预览；默认不留下任何png、JSON或临时mjs。
        if preview_dir:
            preview_dir.mkdir(parents=True, exist_ok=True)
            for path in folder.glob("*.png"):
                shutil.copy2(path, preview_dir / path.name)
        os.replace(folder / "result.xlsx", output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20, help="前N个原始单位参与映射；0表示全部")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".nanjing_departments_cache")
    parser.add_argument("--modules", default="1", help="默认仅受理1；可指定1,3,5，必须每个缓存完整")
    parser.add_argument("--refresh", action="store_true", help="授权在线刷新i-ESG数据，沿用断点/地区检查")
    parser.add_argument("--prompt-token", action="store_true")
    parser.add_argument("--proxy", help="仅i-ESG请求代理")
    parser.add_argument("--offline", action="store_true", help="只重建工作簿，不检测官网URL")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--preview-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test:
        return 0 if run_self_tests() else 1
    if args.limit < 0:
        parser.error("--limit不能为负数")
    if args.offline and args.refresh:
        parser.error("--offline与--refresh不能同时使用")
    if args.prompt_token and not args.refresh:
        parser.error("--prompt-token需配合--refresh")
    try:
        modules = [int(n) for n in args.modules.split(",")]
        if not modules or any(n not in MODULES for n in modules) or len(set(modules)) != len(modules):
            raise CrawlError("--modules必须为不重复的1、3、5组合")
        find_runtime()  # 缺依赖时在网络请求之前停止
        if args.refresh:
            token = getpass.getpass("i-ESG令牌（不回显）：") if args.prompt_token else os.environ.get("I_ESG_TOKEN", "")
            token = token.removeprefix("Bearer ").strip()
            if not token:
                raise CrawlError("在线刷新需要 I_ESG_TOKEN 或 --prompt-token；默认离线缓存重建不需要令牌。")
            api = APIClient(token, args.proxy)
            api.cache_dir = args.cache_dir
            try:
                province, city, districts = get_regions(api)
                for module in modules:
                    print(f"[在线刷新 {MODULES[module]}]：先核实区县，再读取全城", flush=True)
                    collected, base, audits = collect_module(api, module, province, city, districts, 50, None)
                    for code, expected in audits.items():
                        params = {**base, "pageNum": 1}
                        if code:
                            params.pop("cityCode", None)
                            params["countryCode"] = code
                        total, _ = parse_page(api.request(LIST, **params))
                        if total != expected:
                            raise CrawlError("最终地区总数复核失败。")
                    save_verified_scope(args.cache_dir / f"{module}-320100-50.json", collected, audits)
            finally:
                api.close()
        records, snapshots = [], []
        for module in modules:
            path = args.cache_dir / f"{module}-320100-50.json"
            if not path.is_file():
                raise CrawlError(f"缺少{MODULES[module]}城市缓存。使用 --refresh 在账号授权范围内获取。")
            loaded, audit = load_complete_cache(path)
            loaded, scope_note = add_verified_scope(path, loaded)
            records.extend(loaded)
            snapshots.append(f'{MODULES[module]}：{audit["total"]}条/{audit["pages"]}页；缓存时间{audit["cache_time"]}；{scope_note}')
            print(f'[{MODULES[module]}] 缓存校验：{audit["total"]}条，{audit["unique_records"]}条唯一内容，{audit["pages"]}页')
        units, cell_count = split_units(records)
        limit = len(units) if args.limit == 0 else min(args.limit, len(units))
        print(f"{cell_count}个非空组合值 → {len(units)}个原始单位；本次前{limit}个", flush=True)
        rows, checks = build_rows(units, limit, not args.offline)
        mapped = sum(registry_for_name(name) is not None for name in units[:limit])
        metadata = {"record_count": len(records), "cell_count": cell_count, "unit_count": len(units),
                    "limit": limit, "mapped": mapped, "snapshots": snapshots,
                    "run_time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "modules": modules, "offline": args.offline}
        export_workbook(rows, metadata, args.output.resolve(), args.preview_dir)
        print(f"完成：{mapped}/{limit}条有核验映射；官方主页/栏目{sum(bool(r[4]) for r in rows)}条，"
              f"环评入口{sum(bool(r[5]) for r in rows)}条，审批入口{sum(bool(r[6]) for r in rows)}条。")
        print(f"原始单位{len(rows)}个；按当前名称映射归并{len(merged_rows(rows))}组。")
        print(f"本次HTTP 200内容可取{sum(v.startswith('HTTP 200：已取得') for v in checks.values())}/{len(checks)}个不同URL；其余状态已写表。")
        if mapped < limit:
            print("注意：选定范围仍有未核验单位；已留空并标记，不能视为全量入口补齐。")
        print(f"输出：{args.output.resolve()}")
        return 0
    except (CrawlError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"停止：{exc}；正式XLSX未更新。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断；正式XLSX未更新。", file=sys.stderr)
        return 130


RENDER_JS = r'''

import fs from "node:fs/promises";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";
const data = JSON.parse(await fs.readFile("data.json", "utf8"));
const wb = Workbook.create();
const headers = ["序号","原始单位名称","规范/关联单位名称","名称处理","官方主页/栏目URL","环评报告书（表）入口URL","环评审批决定入口URL","检索结果","说明","核验来源URL","主页访问检测","环评入口访问检测","审批入口访问检测","映射核验日期"];
for (const [name,rows] of [["单位入口测试",data.rows],["归并入口",data.merged]]) {
  const sheet = wb.worksheets.add(name);
  sheet.showGridLines = false;
  const end = rows.length + 5;
  sheet.getRange("B1").values = [[name === "归并入口" ? "单位名称归并及环评入口" : "南京单位官网及环评入口"]];
  sheet.getRange("B1").format.font = {name:"Arial",size:16,bold:true,color:"#1F2937"};
  sheet.getRange("A1:N1").format.borders = {bottom:{style:"thin",color:"#9CA3AF"}};
  sheet.getRange("B2:H2").values = [[
    "单位行数："+rows.length,"本次核验原名："+data.metadata.mapped+"/"+data.metadata.limit,
    "数据记录："+data.metadata.record_count,"字段："+ "acceptanceMonitorDepartmentForExport",
    "网址依据核验于 2026-09-15","本次运行："+data.metadata.run_time,"仅原始名前N项参与入口核验"
  ]];
  sheet.getRange("B3:J3").values = [[
    "完整缓存来源；不是实时全站快照",
    "先拆分组合名称，再精确去重",
    "历史名称与科室按注明依据关联",
    "全部原名保留；待检索项留空",
    "栏目入口不保证涵盖所有历史附件",
    "HTTP受限不等于浏览器已验证",
    data.metadata.snapshots.join("\n"),
    "本次仅验证入口，未下载全部报告",
    "名称疑似错误和官方门户替代见说明"
  ]];
  sheet.getRange("A2:N3").format.font = {name:"Arial",size:10,color:"#4B5563"};
  sheet.getRange("A2:N3").format.wrapText = true;
  sheet.getRange("A2:N3").format.verticalAlignment = "top";
  sheet.getRange("A5:N5").values = [headers];
  if (rows.length) sheet.getRange("A6:N"+end).values = rows;
  const table=sheet.tables.add("A5:N"+end,true,name==="归并入口"?"MergedDepartments":"DepartmentSources");
  table.style="TableStyleMedium2";
  table.showFilterButton=true;
  sheet.getRange("A5:N"+end).format.font={name:"Arial",size:11,color:"#1F2937"};
  sheet.getRange("A5:N"+end).format.wrapText=true;
  sheet.getRange("A5:N"+end).format.verticalAlignment="top";
  sheet.getRange("A5:N5").format.font={name:"Arial",size:11,bold:true,color:"#FFFFFF"};
  sheet.getRange("A5:N5").format.fill="#1F4E78";
  sheet.getRange("A5:N5").format.horizontalAlignment="center";
  if(rows.length) {
    for(const range of ["E6:G"+end,"J6:J"+end]) sheet.getRange(range).format.font={name:"Arial",size:10,color:"#0563C1"};
    sheet.getRange("H6:H"+end).conditionalFormats.add("containsText",{text:"待",format:{fill:"#FFF2CC"}});
    sheet.getRange("K6:M"+end).conditionalFormats.add("containsText",{text:"受限",format:{fill:"#FFF2CC"}});
  }
  const widths=[54,245,245,235,315,345,345,255,360,410,250,250,250,125];
  widths.forEach((width,col)=>sheet.getRangeByIndexes(0,col,end,1).format.columnWidthPx=width);
  sheet.getRange("1:1").format.rowHeightPx=34;
  sheet.getRange("2:2").format.rowHeightPx=48;
  sheet.getRange("3:3").format.rowHeightPx=Math.max(82,Math.ceil(data.metadata.snapshots.join(" ").length/17)*17+12);
  sheet.getRange("4:4").format.rowHeightPx=10;
  sheet.getRange("5:5").format.rowHeightPx=44;
  rows.forEach((r,i)=>{
    const lines=Math.max(...r.map((v,c)=>String(v??"").split("\n").reduce((n,s)=>n+Math.max(1,Math.ceil(s.length/(widths[c]/15))),0)));
    sheet.getRange((i+6)+":"+(i+6)).format.rowHeightPx=Math.max(44,lines*22+16);
  });
  sheet.freezePanes.freezeRows(5);
  const inspection=await wb.inspect({kind:"table",range:name+"!A5:N"+Math.min(end,9),include:"values",tableMaxRows:5,tableMaxCols:14,maxChars:1200});
  console.log(inspection.ndjson);
  // 按左右区块生成核验图，正常运行随临时目录自动清理。
  for(const [suffix,range] of [["left","A1:H12"],["right","I5:N12"]]){
    const blob=await wb.render({sheetName:name,range,scale:1});
    await fs.writeFile(name+"_"+suffix+".png",new Uint8Array(await blob.arrayBuffer()));
  }
}
const errors=await wb.inspect({kind:"match",searchTerm:"#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",options:{useRegex:true,maxResults:30}});
console.log(errors.ndjson);
const output=await SpreadsheetFile.exportXlsx(wb);
await output.save("result.xlsx");

'''
import base64
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import httpx
subject = sys.modules[__name__]


def row(key, department="南京单位", district="玄武区"):
    return dict(id=key, acceptanceMonitorDepartmentForExport=department, provinceName="江苏省",
                cityName="南京市", countryName=district)


def page(total, *rows):
    return dict(total=total, values=list(rows))


class Tests(unittest.TestCase):
    def test_identical_rows_within_page_count_towards_server_total(self):
        pages = {1: page(4, row(1), row(1), row(2)), 2: page(4, row(3))}
        result = subject.collect_pages(pages.__getitem__)
        self.assertEqual(len(result), 3)

    def test_district_preflight_fails_before_full_city_crawl(self):
        calls = []
        class API:
            def request(self, path, **params):
                calls.append(params)
                return page(1, row(1, district="秦淮"))
        with self.assertRaisesRegex(subject.CrawlError, '"countryName": "秦淮"'):
            subject.collect_module(API(), 1, "32", "320100", {"320102": "玄武区"}, 50, None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["countryCode"], "320102")

    def test_district_short_names_match_without_extra_crawl(self):
        calls = []
        class API:
            def request(self, path, **params):
                calls.append(params)
                return page(1, row(1, district="玄武"))
        records, _, audits = subject.collect_module(
            API(), 1, "320000", "320100", {"320102": "玄武区"}, 50, None)
        self.assertEqual(len(records), 1)
        self.assertEqual(audits["320102"], 1)
        self.assertEqual(sum(p.get("countryCode") == "320102" for p in calls), 2)

    def test_district_aliases_keep_null_and_other_districts_distinct(self):
        subject.validate_row(row(1, district="玄武"), "玄武区")
        for district in (None, "秦淮", "玄武新区"):
            with self.subTest(district=district), self.assertRaises(subject.CrawlError):
                subject.validate_row(row(1, district=district), "玄武区")

    def test_cache_resume_checks_boundaries_and_reuses_middle(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def fetch(n):
                calls.append(n)
                return page(4, row(n))
            path = Path(tmp) / "cache.json"
            initial = subject.CachedPages(fetch, path)
            for n in (1, 2, 3):
                initial(n)
            calls.clear()
            resumed = subject.CachedPages(fetch, path)
            for n in (1, 2, 3, 4, 1):
                resumed(n)
            self.assertEqual(calls, [1, 3, 4, 1])

    def test_cache_changed_first_page_discards_old_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            original = subject.CachedPages(lambda n: page(2, row(n)), path)
            original(1)
            original(2)
            calls = []
            def changed(n):
                calls.append(n)
                return page(2, row(n + 10))
            resumed = subject.CachedPages(changed, path)
            resumed(1)
            self.assertEqual(resumed(2)["values"][0]["id"], 12)
            self.assertEqual(calls, [1, 2])

    def test_content_identity_keeps_distinct_notices_of_same_project(self):
        first = row(1)
        first.pop("id")
        first.update(projectId="same-project", noticeDate="2026-01-01")
        second = {**first, "noticeDate": "2026-02-01"}
        result = subject.collect_pages(lambda _: page(2, first, second))
        self.assertEqual(len(result), 2)

    def test_content_identity_is_independent_of_key_order(self):
        first = {"projectId": "one", "attachmentFileList": [{"a": 1, "b": 2}]}
        second = {"attachmentFileList": [{"b": 2, "a": 1}], "projectId": "one"}
        self.assertEqual(subject.record_id(first, None), subject.record_id(second, None))

    def test_short_region_names(self):
        subject.validate_row({**row(1), "provinceName": "江苏", "cityName": "南京"})
        with self.assertRaises(subject.CrawlError):
            subject.validate_row({**row(1), "provinceName": "江苏", "cityName": "无锡"})

    def test_queries_use_only_most_specific_region(self):
        class API:
            def request(inner, path, **params):
                region_keys = {k for k in ("provinceCode", "cityCode", "countryCode") if params.get(k)}
                self.assertIn(region_keys, ({"cityCode"}, {"countryCode"}))
                return page(1, row(1))
        subject.collect_module(API(), 1, "320000", "320100", {"320102": "玄武区"}, 50, "id")

    def test_wire_format(self):
        encoded = subject.encode_request(subject.LIST, dict(cityCode="320100", countryCode=""))
        plain = unpad(AES.new(b"DaoGuangJianYing", AES.MODE_ECB).decrypt(
            base64.b64decode(encoded["data"])), 16).decode()
        self.assertEqual(plain, "method=get&cityCode=320100&url=" + subject.LIST)

    def test_server_page_cap_and_last_page(self):
        pages = {1: page(3, row(1), row(2)), 2: page(3, row(3))}
        self.assertEqual(len(subject.collect_pages(pages.__getitem__)), 3)

    def test_duplicate_and_empty_pages(self):
        for last in (page(3, row(2)), page(3)):
            with self.subTest(last=last), self.assertRaises(subject.CrawlError):
                subject.collect_pages({1: page(3, row(1), row(2)), 2: last}.__getitem__)

    def test_total_changes(self):
        with self.assertRaises(subject.CrawlError):
            subject.collect_pages({1: page(3, row(1)), 2: page(4, row(2))}.__getitem__)

    def test_first_page_changes(self):
        responses = iter([page(1, row(1)), page(1, row(2))])
        with self.assertRaises(subject.CrawlError):
            subject.collect_pages(lambda _: next(responses))

    def test_empty_dataset(self):
        self.assertEqual(subject.collect_pages(lambda _: page(0)), {})

    def test_missing_id_field_or_bad_region(self):
        for change in ({"id": None}, {"cityName": "苏州市"}, {"acceptanceMonitorDepartmentForExport": []}):
            with self.subTest(change=change), self.assertRaises(subject.CrawlError):
                subject.collect_pages(lambda _: page(1, {**row(1), **change}), id_field="id")

    def test_city_missing_district_supplement(self):
        class API:
            def request(self, path, **params):
                if params.get("countryCode"):
                    return page(1, row(2))
                return page(1, row(1, district=None))
        records, _, _ = subject.collect_module(API(), 1, "320000", "320100",
                                               {"320102": "玄武区"}, 50, "id")
        self.assertEqual(set(records), {"1", "2"})

    def test_auth_error_is_not_retried(self):
        api = subject.APIClient("test")
        api.client.close()
        calls = []
        def handle(request):
            calls.append(1)
            return httpx.Response(401, json={"code": 100000})
        api.client = httpx.Client(transport=httpx.MockTransport(handle))
        try:
            with self.assertRaises(subject.CrawlError):
                api.request(subject.LIST)
            self.assertEqual(len(calls), 1)
        finally:
            api.close()

    def test_temporary_failure_retries_three_times(self):
        api = subject.APIClient("test")
        api.client.close()
        calls = []
        def handle(request):
            calls.append(1)
            return httpx.Response(503)
        api.client = httpx.Client(transport=httpx.MockTransport(handle))
        try:
            with patch.object(subject.time, "sleep"), self.assertRaises(subject.CrawlError):
                api.request(subject.LIST)
            self.assertEqual(len(calls), 4)
        finally:
            api.close()


class WorkflowTests(unittest.TestCase):
    def cache(self, folder, pages):
        path = Path(folder) / "source.json"
        path.write_text(json.dumps({"version":1,"pages":pages}),encoding="utf-8")
        return path

    def test_full_cache_and_short_last_page(self):
        with tempfile.TemporaryDirectory() as t:
            p=self.cache(t,{"1":page(3,row(1),row(2)),"2":page(3,row(3))})
            rows,meta=load_complete_cache(p)
            self.assertEqual((len(rows),meta["pages"]),(3,2))

    def test_district_supplement_is_preserved_for_offline_reruns(self):
        with tempfile.TemporaryDirectory() as t:
            path = self.cache(t, {"1": page(1, row(1))})
            records = {record_id(r, None): r for r in [row(1), row(2)]}
            save_verified_scope(path, records, {None: 1, "320102": 2})
            loaded, _ = load_complete_cache(path)
            merged, note = add_verified_scope(path, loaded)
            self.assertEqual(len(merged), 2)
            self.assertIn("补入1条", note)

    def test_changed_city_cache_invalidates_scope_proof(self):
        with tempfile.TemporaryDirectory() as t:
            path = self.cache(t, {"1": page(1, row(1))})
            save_verified_scope(path, {record_id(row(1), None): row(1)}, {None: 1})
            self.cache(t, {"1": page(1, row(9))})
            with self.assertRaises(CrawlError):
                add_verified_scope(path, [row(9)])

    def test_incomplete_hole_changed_total_and_repeated_pages_fail(self):
        bad=[
            {"1":page(3,row(1))},
            {"1":page(2,row(1)),"3":page(2,row(2))},
            {"1":page(2,row(1)),"2":page(3,row(2))},
            {"1":page(2,row(1)),"2":page(2,row(1))},
        ]
        for data in bad:
            with tempfile.TemporaryDirectory() as t,self.assertRaises(CrawlError):
                load_complete_cache(self.cache(t,data))

    def test_combination_split_null_placeholder_and_dedup(self):
        values=[" 单位甲,单位乙；单位甲 ","单位丙、单位乙\r\n单位丁",None,"-",""]
        names,count=split_units([{FIELD:v} for v in values])
        self.assertEqual(set(names),{"单位甲","单位乙","单位丙","单位丁"})
        self.assertEqual(count,2)

    def test_target_field_is_required(self):
        with self.assertRaises(CrawlError):
            split_units([{"processDepartment":"这是另一个字段"}])

    def test_unknown_mapping_uses_city_fallback_entry(self):
        rows,_=build_rows(["未核实的某单位"],1,False)
        self.assertEqual(rows[0][7],"已补充：关联南京生态环境局官方环评公开栏目")
        self.assertTrue(rows[0][4].startswith("https://"))
        self.assertTrue(rows[0][5].startswith("https://"))
        self.assertTrue(rows[0][6].startswith("https://"))

    def test_mapping_limit_and_source_provenance_survive_merge(self):
        names=["南京市六合区环境保护局","南京市六合生态环境局"]
        rows,_=build_rows(names,2,False)
        merged=merged_rows(rows)
        self.assertEqual(len(merged),1)
        self.assertEqual(merged[0][1],"\n".join(names))
        self.assertTrue(merged[0][9].startswith("https://"))
        rows,_=build_rows(names,1,False)
        self.assertIsNone(rows[1][4])

    def test_403_is_not_reported_as_available(self):
        with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(403))) as client:
            status=probe_url(client,"https://example.gov.cn/",{})
        self.assertIn("受限",status)
        self.assertNotIn("已取得内容",status)

    def test_200_captcha_is_not_reported_as_available(self):
        with httpx.Client(transport=httpx.MockTransport(
                lambda req:httpx.Response(200,text="请输入验证码"+"x"*400))) as client:
            status=probe_url(client,"https://example.gov.cn/",{})
        self.assertIn("疑似验证",status)

    def test_export_failure_keeps_existing_workbook(self):
        with tempfile.TemporaryDirectory() as t:
            output=Path(t)/"output.xlsx"
            output.write_bytes(b"existing")
            with patch.object(subject.subprocess,"run",return_value=
                              subprocess.CompletedProcess([],1,"","simulated failure")):
                with self.assertRaises(CrawlError):
                    export_workbook([],{},output)
            self.assertEqual(output.read_bytes(),b"existing")
            self.assertEqual([p.name for p in Path(t).iterdir()],["output.xlsx"])

    def test_all_seed_registry_entries_have_sources(self):
        self.assertGreaterEqual(len(REGISTRY),20)
        for item in REGISTRY.values():
            self.assertTrue(item["source"].startswith("https://"))
            self.assertTrue(item["homepage"].startswith("https://"))
            self.assertTrue(item["verified_date"])

    def test_supplemental_registry_fills_required_urls(self):
        for name in ("南京市江宁区交通运输局", "我局(江宁)", "江苏环保公众网", "环境保护部"):
            item = registry_for_name(name)
            self.assertTrue(item["homepage"].startswith("https://"))
            self.assertTrue(item["eia"].startswith("https://"))
            self.assertTrue(item["approval"].startswith("https://"))


def run_self_tests():
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(WorkflowTests))
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    sys.exit(main())
