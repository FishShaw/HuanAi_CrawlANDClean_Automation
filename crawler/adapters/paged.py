"""页码写在查询参数里的网站：第 1 页用 list_url，第 N 页用 page_url.format(n=N)。
适用：常熟经开区（?page=N）、常熟新材料产业园（?pageNumber=N）、常熟高新区（?page=N）。"""
from __future__ import annotations

from .base import crawl_pages


def iter_pages(src, fetcher, known, incremental, max_pages):
    def url_for_page(n: int) -> str:
        return src["list_url"] if n == 0 else src["page_url"].format(n=n + 1)

    return crawl_pages(src, fetcher, known, incremental, max_pages, url_for_page)
