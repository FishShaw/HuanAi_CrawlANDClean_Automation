"""列表翻页的公共循环：只认本栏目路径的链接；本页没有新链接即视为翻到底。"""
from __future__ import annotations

import logging
import re
from typing import Callable, Iterator

from .. import extract
from ..http import FetchError, Fetcher

log = logging.getLogger("eia")


def crawl_pages(src: dict, fetcher: Fetcher, known: set[str], incremental: bool, max_pages: int | None,
                url_for_page: Callable[[int], str],
                total_pages_of: Callable[[str], int | None] | None = None) -> Iterator[list[extract.ListItem]]:
    pattern = re.compile(src["link_pattern"])
    seen: set[str] = set()
    total = None
    n = 0
    while True:
        url = url_for_page(n)
        try:
            html, _ = fetcher.get_text(url, save_raw=False)
        except FetchError as e:
            if n == 0:
                raise
            log.warning("[列表] %s 第 %d 页失败，停止翻页：%s", src["id"], n + 1, e)
            break
        if total is None and total_pages_of:
            total = total_pages_of(html)
        items = [it for it in extract.list_links(html, url, pattern) if it.url not in seen]
        if not items:
            break
        seen.update(it.url for it in items)
        yield items
        n += 1
        if incremental and all(it.url in known for it in items):
            break
        if max_pages and n >= max_pages:
            break
        if total is not None and n >= total + 1:
            break
