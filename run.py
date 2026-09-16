#!/usr/bin/env python3
"""环评报告 / 批复采集管线（苏州试点）。

  .venv/bin/python run.py all                       # 全量：列表→详情→下载→转PDF→匹配→导出
  .venv/bin/python run.py all --incremental         # 每日增量：列表翻到没有新公告为止
  .venv/bin/python run.py list --sources sz_sthjj_pz --max-pages 2
  .venv/bin/python run.py detail --limit 50
  .venv/bin/python run.py download | convert | match | export | coverage
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from crawler import adapters, db, download, export, match, pipeline, to_pdf
from crawler.http import Fetcher

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
FILES = DATA / "files"
OUT = ROOT / "output"
log = logging.getLogger("eia")
_tls = threading.local()


def conn():
    if not hasattr(_tls, "conn"):
        _tls.conn = db.connect(DATA / "eia.sqlite")
    return _tls.conn


def load_sources(only: str | None) -> dict[str, dict]:
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    wanted = set(only.split(",")) if only else None
    out = {}
    for s in cfg["sources"]:
        s = {**cfg.get("defaults", {}), **s}
        if s.get("enabled", True) and (wanted is None or s["id"] in wanted):
            out[s["id"]] = s
    if wanted and wanted - out.keys():
        sys.exit(f"sources.yaml 里没有：{', '.join(wanted - out.keys())}")
    return out


def host(url: str) -> str:
    return urlsplit(url).hostname or ""


def apply_limit(rows: list[dict], args) -> list[dict]:
    if not args.limit:
        return rows
    if not args.per_source:
        return rows[:args.limit]
    counts: dict[str, int] = defaultdict(int)
    out = []
    for r in rows:
        if counts[r["source_id"]] < args.limit:
            counts[r["source_id"]] += 1
            out.append(r)
    return out


def by_host(jobs: dict[str, list], fn) -> None:
    """同一域名的任务串行（配合 Fetcher 的限速），不同域名并行。"""
    def worker(items):
        for item in items:
            try:
                fn(item)
            except Exception:
                log.exception("任务失败：%s", item.get("url") or item.get("id"))
    if not jobs:
        return
    with ThreadPoolExecutor(max_workers=min(12, len(jobs))) as ex:
        for f in [ex.submit(worker, items) for items in jobs.values()]:
            f.result()


def cmd_list(args, sources, fetcher):
    jobs = defaultdict(list)
    for s in sources.values():
        jobs[host(s.get("list_url") or s["portal_url"])].append(s)

    def crawl(src):
        c = conn()
        known = {r[0] for r in c.execute("SELECT url FROM notices WHERE source_id=?", (src["id"],))}
        pages = seen = new = 0
        for items in adapters.get(src["adapter"]).iter_pages(src, fetcher, known, args.incremental, args.max_pages):
            pages += 1
            seen += len(items)
            new += pipeline.insert_items(c, src, items)
            if pages % 10 == 0:
                log.info("[列表] %s 已翻 %d 页，%d 条，新增 %d", src["id"], pages, seen, new)
        log.info("[列表] %s 完成：%d 页，%d 条，新增 %d", src["id"], pages, seen, new)
    by_host(jobs, crawl)


def cmd_detail(args, sources, fetcher):
    stages = args.stages.split(",")
    q = ("SELECT * FROM notices WHERE status IN ('new','error') AND stage IN ({}) AND source_id IN ({})"
         " ORDER BY COALESCE(list_date,'') DESC").format(",".join("?" * len(stages)), ",".join("?" * len(sources)))
    rows = apply_limit([dict(r) for r in conn().execute(q, (*stages, *sources))], args)
    log.info("[详情] 待处理 %d 条", len(rows))
    jobs = defaultdict(list)
    for r in rows:
        jobs[host(r["url"])].append(r)
    done = {"n": 0}
    lock = threading.Lock()

    def one(n):
        pipeline.process_detail(conn(), fetcher, sources[n["source_id"]], n, FILES)
        with lock:
            done["n"] += 1
            if done["n"] % 50 == 0:
                log.info("[详情] 进度 %d/%d", done["n"], len(rows))
    by_host(jobs, one)


def cmd_download(args, sources, fetcher):
    statuses = "('new','failed')" if args.retry_failed else "('new')"
    q = ("SELECT a.*, n.source_id FROM attachments a JOIN notices n ON n.id=a.notice_id"
         f" WHERE a.status IN {statuses} AND a.kind IN ('file','images') AND a.doc_type IN ('report','approval')"
         " AND n.source_id IN ({}) ORDER BY a.id DESC").format(",".join("?" * len(sources)))
    rows = apply_limit([dict(r) for r in conn().execute(q, tuple(sources))], args)
    log.info("[下载] 待下载 %d 个", len(rows))
    jobs = defaultdict(list)
    for r in rows:
        jobs[db.loads(r["extra"]).get("host") or host(r["url"])].append(r)
    by_host(jobs, lambda a: download.download_one(conn(), fetcher, a, FILES))


def cmd_convert(args, sources, fetcher):
    log.info("[转换] %s", to_pdf.convert_pending(conn(), FILES, args.limit))


def cmd_match(args, sources, fetcher):
    log.info("[匹配] %s", match.build(conn(), args.city))


def cmd_export(args, sources, fetcher):
    log.info("[导出] %s", export.run(conn(), OUT, args.city))


def cmd_coverage(args, sources, fetcher):
    print(export.coverage(conn(), OUT, args.city, sources))


def cmd_all(args, sources, fetcher):
    for step in (cmd_list, cmd_detail, cmd_download, cmd_convert, cmd_match, cmd_export, cmd_coverage):
        step(args, sources, fetcher)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["list", "detail", "download", "convert", "match", "export", "coverage", "all"])
    p.add_argument("--sources", help="逗号分隔的来源 id（默认全部）")
    p.add_argument("--max-pages", type=int, help="每个来源最多翻几页（试跑用）")
    p.add_argument("--incremental", action="store_true", help="翻到整页都是已知公告就停")
    p.add_argument("--limit", type=int, help="详情/下载/转换最多处理几条（试跑用）")
    p.add_argument("--per-source", action="store_true", help="--limit 按每个来源分别计算")
    p.add_argument("--stages", default="S0,S1,S2,S3,UNKNOWN", help="要抓详情的阶段")
    p.add_argument("--retry-failed", action="store_true", help="下载时重试之前失败的附件")
    p.add_argument("--city", default="苏州市")
    args = p.parse_args()

    (DATA / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%m-%d %H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(DATA / "logs" / "run.log", encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sources = load_sources(args.sources)
    fetcher = Fetcher(DATA / "raw", min_interval=float(next(iter(sources.values())).get("min_interval", 3.0)))
    globals()[f"cmd_{args.command}"](args, sources, fetcher)


if __name__ == "__main__":
    main()
