"""通用页面解析：列表链接、详情页标题/日期/表格/附件/图片/百度网盘。"""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit

from selectolax.parser import HTMLParser, Node

from . import classify

DATE = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
PUB_DATE = re.compile(r"(?:发布日期|发布时间|发文日期|成文日期|日期|时间)\s*[:：]\s*(20\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2})")
FILE_EXT = re.compile(r"\.(pdf|docx?|wps|rtf|ofd|xlsx?|zip|rar|7z)$", re.I)
IMG_EXT = re.compile(r"\.(jpe?g|png)$", re.I)
IMG_NOISE = re.compile(r"logo|icon|qrcode|ewm|erweima|jiucuo|wza|dzjg|banner|share|beian|gongan|police|blank|arrow|btn", re.I)
BAIDU = re.compile(r"https?://pan\.baidu\.com/s/[\w\-]+(?:\?pwd=(\w{4}))?")
BAIDU_CODE = re.compile(r"(?:提取码|提取密码|密码)\s*[:：]?\s*([A-Za-z0-9]{4})")
CONTENT_SELECTORS = [".TRS_Editor", "#zoom", ".zoom", ".article-content", ".article_con", ".xl_con", ".detail-content",
                     ".content", "#content", ".article", ".con", ".news_content", ".main-txt", ".bt-article", ".view"]


def fmt_date(m: re.Match) -> str:
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def text_of(node: Node | None) -> str:
    return re.sub(r"\s+", " ", node.text(separator=" ") if node else "").strip()


@dataclass
class ListItem:
    url: str
    title: str
    date: str | None = None
    extra: dict = field(default_factory=dict)


def _title_score(a: Node, title: str) -> tuple:
    """同一链接出现多次时选哪个文字当标题：有 title 属性优先，其次不是“附件：xxx.pdf”这种，再次更长。"""
    bad = title.startswith("附件") or bool(FILE_EXT.search(title))
    return (bool(a.attributes.get("title")), not bad, len(title))


def list_links(html: str, base_url: str, pattern: re.Pattern) -> list[ListItem]:
    tree = HTMLParser(html)
    out: dict[str, ListItem] = {}
    scores: dict[str, tuple] = {}
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith("javascript"):
            continue
        url = urljoin(base_url, href)
        if not pattern.search(url):
            continue
        title = re.sub(r"\s+", "", a.attributes.get("title") or a.text() or "")
        title = re.sub(r"\[?\(?20\d{2}-\d{1,2}-\d{1,2}\)?\]?$", "", title)
        date = None
        node = a.parent
        for _ in range(3):
            if node is None:
                break
            t = node.text()
            if len(t) > 400:
                break
            m = DATE.search(t)
            if m:
                date = fmt_date(m)
                break
            node = node.parent
        score = _title_score(a, title)
        if url in out:
            if score > scores[url]:
                out[url].title, scores[url] = title, score
            out[url].date = out[url].date or date
        else:
            out[url], scores[url] = ListItem(url, title, date), score
    return list(out.values())


@dataclass
class Detail:
    title: str
    pub_date: str | None
    rows: list[dict]
    files: list[tuple[str, str]]          # (url, 名称)
    images: list[str]
    baidu: list[tuple[str, str | None]]   # (链接, 提取码)
    content_html: str


def _title(tree: HTMLParser, hint: str | None = None) -> str:
    stem = re.sub(r"(\.\.\.|…)+$", "", classify.clean(hint or ""))
    if len(stem) >= 8:  # 列表标题被截断时，在详情页里找以同样开头的完整标题
        best = None
        for node in tree.css("title,h1,h2,h3,h4,strong,b,p,span,td,.title,.tit,.article-title,.bt"):
            t = classify.clean(node.text())
            if t.startswith(stem[:12]) and len(stem) <= len(t) <= len(stem) + 150 and (best is None or len(t) < len(best)):
                best = t
        if best:
            return re.split(r"[-|_]", best)[0] if len(re.split(r"[-|_]", best)[0]) >= len(stem) else best
    t = text_of(tree.css_first("title"))
    parts = [p.strip() for p in re.split(r"\s+[-|_]\s+|_|\|", t) if p.strip()]
    best = parts[0] if parts else ""
    if len(best) < 8:
        for sel in ("h1", ".title", ".article-title", "h2"):
            h = text_of(tree.css_first(sel))
            if len(h) >= 8:
                return h
    return best


def _content_node(tree: HTMLParser) -> Node | None:
    best, best_len = None, 0
    for sel in CONTENT_SELECTORS:
        for node in tree.css(sel):
            n = len(node.text())
            if n > best_len:
                best, best_len = node, n
        if best_len > 200:
            return best
    return best or tree.body


def _file_name(a: Node, url: str) -> str:
    name = re.sub(r"\s+", "", a.attributes.get("title") or a.text() or "")
    if not name or name in ("下载", "点击下载", "附件"):
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return name


def parse_tables(tree: HTMLParser, base_url: str) -> list[dict]:
    rows_out: list[dict] = []
    for table in tree.css("table"):
        trs = table.css("tr")
        header_idx, header = None, None
        for i, tr in enumerate(trs[:4]):
            cells = [classify.clean(td.text()) for td in tr.css("td,th")]
            if any(("项目名称" in c or "文件名称" in c) for c in cells):
                header_idx, header = i, cells
                break
        if header is None:
            continue
        colmap: dict[str, int] = {}
        for j, h in enumerate(header):
            if "项目名称" in h or h == "项目":
                colmap.setdefault("name", j)
            elif "文件名称" in h:
                colmap.setdefault("file_name", j)
            elif "建设单位" in h:
                colmap.setdefault("company", j)
            elif "建设地点" in h:
                colmap.setdefault("location", j)
            elif "评价机构" in h or "环评机构" in h or "环评单位" in h:
                colmap.setdefault("eia_org", j)
            elif "文号" in h:
                colmap.setdefault("doc_no", j)
            elif "时间" in h or "日期" in h:
                colmap.setdefault("doc_date", j)
        for tr in trs[header_idx + 1:]:
            tds = tr.css("td,th")
            cells = [classify.clean(td.text()) for td in tds]
            if len(cells) < 2:
                continue
            row = {k: cells[j] for k, j in colmap.items() if j < len(cells) and cells[j]}
            key_text = row.get("name") or row.get("file_name")
            if not key_text or key_text in header:
                continue
            row["links"] = []
            for a in tr.css("a[href]"):
                url = urljoin(base_url, a.attributes["href"].strip())
                row["links"].append((url, _file_name(a, url)))
            rows_out.append(row)
    return rows_out


def parse_detail(html: str, url: str, hint_title: str | None = None) -> Detail:
    tree = HTMLParser(html)
    for bad in tree.css("script,style,noscript"):
        bad.decompose()
    title = _title(tree, hint_title)
    body_text = text_of(tree.body)
    m = PUB_DATE.search(body_text)
    pub_date = None
    if m:
        pub_date = fmt_date(DATE.search(m.group(1)))
    else:
        m2 = DATE.search(title)
        pub_date = fmt_date(m2) if m2 else None

    files: dict[str, str] = {}
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("javascript", "mailto")):
            continue
        u = urljoin(url, href)
        path = unquote(urlsplit(u).path)
        if FILE_EXT.search(path):
            name = _file_name(a, u)
            if u not in files or len(name) > len(files[u]):
                files[u] = name

    content = _content_node(tree)
    images: list[str] = []
    doc_stem = urlsplit(url).path.rsplit("/", 1)[-1].split(".")[0]
    for img in (content.css("img[src]") if content else []):
        u = urljoin(url, img.attributes["src"].strip())
        path = urlsplit(u).path
        if not IMG_EXT.search(path) or IMG_NOISE.search(path):
            continue
        if doc_stem in path or re.search(r"/(files|images|upload|uploads|attach)/", path, re.I):
            if u not in images:
                images.append(u)

    raw_text = htmllib.unescape(html)
    baidu: list[tuple[str, str | None]] = []
    for bm in BAIDU.finditer(raw_text):
        link = bm.group(0)
        code = bm.group(1)
        if not code:
            cm = BAIDU_CODE.search(raw_text[bm.end(): bm.end() + 200])
            code = cm.group(1) if cm else None
        if link not in [b[0] for b in baidu]:
            baidu.append((link, code))

    return Detail(title=title, pub_date=pub_date, rows=parse_tables(tree, url), files=list(files.items()),
                  images=images, baidu=baidu, content_html=content.html if content else "")
