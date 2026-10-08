"""按审批阶段抽样公告，看环评报告全本挂在哪个阶段。

要回答的问题：某个省是不是「基本只有受理阶段拿得到完整环评报告」。
做法：每个栏目按标题把公告分成 受理 / 拟审批 / 审批决定 三个阶段，
每阶段抽若干条打开详情页，看页面上有没有指向报告正文的附件。

「有报告」的判定（is_report）：
- 附件扩展名是 pdf/doc/docx/zip/rar/7z/wps
- 决定类（审查意见、审批意见、批文文号、「…的函」）和随附材料（删减说明、概况与对策措施摘要、
  公示表）一律不算；公众参与说明只有和报告打包时才算
- 剩下的附件，名字像正文（报告书 / 报告表 / 环评报告 / 全本 / 环评文本 / 公示稿 / 公示版，
  或直接用项目名命名——安徽马鞍山、蚌埠都这么挂）**或者**文件 ≥ 1.5MB 就算。
  大小是硬证据：全本动辄几 MB 到几十 MB，拟审批阶段挂的「概况、主要环境影响及对策措施」
  摘要只有几十 KB
- 指向非政府域名（建设单位网站、第三方平台）的不算——交付口径只认官方来源

    .venv/bin/python scripts/report_stage_probe.py --spec <tsv> --per-stage 12 > out.tsv
spec 每行：省\t市\t阶段(受理|拟审批|审批决定|混合)\t栏目URL
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import random
import re
import sys
import urllib.parse
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "list_api"))

import collect  # noqa: E402

STAGE = [
    ("受理", re.compile(r"受理")),
    ("拟审批", re.compile(r"拟(?:作出|批准|审批|对|同意)|审批前|批前|拟批")),
    ("审批决定", re.compile(r"批复|审批决定|审批意见|作出.{0,12}(?:决定|批复)|审批结果|已批准|准予|决定公告")),
]
EXT = re.compile(r"\.(pdf|docx?|zip|rar|7z|wps)(?:$|\?)", re.I)
# 正文的写法。蚌埠标成「【环评文本】」，金华写「环评报告-…-公示稿」，湖州打包成「…报告书公示稿及公众参与说明.zip」
REPORT = re.compile(r"报告书|报告表|环评报告|环境影响报告|环境影响评价文件|环评文件|全本|环评文本|报告文本|公示文本|"
                    r"公示稿|公示本|公开本|公示版|报批稿|送审稿")
# 安徽马鞍山、蚌埠直接用项目名命名全本
NAMED = re.compile(r"(?:项目|工程|改造|扩建|技改|生产线|基地|园区)[^/]{0,30}\.(?:pdf|docx?|zip|rar|7z|wps)")
# 决定类文件一律不算：浙江审批决定阶段挂的是审查意见 / 批文，文件名里带着报告书全名
# （「湖环建〔2026〕36号-关于…环境影响报告书的审查意见」），只看「报告书」会全部误收
DECISION = re.compile(r"审查意见|审批意见|批复|许可决定|决定书|不予批准|不予许可|的函|"
                      r"[〔\[]\s*20\d\d\s*[〕\]]|（20\d\d）\d+号")
# 随附的非正文材料
SIDE = re.compile(r"删减|删除.{0,4}说明|承诺|公示表|登记表|一览表|信息表|申请表|情况说明|专家意见|"
                  r"技术评估|反馈|附图|概况|对策|措施|简本|摘要")
# 公众参与说明单独挂时不算；和报告打包在一起（湖州那种 zip）就算
PUBLIC = re.compile(r"公参|公众参与")
BIG = 1_500_000


MIN_PAGES = 30


def is_report(label: str, size: int, pages=None) -> bool:
    """pages 是可选的页数取值函数。名字认不出、只凭大小进来的文件才会去数页：
    宁波审批决定挂的「附件1」是 8 页的扫描批文（0.9–1.6MB），受理挂的是 450 多页的报告书。"""
    if DECISION.search(label) or SIDE.search(label):
        return False
    strong = REPORT.search(label)
    if PUBLIC.search(label) and not strong:
        return False
    if strong or NAMED.search(label):
        return True
    if size < BIG:
        return False
    n = pages() if pages else None
    return n is None or n >= MIN_PAGES


def stage_of(title: str) -> str:
    # 「受理并拟批准」这类合并公示归受理——全本若挂着，就是受理阶段拿得到
    for name, pat in STAGE:
        if pat.search(title):
            return name
    return ""


def gov(host: str) -> bool:
    return host.endswith(".gov.cn") or re.match(r"^\d+\.\d+\.\d+\.\d+", host) is not None


def attachments(url: str) -> tuple[list[str], list[str], list[str]]:
    """返回 (官方来源的报告附件, 非官方来源的报告附件, 页面上全部附件)。"""
    r = collect.get(url)
    off, ext, every = [], [], []
    for tag, inner in re.findall(r"(<a\b[^>]*>)(.*?)</a>", r.text, re.S):
        m = re.search(r"href=[\"']([^\"']+)[\"']", tag)
        if not m:
            continue
        href = urllib.parse.urljoin(str(r.url), m.group(1).strip())
        text = re.sub(r"<[^>]+>|\s+", "", inner)
        t = re.search(r"title=[\"']([^\"']*)", tag)
        label = text + " " + (t.group(1) if t else "") + " " + urllib.parse.unquote(href.rsplit("/", 1)[-1])
        if not (EXT.search(href) or EXT.search(label)):
            continue
        every.append(label.strip()[:50])
        if DECISION.search(label) or SIDE.search(label):
            continue
        size = _size(href)
        if not is_report(label, size, lambda h=href: pdf_pages(h)):
            continue
        tag_ = f"{label.strip()[:50]} [{size / 1e6:.1f}MB]"
        (off if gov(urllib.parse.urlparse(href).netloc) else ext).append(tag_)
    return off, ext, every


def pdf_pages(href: str, cap: int = 80_000_000):
    """数 PDF 页数，取不到返回 None（非 PDF、下载失败）。

    先只取头尾各 1MB 找页树的 /Count；线性化和普通 PDF 的页树一般在这两段里。
    找不到再整份下载（封顶 80MB）。"""
    try:
        counts = []
        for rng in ("bytes=0-1048575", "bytes=-1048576"):
            r = httpx.get(href, headers={**collect.UA, "Range": rng}, timeout=60, follow_redirects=True)
            if not r.content.startswith(b"%PDF") and rng.startswith("bytes=0"):
                return None
            counts += [int(x) for x in re.findall(rb"/Type\s*/Pages\b[^>]{0,200}?/Count\s+(\d+)", r.content)]
            counts += [int(x) for x in re.findall(rb"/Count\s+(\d+)[^>]{0,200}?/Type\s*/Pages\b", r.content)]
            if r.status_code == 200:      # 不支持 Range，已经拿到整份
                break
        if counts:
            return max(counts)
        with httpx.stream("GET", href, headers=collect.UA, timeout=120, follow_redirects=True) as g:
            buf = bytearray()
            for chunk in g.iter_bytes():
                buf += chunk
                if len(buf) > cap:
                    break
        m = [int(x) for x in re.findall(rb"/Count\s+(\d+)", bytes(buf))]
        return max(m) if m else len(re.findall(rb"/Type\s*/Page(?!s)", bytes(buf))) or None
    except Exception:
        return None


def _size(href: str) -> int:
    """文件大小。HEAD 不回长度、Range 不支持的站（浙江 jpaas 的 download?fileUrl=）
    就流式 GET 只读响应头，拿 Content-Length 后立刻断开，不下载正文。"""
    try:
        h = httpx.head(href, headers=collect.UA, timeout=15, follow_redirects=True)
        n = int(h.headers.get("content-length") or 0)
        if n:
            return n
        with httpx.stream("GET", href, headers=collect.UA, timeout=20, follow_redirects=True) as g:
            n = int(g.headers.get("content-length") or 0)
            m = re.search(r"/(\d+)$", g.headers.get("content-range", ""))
            return n or (int(m.group(1)) if m else 0)
    except Exception:
        return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--per-stage", type=int, default=12)
    ap.add_argument("--max-pages", type=int, default=8)
    args = ap.parse_args()
    random.seed(7)

    jobs = []
    for line in open(args.spec, encoding="utf-8"):
        if not line.strip() or line.startswith("#"):
            continue
        prov, city, declared, url = line.rstrip("\n").split("\t")[:4]
        try:
            fam, links = collect.collect(url, args.max_pages)
        except Exception as e:
            print(f"{prov}\t{city}\t{declared}\tLIST_ERR\t{type(e).__name__}", file=sys.stderr)
            continue
        by = {}
        for u, t in links:
            s = stage_of(t) if declared == "混合" else declared
            if s and (declared == "混合" or stage_of(t) in ("", s)):
                by.setdefault(s, []).append((u, t))
        for s, items in by.items():
            for u, t in random.sample(items, min(args.per_stage, len(items))):
                jobs.append((prov, city, s, url, u, t))
        print(f"{prov}\t{city}\t{declared}\t{fam}\t{len(links)}条\t" +
              " ".join(f"{k}:{len(v)}" for k, v in by.items()), file=sys.stderr)

    def run(j):
        try:
            off, ext, every = attachments(j[4])
            return j + (len(off), len(ext), " | ".join(off[:2]), len(every), " | ".join(every[:4]), "")
        except Exception as e:
            return j + (0, 0, "", 0, "", type(e).__name__)

    print("省\t市\t阶段\t栏目\t公告\t标题\t官方报告附件数\t非官方报告附件数\t附件样例"
          "\t全部附件数\t全部附件\t错误")
    with cf.ThreadPoolExecutor(6) as ex:
        for row in ex.map(run, jobs):
            # 标题里偶尔夹着制表符，会把后面的列整体挤歪
            print("\t".join(str(x).replace("\t", " ").replace("\n", " ") for x in row), flush=True)


if __name__ == "__main__":
    main()
