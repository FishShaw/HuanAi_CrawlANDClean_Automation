"""TRS 系网站：列表 xxx_list.shtml / xxx_list_N.shtml，页数写在 createPageHTML('page_div', 总页数, ...) 里。
适用：苏州市生态环境局、苏州高新区、吴中、相城、张家港、昆山，以及无锡、镇江、宿迁等同类站点。"""
from __future__ import annotations

import re

from .base import crawl_pages

PAGER = re.compile(r"createPageHTML\(\s*'[^']*'\s*,\s*(\d+)")


def _total_pages(html: str) -> int | None:
    m = PAGER.search(html)
    return int(m.group(1)) if m else None


def iter_pages(src, fetcher, known, incremental, max_pages):
    root, ext = src["list_url"].rsplit(".", 1)

    def url_for_page(n: int) -> str:
        # 不同站点第 2 页可能是 _1 也可能是 _2；从 _1 开始连续请求，重复页由 crawl_pages 去重
        return src["list_url"] if n == 0 else f"{root}_{n}.{ext}"

    return crawl_pages(src, fetcher, known, incremental, max_pages, url_for_page, _total_pages)
