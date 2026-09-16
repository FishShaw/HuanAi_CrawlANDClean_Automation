#!/usr/bin/env python3
"""从 schema_pg.sql 生成设计文档 HTML（字段字典随 DDL 自动同步）。

运行：python3 docs/national_eia_db/build_doc.py   → 输出 docs/national_eia_db/design.html
"""
from __future__ import annotations

import html
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIGJAM = "https://www.figma.com/board/TdR3kQEZJbEOuXjlHkOv8D/?node-id=20-166"

LAYER = {
    "app_config": "dict", "dict_stage": "dict", "dict_report_version": "dict", "dict_source_class": "dict", "dict_scope_rule": "dict",
    "dict_gap_reason": "dict",
    "region": "r", "authority": "r", "site": "r", "entry": "r", "entry_scope": "r",
    "crawl_run": "o", "notice": "o", "notice_item": "o", "file_blob": "o", "attachment": "o",
    "eia_case": "i", "case_item_link": "i", "case_document": "i",
    "case_delivery": "d", "case_gap": "d", "review_task": "d",
}
LAYER_NAME = {"dict": "配置 / 字典", "r": "登记", "o": "观察", "i": "推断", "d": "交付"}
LAYER_ORDER = ["r", "o", "i", "d", "dict"]

COL = re.compile(r"^\s{2}([a-z_0-9]+)\s+([a-z]+(?:\(\d+(?:,\d+)?\))?(?:\[\])?)\s*(.*?)\s*(?:--\s*(.*))?$")


def parse_schema(sql: str) -> list[dict]:
    tables = []
    for m in re.finditer(r"CREATE TABLE (\w+) \(([^\n]*)\n(.*?)\n\);", sql, flags=re.S):
        name, head, body = m.group(1), m.group(2), m.group(3)
        comment = head.split("--", 1)[1].strip() if "--" in head else ""
        cols, pk_cols = [], []
        lines = body.split("\n")
        for i, line in enumerate(lines):
            pk = re.match(r"^\s+PRIMARY KEY \(([^)]*)\)", line)
            if pk:
                pk_cols = [c.strip() for c in pk.group(1).split(",")]
                continue
            cm = COL.match(line)
            if not cm:
                continue
            col, typ, rest, note = cm.groups()
            # 多行 CHECK 的枚举值在后续行，拼到括号配平为止
            j = i + 1
            while j < len(lines) and (rest.rstrip(",").endswith("IN") or rest.count("(") > rest.count(")")):
                rest = rest + " " + lines[j].strip()
                j += 1
            flags = []
            if "PRIMARY KEY" in rest:
                flags.append("PK")
            if "AS IDENTITY" in rest:
                flags.append("自增")
            ref = re.search(r"REFERENCES (\w+)\((\w+)\)", rest)
            if ref:
                flags.append(f"FK → {ref.group(1)}")
            if re.search(r"\bUNIQUE\b", rest):
                flags.append("UK")
            if "NOT NULL" in rest and "PRIMARY KEY" not in rest:
                flags.append("必填")
            enum = re.search(r"IN\s*\(([^)]*)\)", rest)
            dflt = re.search(r"(?<!BY )DEFAULT ([^\s,]+)", rest)
            cols.append({
                "name": col, "type": typ, "flags": flags,
                "enum": [v.strip().strip("'") for v in enum.group(1).split(",")] if enum else [],
                "default": dflt.group(1) if dflt else "",
                "note": (note or "").strip(),
            })
        for c in cols:
            if c["name"] in pk_cols and "PK" not in c["flags"]:
                c["flags"].insert(0, "PK")
        tables.append({"name": name, "comment": comment, "cols": cols})
    return tables


def indexes(sql: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"CREATE (UNIQUE )?INDEX (\w+)\s+ON (\w+) \(([^)]*)\)\s*(WHERE [^;]*)?;", sql, flags=re.S):
        uniq, _idx, table, cols, where = m.groups()
        desc = f"{'唯一' if uniq else '索引'} ({cols.strip()})" + (f" {' '.join(where.split())}" if where else "")
        out.setdefault(table, []).append(desc)
    return out


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def field_dictionary(sql: str) -> str:
    tables = parse_schema(sql)
    idx = indexes(sql)
    by_layer: dict[str, list[dict]] = {}
    for t in tables:
        by_layer.setdefault(LAYER[t["name"]], []).append(t)
    parts = []
    for layer in LAYER_ORDER:
        for t in by_layer.get(layer, []):
            rows = []
            for c in t["cols"]:
                flags = "".join(f'<span class="flag">{esc(f)}</span>' for f in c["flags"])
                extra = []
                if c["enum"]:
                    extra.append("取值：" + " / ".join(f"<code>{esc(v)}</code>" for v in c["enum"]))
                if c["default"] and c["default"] != "CURRENT_TIMESTAMP":
                    extra.append(f"默认 <code>{esc(c['default'])}</code>")
                note = esc(c["note"]) + ("<br>" if c["note"] and extra else "") + "；".join(extra)
                rows.append(
                    f'<tr><td class="f">{esc(c["name"])}</td><td class="t">{esc(c["type"])}</td>'
                    f'<td class="k">{flags}</td><td>{note}</td></tr>')
            idx_html = ""
            if t["name"] in idx:
                idx_html = '<p class="idx">' + "　".join(f"<code>{esc(d)}</code>" for d in idx[t["name"]]) + "</p>"
            parts.append(f"""
<section class="entity" id="t-{t['name']}">
  <div class="entity-head">
    <span class="chip chip-{layer}">{LAYER_NAME[layer]}</span>
    <h4><code>{esc(t['name'])}</code></h4>
    <span class="entity-cn">{esc(t['comment'])}</span>
  </div>
  <div class="tbl"><table>
    <thead><tr><th>字段</th><th>类型</th><th>约束</th><th>说明</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
  {idx_html}
</section>""")
    return "\n".join(parts)


TEMPLATE = r"""<title>全国环评两件套采集库</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Noto+Sans+SC:wght@400;500;700&family=Noto+Serif+SC:wght@600;700&display=swap">
<style>
:root {
  --paper: #F4F5F7;
  --surface: #FFFFFF;
  --ink: #1B1E23;
  --muted: #5A6170;
  --rule: #D7DBE2;
  --rule-strong: #B9BFC9;
  --red: #B3312A;
  --code-bg: #E9ECF1;
  --r-bg: #FFECBD; --r-ink: #5B4510;
  --o-bg: #CFE8FF; --o-ink: #0E3E66;
  --i-bg: #E3D8FF; --i-ink: #3A2A78;
  --d-bg: #FFE3A3; --d-ink: #5B4510;
  --dict-bg: #E6E9EE; --dict-ink: #3A404A;
  --pass: #1E7A4C;
  --ok-bg: #DDF1E6; --ok-ink: #145C38;
  --no-bg: #F6E0DE; --no-ink: #8A241E;
  --tbd-bg: #FFF0C7; --tbd-ink: #6B4E00;
  --focus: #2F6FDB;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #14161A; --surface: #1C1F25; --ink: #E5E8ED; --muted: #9BA2AE;
    --rule: #2C3038; --rule-strong: #434955; --red: #E0675F; --code-bg: #262A32;
    --r-bg: #4A3C16; --r-ink: #FFE3A0; --o-bg: #173651; --o-ink: #BFE0FF;
    --i-bg: #312758; --i-ink: #DCD0FF; --d-bg: #54410F; --d-ink: #FFE3A0;
    --dict-bg: #2A2E36; --dict-ink: #C9CED8; --pass: #5CC48E;
    --ok-bg: #173D2A; --ok-ink: #9FE0BC; --no-bg: #4A2221; --no-ink: #F2B3AE;
    --tbd-bg: #45380F; --tbd-ink: #F5D77F; --focus: #7FA8F0;
  }
}
:root[data-theme="dark"] {
  --paper: #14161A; --surface: #1C1F25; --ink: #E5E8ED; --muted: #9BA2AE;
  --rule: #2C3038; --rule-strong: #434955; --red: #E0675F; --code-bg: #262A32;
  --r-bg: #4A3C16; --r-ink: #FFE3A0; --o-bg: #173651; --o-ink: #BFE0FF;
  --i-bg: #312758; --i-ink: #DCD0FF; --d-bg: #54410F; --d-ink: #FFE3A0;
  --dict-bg: #2A2E36; --dict-ink: #C9CED8; --pass: #5CC48E;
  --ok-bg: #173D2A; --ok-ink: #9FE0BC; --no-bg: #4A2221; --no-ink: #F2B3AE;
  --tbd-bg: #45380F; --tbd-ink: #F5D77F; --focus: #7FA8F0;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 400 16px/1.8 "Noto Sans SC", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing: antialiased;
}
a { color: inherit; text-decoration-color: var(--rule-strong); text-underline-offset: 3px; }
a:hover { text-decoration-color: var(--red); }
a:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 2px; }
code, .mono { font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace; }
code { font-size: 0.86em; background: var(--code-bg); padding: 1px 5px; border-radius: 3px; word-break: break-word; }

.wrap {
  max-width: 1200px; margin: 0 auto; padding-inline: 20px; padding-block: 48px 120px;
  display: grid; grid-template-columns: 210px minmax(0, 1fr); gap: 56px;
}
nav.toc { position: sticky; top: 28px; align-self: start; font-size: 13.5px; line-height: 1.6; }
nav.toc .toc-label { font-size: 11px; letter-spacing: 0.14em; color: var(--muted); margin-bottom: 10px; }
nav.toc ol { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
nav.toc a { text-decoration: none; color: var(--muted); display: block; padding-left: 12px; border-left: 2px solid var(--rule); }
nav.toc a:hover { color: var(--ink); border-left-color: var(--red); }
nav.toc a.sub { padding-left: 24px; }
main { min-width: 0; }
@media (max-width: 980px) {
  .wrap { grid-template-columns: minmax(0, 1fr); gap: 24px; padding-block: 28px 80px; }
  nav.toc { position: static; }
  nav.toc ol { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  nav.toc a.sub { padding-left: 12px; }
}

header.doc .issuer { font-size: 13px; letter-spacing: 0.18em; color: var(--red); font-weight: 500; }
header.doc h1 {
  font-family: "Noto Serif SC", "Songti SC", "STSong", serif; font-weight: 700;
  font-size: clamp(32px, 4.4vw, 48px); line-height: 1.22; margin: 8px 0 0; text-wrap: balance; letter-spacing: 0.02em;
}
header.doc .redline { height: 3px; background: var(--red); margin: 20px 0 18px; }
header.doc .lede { font-size: 18px; line-height: 1.85; max-width: 44em; margin: 0; }
header.doc .meta { margin-top: 18px; display: flex; flex-wrap: wrap; gap: 6px 22px; font-size: 13px; color: var(--muted); }
header.doc .meta b { color: var(--ink); font-weight: 500; }

h2 {
  font-family: "Noto Serif SC", "Songti SC", serif; font-weight: 700; font-size: 28px; line-height: 1.35;
  margin: 76px 0 18px; padding-top: 18px; border-top: 1px solid var(--rule-strong); text-wrap: balance; scroll-margin-top: 20px;
}
h2 .num { color: var(--red); margin-right: 10px; font-weight: 600; }
h3 { font-size: 19px; font-weight: 700; line-height: 1.5; margin: 40px 0 10px; text-wrap: balance; scroll-margin-top: 20px; }
h4 { margin: 0; font-size: 16px; }
p { margin: 10px 0; max-width: 46em; }
ul, ol { padding-left: 1.3em; margin: 10px 0; }
li { max-width: 46em; margin: 5px 0; }
.muted { color: var(--muted); }
strong { font-weight: 700; }

.verdict { display: grid; gap: 0; margin: 22px 0 0; border-top: 1px solid var(--rule); }
.verdict > div { display: grid; grid-template-columns: 7.5em minmax(0, 1fr); gap: 16px; padding: 14px 0; border-bottom: 1px solid var(--rule); }
.verdict dt { font-size: 13px; letter-spacing: 0.08em; color: var(--muted); padding-top: 2px; }
.verdict dd { margin: 0; }
@media (max-width: 560px) { .verdict > div { grid-template-columns: minmax(0, 1fr); gap: 2px; } }

.tbl { overflow-x: auto; background: var(--surface); border: 1px solid var(--rule); border-radius: 6px; margin: 14px 0 22px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; line-height: 1.65; }
th, td { text-align: left; vertical-align: top; padding: 9px 13px; border-bottom: 1px solid var(--rule); }
tbody tr:last-child td { border-bottom: 0; }
th { font-size: 12px; font-weight: 500; letter-spacing: 0.08em; color: var(--muted); background: var(--surface); white-space: nowrap; }
td.f { font-family: "JetBrains Mono", ui-monospace, Menlo, monospace; font-size: 13px; white-space: nowrap; font-weight: 500; }
td.t { font-family: "JetBrains Mono", ui-monospace, Menlo, monospace; font-size: 12.5px; color: var(--muted); white-space: nowrap; }
td.k { white-space: nowrap; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td code { font-size: 12px; }
.flag { display: inline-block; font: 500 11px/1 "JetBrains Mono", monospace; padding: 3px 5px; margin: 1px 4px 1px 0; border-radius: 3px; border: 1px solid var(--rule-strong); color: var(--muted); }
.pass { color: var(--pass); font-weight: 500; white-space: nowrap; }

.chip { display: inline-block; font-size: 12px; font-weight: 500; line-height: 1; padding: 5px 8px; border-radius: 4px; letter-spacing: 0.06em; white-space: nowrap; }
.chip-r { background: var(--r-bg); color: var(--r-ink); }
.chip-o { background: var(--o-bg); color: var(--o-ink); }
.chip-i { background: var(--i-bg); color: var(--i-ink); }
.chip-d { background: var(--d-bg); color: var(--d-ink); }
.chip-dict { background: var(--dict-bg); color: var(--dict-ink); }
.pill { display: inline-block; font-size: 12px; font-weight: 500; line-height: 1; padding: 4px 7px; border-radius: 999px; white-space: nowrap; }
.pill-yes { background: var(--ok-bg); color: var(--ok-ink); }
.pill-no { background: var(--no-bg); color: var(--no-ink); }
.pill-tbd { background: var(--tbd-bg); color: var(--tbd-ink); }

.layers { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 18px 0 8px; }
.layer { padding: 14px 14px 12px; border-radius: 6px; background: var(--surface); border: 1px solid var(--rule); display: grid; gap: 8px; align-content: start; }
.layer .tables { font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 12.5px; line-height: 1.7; color: var(--muted); }
.layer p { margin: 0; font-size: 14px; line-height: 1.7; }
@media (max-width: 860px) { .layers { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 480px) { .layers { grid-template-columns: minmax(0, 1fr); } }

.chain { font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 13px; background: var(--surface); border: 1px solid var(--rule); border-radius: 6px; padding: 12px 14px; overflow-x: auto; white-space: nowrap; }

.diagram { background: var(--surface); border: 1px solid var(--rule); border-radius: 6px; padding: 16px; overflow-x: auto; margin: 14px 0; }
.diagram pre.mermaid { margin: 0; min-width: 760px; }

.entity { margin: 28px 0 0; scroll-margin-top: 20px; }
.entity-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 12px; }
.entity-head h4 code { background: none; padding: 0; font-size: 17px; font-weight: 500; }
.entity-cn { color: var(--muted); font-size: 14px; }
.entity .tbl { margin-top: 10px; }
.idx { font-size: 13px; color: var(--muted); margin: -12px 0 0; max-width: none; }

ol.steps { counter-reset: s; list-style: none; padding: 0; display: grid; gap: 0; border-top: 1px solid var(--rule); }
ol.steps > li { counter-increment: s; display: grid; grid-template-columns: 2.4em minmax(0, 1fr); gap: 10px; padding: 12px 0; border-bottom: 1px solid var(--rule); max-width: none; margin: 0; }
ol.steps > li::before { content: counter(s); font-family: "JetBrains Mono", monospace; font-size: 13px; color: var(--red); padding-top: 3px; }

.note { border-left: 3px solid var(--red); padding: 4px 0 4px 14px; margin: 18px 0; max-width: 46em; }
footer { margin-top: 80px; padding-top: 18px; border-top: 1px solid var(--rule); font-size: 13px; color: var(--muted); }
footer ul { list-style: none; padding: 0; display: grid; gap: 4px; }
</style>

<div class="wrap">
<nav class="toc" aria-label="目录">
  <div class="toc-label">目录</div>
  <ol>
    <li><a href="#verdict">结论</a></li>
    <li><a href="#decisions">业务口径</a></li>
    <li><a href="#facts">一、拆回基本面</a></li>
    <li><a href="#patches">二、只在修补表面的部分</a></li>
    <li><a href="#path">三、重新推导的路径</a></li>
    <li><a class="sub" href="#scope">范围与来源</a></li>
    <li><a class="sub" href="#status">交付状态</a></li>
    <li><a class="sub" href="#fields">字段字典</a></li>
    <li><a class="sub" href="#linking">名字怎么关联</a></li>
    <li><a class="sub" href="#flow">入库流程</a></li>
    <li><a class="sub" href="#naming">命名规范</a></li>
    <li><a href="#premises">四、成立的前提</a></li>
    <li><a href="#verify">五、验证的第一步</a></li>
  </ol>
</nav>

<main>
<header class="doc">
  <div class="issuer">数据库设计稿 · v0.4</div>
  <h1>全国环评两件套采集库</h1>
  <div class="redline" role="presentation"></div>
  <p class="lede">每一个在范围内、已批准的环评审批事项，库里都有来自<strong>官方网站</strong>的<strong>报告</strong>和<strong>审批意见</strong>两份文件，每份都能沿着「文件 ← 公告 ← 栏目入口」追到源头；拿不到的，写清楚为什么、查过哪里、什么时候再查。</p>
  <div class="meta">
    <span><b>日期</b> 2026-09-15（业务口径已全部确认）</span>
    <span><b>读者</b> 爬虫研发 · 数据运营</span>
    <span><b>配套</b> <code>schema_pg.sql</code> <code>refresh_pg.sql</code> <code>smoke_test_sqlite.py</code></span>
    <span><b>画板</b> <a href="%%FIGJAM%%">FigJam ER 图与流程图</a></span>
  </div>
</header>

<h2 id="verdict">结论</h2>
<dl class="verdict">
  <div><dt>核心单位</dt><dd>「环评审批事项」<code>eia_case</code>：一个项目的一次环评文件审批。不是网站，也不是项目名；重新报批就是新事项。</dd></div>
  <div><dt>范围</dt><dd>批复日 ≥ 2021-01-01、已批准、非辐射类（含输变电等电磁类）、非告知承诺制。网站翻不到 2021 年的，回溯到最早可达日期。范围外的事项留在库里，但不交付、不计缺口。</dd></div>
  <div><dt>锚点</dt><dd>以批复为锚，在批复日前 6 个月内回溯报告。报告优先取审批阶段的：批复公告随附 → 拟审批公示随附（含受理和拟审批合并公示）→ 受理公示版兜底；同一阶段内报批稿优先。用了受理版的，要记下本事项的拟审批公示是否已经抓到；没抓到的去补抓，抓到更好的版本自动替换。</dd></div>
  <div><dt>来源</dt><dd>两份文件都必须来自官方网站：环保局网站、区县环保局在政府门户上的栏目、行政审批局 / 管委会 / 政务服务网站都算。网站登记来源等级并写明判定依据，文件地址还要落在官方域名内。网盘、第三方平台、建设单位全本公示一律不交付。</dd></div>
  <div><dt>两件套保证</dt><dd>写成数据库约束：<code>COMPLETE</code>（关联分 ≥ 0.9）和 <code>LINK_REVIEW</code>（0.6 – 0.9，带待复核标记）都交付，但都必须是两份已校验的官方文件加两条来源链；在范围内却没交付的，必须在 <code>case_gap</code> 登记原因。</dd></div>
  <div><dt>名字</dt><dd>名字不当键。关联走证据阶梯：同一行 → 项目代码 → 批复文号 → 受理号 → 建设单位加项目名 → 模糊。每条关联存分数和证据。</dd></div>
</dl>

<h2 id="decisions">业务口径（2026-09-15 确认）</h2>
<div class="tbl"><table>
<thead><tr><th>问题</th><th>确认结果</th><th>落到库里</th></tr></thead>
<tbody>
<tr><td>报告取哪个阶段</td><td>审批阶段的报告优先（批复公告随附优先），没有才用受理阶段的</td><td><code>dict_stage.report_rank</code>：APPROVAL 1 → PRE_APPROVAL / ACCEPT_PRE_APPROVAL 2 → ACCEPT 3；同阶段按 <code>dict_report_version</code>（报批稿 → 送审稿 → 公示稿）；兜底时记录 <code>report_upgrade_possible</code></td></tr>
<tr><td>建设单位报批前全本公示能不能用</td><td>不算；必须来自官方网站</td><td><code>BUILDER_FULLTEXT</code> 不作报告来源；<code>site.source_class</code> 和 <code>attachment.on_official_host</code> 双重过滤；触发器拒绝非官方文件</td></tr>
<tr><td>辐射类、告知承诺制、不予批准</td><td>不在范围</td><td><code>dict_scope_rule</code> 标记项目行 → <code>eia_case.scope</code> → 交付状态 <code>OUT_OF_SCOPE</code></td></tr>
<tr><td>关联分不够的要不要交付</td><td>需要，带标记</td><td>0.6 – 0.9 → <code>LINK_REVIEW</code>，<code>v_delivery_export.needs_review = true</code>，同时开复核任务</td></tr>
<tr><td>回溯到哪一年</td><td>2021-01-01 起；没有则到最早可追溯</td><td><code>app_config.scope_start_date</code>；批复类栏目回到 2021-01-01，报告类栏目回到 2020-07-01；翻不到记 <code>window_earliest</code> 和 <code>reached_site_limit</code></td></tr>
</tbody></table></div>

<h3>第二轮确认：来源等级与输变电</h3>
<div class="tbl"><table>
<thead><tr><th>问题</th><th>确认结果</th><th>影响</th></tr></thead>
<tbody>
<tr><td>区县生态环境局挂在区政府门户上的栏目，算不算「环保局官方网站」</td><td><span class="pill pill-yes">算</span><br><code>EEB_PORTAL_COLUMN</code></td><td>很多区县环保局没有独立域名，如上海各区生态环境局、苏州工业园区生态环境局</td></tr>
<tr><td>行政审批局、开发区管委会、政务服务网站，算不算</td><td><span class="pill pill-yes">算</span><br><code>OTHER_APPROVER_SITE</code></td><td>南京受理记录中这类部门约占 26%，这些事项留在可交付范围内。苏州现有 381 条批复公告里有 318 条来自这类网站。登记时要留下「官方」的依据：政府网站标识码或 ICP 备案主体；像常熟新材料产业园这种用 IP 地址访问的站点，要先核实主办单位</td></tr>
<tr><td>输变电工程算不算辐射类</td><td><span class="pill pill-yes">算</span><br><code>RAD_POWER_GRID</code></td><td>按分类管理名录「核与辐射」大类口径。南京项目名命中输变电关键词 363 条，这些事项不交付</td></tr>
</tbody></table></div>

<h2 id="facts"><span class="num">一</span>拆回基本面</h2>

<h3>1. 已经确认、绕不开的基本事实</h3>
<div class="tbl"><table>
<thead><tr><th>事实</th><th>依据</th></tr></thead>
<tbody>
<tr><td>报告和审批意见在不同时间、不同公告里发布，常在不同栏目，有时在不同网站。</td><td>需求表：浦东保税区「环评和审批不在一起，需要靠业务逻辑来合并」；苏州受理、拟审批、批准公告是三个栏目。</td></tr>
<tr><td>「这两份文件属于同一个项目」是推断，不是网站给出的事实，除非两个链接挂在同一行。</td><td>上海市局平台「项目名称链接里是报告、批文内容里是审批」属于少数的同页情形。</td></tr>
<tr><td>地方网站几乎不公布跨阶段的统一编号。</td><td>苏州已抓公告正文里出现投资项目统一代码：批复类 <span class="mono">23 / 381</span>（6.0%），受理类 <span class="mono">1 / 279</span>。</td></tr>
<tr><td>同一事项在不同阶段的名称会变：套上「关于……的批复」、公司简称、全半角括号。</td><td>苏州现有按名称归并：<span class="mono">1,145</span> 个项目里两份文件都有的只有 <span class="mono">102</span> 个（8.9%）。</td></tr>
<tr><td>报告公示到批复公告通常只隔两三周。</td><td>苏州这 102 个项目：中位 <span class="mono">14</span> 天，最长 <span class="mono">75</span> 天。</td></tr>
<tr><td>审批阶段的公告很少附报告，多数事项只能用受理版兜底。</td><td>苏州范围内有报告附件的公告：批复公告 <span class="mono">4 / 323</span>，拟审批公示 <span class="mono">3 / 249</span>，受理公示 <span class="mono">260 / 263</span>。按现有匹配结果，范围内已批复、找到报告的 <span class="mono">101</span> 个项目里，报告来自审批阶段的只有 <span class="mono">5</span> 个。批复公告附的报告也可能和受理版是同一个文件（常熟高新区林森项目两次附的都是同名「公示稿」），要用内容哈希区分。</td></tr>
<tr><td>阶段有时合并发布；报告版本常写在附件名里。</td><td>苏州有 <span class="mono">30</span> 条「受理和拟审批合并公示」；受理阶段的报告附件中 <span class="mono">21</span> 份写着「报批稿」（其中 7 份在合并公示里），<span class="mono">7</span> 份写着「送审稿」。</td></tr>
<tr><td>很多事项在受理阶段没有附件。</td><td>南京 <span class="mono">7,070</span> 条受理记录中 <span class="mono">3,850</span> 条附件数为 0（54.5%）。</td></tr>
<tr><td>相当一部分环评不是由环保部门受理或审批。</td><td>南京受理记录：行政审批局 21.9%、管委会 3.4%、政务服务 0.5%。</td></tr>
<tr><td>批复文号只在作出决定之后出现，但一旦出现，在同一机关内唯一。</td><td>南京记录中 <span class="mono">4,663</span> 条带文号，形如 <code>宁环(高)建〔2026〕39号</code>；苏州项目行 <span class="mono">429 / 1,589</span> 抽到文号。</td></tr>
<tr><td>发文机关不等于网站主办方。</td><td>苏州市生态环境局网站统一发布太仓、昆山、张家港等分局；相城区网站发布相城经开区的环评。</td></tr>
<tr><td>列表能翻到的历史深度有限，而且各栏目不同。</td><td>苏州市局：受理公示只到 2024-10，批准公告只到 2021-08。</td></tr>
<tr><td>附件形态杂，链接会失效，公告会撤。</td><td>pdf / doc / wps / zip / rar / 图片 / 正文 / 网盘都出现过；浦东保税区外「有很大一部分链接无效」；松江入口一度为空。</td></tr>
<tr><td>环境影响登记表实行备案，没有审批意见。</td><td>《环境影响评价法》对登记表实行备案管理，因此登记表项目不在「两件套」范围内。</td></tr>
</tbody></table></div>

<h3>2. 习惯性接受、却没有验证过的假设</h3>
<ul>
  <li><strong>「一个网站 = 一个爬虫脚本 = 需求表一行」是合适的管理单位。</strong>上海市局和浦东（保税区外）用的是同一套 <code>hpxm_list_login.jsp</code> 系统；苏州 18 个栏目只用了 3 个 adapter。真正的单位是「栏目入口」，代码按 CMS 家族复用。</li>
  <li><strong>「项目名能把两份文件对上」。</strong>上面第 3、4 条事实已经说明名字不是标识。</li>
  <li><strong>「审批公示阶段一定挂着报告」。</strong>苏州的记录是拟审批公示「一般无报告，仅项目信息」。用受理版兜底会是常态，不是例外。</li>
  <li><strong>「两份都有才入库」能保证质量。</strong>它只会把缺口藏起来；第二份文件晚几天出现时，前一份已经被丢掉，再也对不上。</li>
  <li><strong>「匹配率低 = 匹配算法差」。</strong>苏州 <span class="mono">532</span> 个「有批复没报告」里，只有 <span class="mono">68</span> 个能用「报告栏目的列表翻不到那么早」解释。剩下约 <span class="mono">430</span> 个是没对上、报告在未登记的栏目，还是根本没公开，现在的库分不出来。</li>
  <li><strong>「全国 = 把所有环保局网站爬一遍」。</strong>审批权在市局、分局、行政审批局、开发区管委会之间的分布各地不同，很多由上级或政务服务平台统一发布。需要爬的入口数量是未知数，要先普查。</li>
  <li><strong>「URL 去重就够了」。</strong>同一份报告会在受理、拟审批两处以不同 URL 出现，也会被市局和区县站点镜像，要按文件内容哈希去重。</li>
</ul>

<h3>3. 真正想实现的目标</h3>
<p>对每一个<strong>在范围内、已批准</strong>的环评审批事项，交付一对<strong>来自官方网站</strong>的文件：最优可得的报告（批复公告随附 &gt; 拟审批公示随附 &gt; 受理公示版）加上审批意见；每份文件带完整来源链，两份文件之间的关联有置信度，置信度不足的带标记交付；拿不到的有原因、有证据、有复查计划；覆盖率能按机关、按月份算出来。</p>
<p class="note">不是目标：「每个项目都必须有两份文件」。对没有公开、或只在网盘和第三方平台出现的事项，这做不到，也不该假装做到。数据库能保证的是：<strong>交付的一定是两份官方文件，没交付的一定知道为什么。</strong></p>

<h3>4. 现实中的资源与约束</h3>
<ul>
  <li><strong>人力：</strong>需求表显示上海 15 个入口大约 3 周完成（6/30 – 7/17），基本一人一线。按站点线性写脚本扩到全国不可行。</li>
  <li><strong>已有资产：</strong>苏州爬虫（<code>sources.yaml</code> 登记 + <code>trs</code> / <code>paged</code> / <code>sipac</code> 三个 adapter + 标题分类规则 + SQLite）、上海 15 个 scraper、OSS、南京全量受理明细（含项目代码、文号，只作线索）。</li>
  <li><strong>来源：</strong>文件只认官方网站（环保局、审批机关、政府门户栏目）；第三方平台和网盘只能帮忙找线索。</li>
  <li><strong>站点：</strong>限速、需登录、验证码、JS 渲染、列表深度限制、改版。需要登录或验证码的入口不绕过。</li>
  <li><strong>数据：</strong>报告书文件常见几十 MB，格式杂，图片型批复需要 OCR；公告会被撤下。</li>
  <li><strong>时间：</strong>每天都有新批复；历史从 2021-01-01 回溯。</li>
</ul>

<h2 id="patches"><span class="num">二</span>原方案里只在修补表面的部分</h2>
<div class="tbl"><table>
<thead><tr><th>现在的做法</th><th>在修补什么</th><th>根上的问题</th></tr></thead>
<tbody>
<tr><td>入口只记在需求表的站点行（入口 / 入口URL / Python文件）</td><td>表格加列</td><td>来源停在站点层，单个文件追不到是哪个栏目、哪次抓取拿到的；也分不出是不是官方网站</td></tr>
<tr><td>「两文档都有才入库」（闵行、金山、奉贤）</td><td>每个 scraper 自己过滤</td><td>缺口不可见，各站口径不一，后到的文件无法补配</td></tr>
<tr><td>上传 OSS 列写「1150/11」「3120/2468」</td><td>人工记数</td><td>数字没有定义，无法按机关、按月份算覆盖率</td></tr>
<tr><td><code>name_key</code> + 相似度阈值 0.85 / 0.92</td><td>调阈值</td><td>不记证据，没有复核回路：调高了漏，调低了错</td></tr>
<tr><td>备注「各别文档有炸」「待检查」「链接无效」</td><td>自由文本</td><td>文件状态没有状态机，调度器无法据此重试</td></tr>
<tr><td>输出目录按项目名分文件夹，文件名里写阶段</td><td>用名字当身份</td><td>改名即断链，同一文件多份拷贝</td></tr>
<tr><td><code>projects</code> 表用逗号串存 <code>notice_ids</code>，只存选中的一份报告</td><td>结果表覆盖写</td><td>候选和选择理由丢失，规则一改无法重算</td></tr>
</tbody></table></div>

<h2 id="path"><span class="num">三</span>从基本事实重新推导的路径</h2>
<h3>推导链</h3>
<div class="tbl"><table>
<thead><tr><th>从这些事实和口径出发</th><th>推出的设计</th></tr></thead>
<tbody>
<tr><td>关联是推断；没有统一编号；名字会变</td><td>观察和推断分开存。推断带分数、证据、规则版本，可以整批删掉重算，人工结论保留。</td></tr>
<tr><td>发文机关 ≠ 网站；市局栏目替区县发布</td><td>登记层拆成 <code>authority</code> / <code>site</code> / <code>entry</code> / <code>entry_scope</code> 四张表。</td></tr>
<tr><td>文件必须来自官方网站；审批机关不一定是环保局，同一机关还会用门户栏目、文件服务器、网盘发布；有的官方网站不是 gov.cn 域名</td><td>网站登记来源等级 <code>source_class</code>，附件记录 <code>on_official_host</code>；选文件和触发器都按这两项过滤。</td></tr>
<tr><td>范围由业务口径决定，口径还可能调整</td><td>范围写成可重算的 <code>scope</code> 字段和规则字典；范围外事项保留，不交付。</td></tr>
<tr><td>很多事项没附件；列表有深度限制；链接会失效</td><td>「两份都有」不在抓取时过滤，而是交付层的表约束；没交付的写进 <code>case_gap</code>。</td></tr>
<tr><td>文号只在决定后出现且机关内唯一；目标以「已批准事项」为分母</td><td>以批复为锚，回溯找报告；（机关, 文号）建唯一索引。</td></tr>
<tr><td>同一文件多处出现</td><td>文件按 sha256 存一份（<code>file_blob</code>），页面上的链接各记一条（<code>attachment</code>）。</td></tr>
<tr><td>人力有限</td><td>代码按 CMS 家族写 adapter；加栏目是登记一行 <code>entry</code>，不是写一个新脚本。</td></tr>
</tbody></table></div>

<h3>四层模型</h3>
<p>数据只朝一个方向流：登记决定去哪抓，观察记录抓到了什么，推断决定谁和谁是一件事，交付兑现「两件套」合同。</p>
<div class="layers">
  <div class="layer"><span class="chip chip-r">登记 · 去哪里爬</span><p>机关、网站及来源等级、栏目入口，以及栏目替哪些机关发布。</p><div class="tables">region<br>authority<br>site<br>entry<br>entry_scope</div></div>
  <div class="layer"><span class="chip chip-o">观察 · 看到了什么</span><p>只追加。重复看到只刷新 <code>last_seen_at</code>。</p><div class="tables">crawl_run<br>notice<br>notice_item<br>attachment<br>file_blob</div></div>
  <div class="layer"><span class="chip chip-i">推断 · 怎么关联</span><p>每个判断带分数和证据；范围可重算。</p><div class="tables">eia_case<br>case_item_link<br>case_document</div></div>
  <div class="layer"><span class="chip chip-d">交付 · 两件套合同</span><p>交付的由约束保证，缺的必须有原因。</p><div class="tables">case_delivery<br>case_gap<br>review_task</div></div>
</div>
<p class="muted">配置与字典：<code>app_config</code> · <code>dict_stage</code> · <code>dict_source_class</code> · <code>dict_scope_rule</code> · <code>dict_gap_reason</code>。</p>
<p>每份交付文件的来源链：</p>
<div class="chain">file_blob.sha256 ← attachment.url（on_official_host）← notice.url（stage）← entry.entry_code ← site.root_url（source_class）← authority.authority_code</div>

<h3>关系图</h3>
<p class="muted">带字段说明的完整版在 <a href="%%FIGJAM%%">FigJam 画板</a>，按模板配色：黄 = 登记与交付，蓝 = 观察，紫 = 推断。</p>
<div class="diagram"><pre class="mermaid">
erDiagram
  region ||--o{ authority : "区划"
  authority ||--o{ site : "主办"
  dict_source_class ||--o{ site : "来源等级"
  site ||--o{ entry : "栏目"
  entry ||--o{ entry_scope : "发布范围"
  authority ||--o{ entry_scope : "被发布"
  entry ||--o{ crawl_run : "批次"
  entry ||--o{ notice : "首次发现"
  crawl_run ||--o{ notice : "抓到"
  authority ||--o{ notice : "发文"
  notice ||--o{ notice_item : "项目行"
  dict_scope_rule ||--o{ notice_item : "范围判定"
  notice ||--o{ attachment : "链接"
  notice_item |o--o{ attachment : "所属行"
  file_blob ||--o{ attachment : "内容"
  authority ||--o{ eia_case : "审批"
  notice_item ||--o{ case_item_link : "候选"
  eia_case ||--o{ case_item_link : "证据"
  eia_case ||--o{ case_document : "候选文件"
  attachment ||--o{ case_document : "被选"
  dict_stage ||--o{ case_document : "排序"
  eia_case ||--|| case_delivery : "交付"
  eia_case ||--o{ case_gap : "缺口"
  dict_gap_reason ||--o{ case_gap : "原因"
  eia_case ||--o{ review_task : "复核"
</pre></div>

<h3 id="scope">范围与来源规则</h3>
<p><strong>范围内</strong> = 已作出批准决定 + 批复日 ≥ 2021-01-01 + 非辐射类 + 非告知承诺制。报告书、报告表都在内；登记表是备案，不在内。<code>refresh_pg.sql</code> 每次先按项目行标记重算 <code>eia_case.scope</code>，口径调整后重跑即可。</p>
<div class="tbl"><table>
<thead><tr><th>rule_code</th><th>匹配</th><th>模式</th><th>结果</th><th class="n">现有数据命中</th></tr></thead>
<tbody>
<tr><td class="f">RAD_NUCLEAR</td><td>项目名</td><td><code>核技术利用|放射性|射线装置|同位素|加速器|伽马刀|DSA|PET</code></td><td class="f">OUT_RADIATION</td><td class="n">南京 277</td></tr>
<tr><td class="f">RAD_POWER_GRID</td><td>项目名</td><td><code>输变电|变电站|换流站|输电线路|开关站</code></td><td class="f">OUT_RADIATION</td><td class="n">南京 363 · 苏州项目行 14</td></tr>
<tr><td class="f">RAD_EM_OTHER</td><td>项目名</td><td><code>广播电视发射|通信基站|雷达站|卫星地球站</code></td><td class="f">OUT_RADIATION</td><td class="n">南京 30</td></tr>
<tr><td class="f">RAD_ENTRY · RAD_TITLE</td><td>栏目名 · 标题</td><td><code>辐射</code></td><td class="f">OUT_RADIATION</td><td class="n">苏州标题 33</td></tr>
<tr><td class="f">COMMITMENT</td><td>标题</td><td><code>告知承诺|承诺制</code></td><td class="f">OUT_COMMITMENT</td><td class="n">苏州标题 42</td></tr>
<tr><td class="f">REJECTED</td><td>标题</td><td><code>不予批准|不予审批|不予许可|予以退回</code></td><td class="f">OUT_REJECTED</td><td class="n">0</td></tr>
</tbody></table></div>
<p class="muted">南京的命中数按项目名加类别统计，规则之间有重叠，只用来估量级。专门的辐射栏目、告知承诺栏目在 <code>entry.scope_hint</code> 标出后直接不抓。</p>

<p><strong>来源等级。</strong>两道过滤同时满足才可交付：网站等级可交付，且文件地址的域名属于该网站（<code>root_url</code> 或登记的 <code>file_hosts</code>）。官方公告里贴的网盘链接也不交付。登记网站时在 <code>official_evidence</code> 写明判定依据（政府网站标识码、ICP 备案主体或上级政府网站的入口链接），<code>gov_site_code</code> 有则填；写不出依据的按第三方处理，表约束会拒绝没有依据的官方等级。</p>
<div class="tbl"><table>
<thead><tr><th>source_class</th><th>含义</th><th>可交付</th><th>例</th></tr></thead>
<tbody>
<tr><td class="f">EEB_OWN_SITE</td><td>生态环境部门自有网站</td><td><span class="pill pill-yes">是</span></td><td>苏州市生态环境局 sthjj.suzhou.gov.cn</td></tr>
<tr><td class="f">EEB_PORTAL_COLUMN</td><td>生态环境部门在政府门户上的官方栏目</td><td><span class="pill pill-yes">是</span></td><td>苏州工业园区生态环境局栏目、上海各区生态环境局栏目</td></tr>
<tr><td class="f">OTHER_APPROVER_SITE</td><td>行政审批局、管委会、政务服务的网站或门户公示栏目</td><td><span class="pill pill-yes">是</span></td><td>昆山市政府「公示公告」、常熟高新区「通知公告」</td></tr>
<tr><td class="f">THIRD_PARTY</td><td>第三方平台、建设单位、网盘</td><td><span class="pill pill-no">否</span></td><td>百度网盘链接、聚合平台</td></tr>
</tbody></table></div>

<p><strong>历史回溯。</strong></p>
<div class="tbl"><table>
<thead><tr><th>栏目</th><th>回溯到</th><th>依据</th></tr></thead>
<tbody>
<tr><td>批复类栏目（<code>stage_hint = APPROVAL</code>）</td><td class="f">2021-01-01</td><td>范围起点</td></tr>
<tr><td>受理、拟审批、混合栏目</td><td class="f">2020-07-01</td><td>起点再往前 6 个月：苏州 102 对报告→批复间隔中位 14 天、最长 75 天</td></tr>
<tr><td>网站翻不到目标日期</td><td>最早可达日期</td><td>记 <code>entry.window_earliest</code>、<code>backfill_status = reached_site_limit</code>；受影响事项缺口记 <code>OUT_OF_WINDOW</code></td></tr>
</tbody></table></div>
<p class="muted"><code>v_entry_backfill</code> 列出每个栏目的目标日期和是否已经够到。</p>

<h3 id="status">交付状态</h3>
<div class="tbl"><table>
<thead><tr><th>delivery_status</th><th>含义</th><th>交付</th><th>缺口 / 任务</th></tr></thead>
<tbody>
<tr><td class="f">COMPLETE</td><td>两份已校验的官方文件，关联分 ≥ 0.9</td><td><span class="pill pill-yes">交付</span></td><td>—</td></tr>
<tr><td class="f">LINK_REVIEW</td><td>两份已校验的官方文件，关联分 0.6 – 0.9</td><td><span class="pill pill-yes">交付 · needs_review</span></td><td>复核任务 <code>LINK_LOW_CONF</code>；确认后改 MANUAL 1.0，下次 refresh 升为 COMPLETE</td></tr>
<tr><td class="f">NO_LINK_EVIDENCE</td><td>选中了两份文件，但没有已接受的关联证据</td><td><span class="pill pill-no">不交付</span></td><td>复核任务 <code>LINK_MISSING</code></td></tr>
<tr><td class="f">MISSING_REPORT · MISSING_APPROVAL · MISSING_BOTH</td><td>缺一份或两份</td><td><span class="pill pill-no">不交付</span></td><td><code>case_gap</code>，原因按下文顺序自动判定</td></tr>
<tr><td class="f">PENDING_DECISION</td><td>还没作出审批决定</td><td><span class="pill pill-no">不交付</span></td><td>审批意见缺口 <code>PENDING_DECISION</code></td></tr>
<tr><td class="f">OUT_OF_SCOPE</td><td>辐射、告知承诺、不予批准或 2021 年以前</td><td><span class="pill pill-no">不交付</span></td><td>不登记</td></tr>
</tbody></table></div>
<p>缺口原因的自动判定顺序：待决定 → <code>FILE_NOT_VERIFIED</code>（官方网站上有候选，但下载或校验没通过）→ <code>NOT_OFFICIAL_SOURCE</code>（只在非官方渠道找到）→ <code>NOT_YET_CRAWLED</code>。<code>OUT_OF_WINDOW</code>、<code>NOT_PUBLISHED</code> 由回溯任务写入，refresh 不会覆盖。</p>

<h3 id="fields">字段字典</h3>
<p class="muted">由 <code>schema_pg.sql</code> 自动生成。PK 主键 · FK 外键 · UK 唯一 · 必填 = NOT NULL。</p>
%%FIELDS%%

<h3 id="linking">名字怎么关联</h3>
<p><strong>规范化（norm_version = v1）。</strong>原文一律保留在 <code>*_raw</code>，规范化结果另存并记版本，规则升级后整批重算。</p>
<div class="tbl"><table>
<thead><tr><th>对象</th><th>规则</th><th>例</th></tr></thead>
<tbody>
<tr><td class="f">project_name</td><td>NFKC 全角转半角；去空白和《》“”；剥掉「关于（对）……的批复 / 审批意见」外壳；去掉「环境影响报告书 / 表」「公示版」「征求意见稿」等后缀；<strong>数字和规模保留</strong>（年产 3 万吨和 5 万吨是两个项目）</td><td>关于对某某公司年产3万吨XX项目环境影响报告表的批复 → 某某公司年产3万吨XX项目</td></tr>
<tr><td class="f">builder</td><td>NFKC；去空白；括号统一；保留全称。另算 core（去地区前缀、去「有限公司」等后缀），只用于模糊匹配</td><td>苏州伟亿祥塑胶有限公司 → core：伟亿祥塑胶</td></tr>
<tr><td class="f">doc_no</td><td>〔〕【】（）[] 统一成 []；去「第」和空白</td><td>苏环建〔2026〕85号 → 苏环建[2026]85号</td></tr>
<tr><td class="f">project_code</td><td>必须匹配 <code>\d{4}-\d{6}-\d{2}-\d{2}-\d{6}</code>，不符合就不填</td><td>2509-320193-89-01-544060</td></tr>
<tr><td class="f">authority</td><td>按 <code>authority.aliases</code> 精确映射；映射不上开 <code>AUTHORITY_UNRESOLVED</code> 复核，不猜</td><td>市生态环境局（苏州站）→ 320500-SZSTHJJ</td></tr>
</tbody></table></div>

<p><strong>关联键阶梯。</strong>从强到弱尝试，命中即停；否决项优先于任何分数。</p>
<div class="tbl"><table>
<thead><tr><th>顺序</th><th>match_key</th><th>成立条件</th><th class="n">score</th></tr></thead>
<tbody>
<tr><td>1</td><td class="f">SAME_ROW</td><td>报告链接和审批意见链接挂在同一个项目行</td><td class="n">1.00</td></tr>
<tr><td>2</td><td class="f">PROJECT_CODE</td><td>投资项目统一代码相同，报告书 / 报告表不冲突；同一代码对应多个事项时降为 0.80</td><td class="n">0.98</td></tr>
<tr><td>3</td><td class="f">DOC_NO</td><td>规范化批复文号相同（批复公告行 ↔ 批复文件）</td><td class="n">0.98</td></tr>
<tr><td>4</td><td class="f">ACCEPT_NO</td><td>受理 / 办件编号相同</td><td class="n">0.95</td></tr>
<tr><td>5</td><td class="f">BUILDER_NAME_EXACT</td><td>规范化建设单位 + 规范化项目名都相同，且在同一机关发布范围内</td><td class="n">0.92</td></tr>
<tr><td>6</td><td class="f">NAME_FUZZY</td><td>同机关、批复日前 6 个月内名称相似；带「待复核」标记交付，同时开复核任务</td><td class="n">0.60 – 0.89</td></tr>
<tr><td>否决</td><td class="f">任意</td><td>机关不在同一发布范围；日期倒序（批复早于受理）；报告书与报告表冲突；该批复已被更高分的事项占用</td><td class="n">0</td></tr>
</tbody></table></div>
<p>低于 0.6 的关联不能接受（表约束）。两个门槛 0.9 / 0.6 同时写在表约束里（下限）和 <code>app_config</code> 里（可以调得更严）。时间窗：受理日 ≤ 拟审批日 ≤ 批复日；报告候选只在批复日前 6 个月内找（<code>report_lookback_months</code>）。</p>

<p><strong>文件取用。</strong>所有候选先过三道门：已校验、网站来源等级可交付、文件地址在官方域名内。</p>
<div class="tbl"><table>
<thead><tr><th>角色</th><th>顺序</th><th>来源</th><th>说明</th></tr></thead>
<tbody>
<tr><td>报告</td><td>1</td><td class="f">APPROVAL</td><td>批复公告随附的报告；和受理版内容哈希相同时说明是同一份文件，来源仍记批复公告</td></tr>
<tr><td>报告</td><td>2</td><td class="f">PRE_APPROVAL · ACCEPT_PRE_APPROVAL</td><td>拟审批公示随附的报告；「受理和拟审批合并公示」也算这一档，不算兜底</td></tr>
<tr><td>报告</td><td>3</td><td class="f">ACCEPT</td><td>兜底；交付行 <code>report_is_fallback = true</code>。本事项的拟审批公示还没抓到、而该机关发过拟审批公示时，再标 <code>report_upgrade_possible = true</code>，回溯任务优先去补抓；以后抓到审批阶段的报告，下次 refresh 自动替换</td></tr>
<tr><td>报告</td><td>不取</td><td class="f">BUILDER_FULLTEXT</td><td>已确认不取：建设单位报批前全本公示不是官方网站发布的</td></tr>
<tr><td>审批意见</td><td>—</td><td class="f">APPROVAL</td><td>批复文件；批复是正文页面时，把正文转成 PDF 作为 <code>body_html</code> 附件；只有名单、文号没有文件的记 <code>LISTED_ONLY</code> 缺口</td></tr>
<tr><td>同阶段内</td><td>—</td><td>—</td><td>版本（报批稿 → 送审稿 → 公示稿 / 征求意见稿 → 未标版本）→ 发布日期新 → 有 PDF → 页数多；过不了三道门的文件不参与，自动落到下一顺位</td></tr>
</tbody></table></div>

<h3 id="flow">入库与配对流程</h3>
<p class="muted">流程图版本在 FigJam 画板 ER 图下方。</p>
<ol class="steps">
  <li><div><strong>登记入口。</strong>需求表每个栏目一行 <code>entry</code>，填 <code>adapter</code>、<code>stage_hint</code>；网站填 <code>source_class</code> 和 <code>official_evidence</code>；辐射、告知承诺专栏填 <code>scope_hint</code>（不抓）；在 <code>entry_scope</code> 写明替哪些机关发布。</div></li>
  <li><div><strong>列表抓取。</strong>按 <code>list_cursor</code> 增量翻页；历史回溯到目标日期（批复类 2021-01-01，其余 2020-07-01），翻不到就停在最早可达日期，记下 <code>window_earliest</code>。每次运行写一条 <code>crawl_run</code>。</div></li>
  <li><div><strong>公告落库。</strong>规范化 URL 后 upsert <code>notice</code>，刷新 <code>last_seen_at</code>，保存原始快照；判定是否环评公告和所属阶段（受理和拟审批合并公示单独标出），写明依据。</div></li>
  <li><div><strong>抽取项目行。</strong>一条公告拆成 N 个 <code>notice_item</code>；项目名、建设单位、项目代码、文号的原文和规范化值各存一份；按 <code>dict_scope_rule</code> 标出 <code>is_radiation</code>、<code>approval_mode</code>、<code>decision</code>。</div></li>
  <li><div><strong>发现附件。</strong>每个链接一条 <code>attachment</code>，判定 <code>doc_role</code>，按附件名识别报告版本 <code>version_label</code>，记下 <code>url_host</code> 并判定 <code>on_official_host</code>（网盘恒为否）；压缩包成员用 <code>parent_att_id</code> 挂回压缩包。</div></li>
  <li><div><strong>下载与校验。</strong>算 sha256 写 <code>file_blob</code>（已存在就只挂链接），转 PDF，校验能打开、页数大于 0、不是错误页，通过后 <code>verified = true</code>。</div></li>
  <li><div><strong>关联。</strong>按关联键阶梯为每个项目行找事项，写 <code>case_item_link</code>。≥ 0.9 且无否决项直接接受；0.6 – 0.9 也接受，交付时带标记；没有候选或低于 0.6 就新建 <code>eia_case</code>（SEED）。</div></li>
  <li><div><strong>登记候选文件。</strong>事项下所有报告和审批意见候选写进 <code>case_document</code>，不管是不是官方来源，便于缺口归因。</div></li>
  <li><div><strong>运行 <code>refresh_pg.sql</code>。</strong>汇总标记、重算 <code>scope</code> → 选文件（过三道门，报告按阶段、版本排序）→ upsert <code>case_delivery</code>（约束和触发器把关）→ 更新 <code>case_gap</code> → 开复核任务。脚本幂等。</div></li>
  <li><div><strong>回溯缺口和可升级的报告。</strong>对 <code>report_upgrade_possible = true</code> 的事项，先去补抓它的拟审批公示；对缺报告的已批准事项，在同机关 <code>entry_scope</code> 内查批复日前 6 个月的受理、拟审批公告（含站内搜索），回到第 2 步；查遍官方入口仍没有，把缺口原因改成 <code>NOT_PUBLISHED</code> 或 <code>OUT_OF_WINDOW</code> 并附证据。</div></li>
  <li><div><strong>交付。</strong>下游只读 <code>v_delivery_export</code>，用 <code>needs_review</code> 区分带标记的行；覆盖率看 <code>v_coverage_by_authority_month</code>（分母 = 范围内已决定事项）；回溯进度看 <code>v_entry_backfill</code>。</div></li>
</ol>

<h3 id="naming">命名规范</h3>
<div class="tbl"><table>
<thead><tr><th>对象</th><th>格式</th><th>例</th></tr></thead>
<tbody>
<tr><td class="f">authority_code</td><td>{行政区划码}-{机关简拼}</td><td class="f">320500-SZSTHJJ</td></tr>
<tr><td class="f">entry_code</td><td>{行政区划码}-{站点简拼}-{阶段}-{两位序号}</td><td class="f">310000-SHSTHJ-APPROVAL-01</td></tr>
<tr><td class="f">case_code</td><td>EIA-{行政区划码}-{首次发现年}-{六位流水}</td><td class="f">EIA-320500-2026-000123</td></tr>
<tr><td>原始文件</td><td>oss://{bucket}/eia/blob/{sha256 前 2 位}/{sha256}.{ext}</td><td class="f">…/eia/blob/ab/ab12….pdf</td></tr>
<tr><td>转换后的 PDF</td><td>oss://{bucket}/eia/pdf/{前 2 位}/{pdf_sha256}.pdf</td><td class="f">…/eia/pdf/7c/7c90….pdf</td></tr>
<tr><td>页面快照</td><td>oss://{bucket}/eia/raw/{entry_code}/{yyyy}/{mm}/{url 的 sha1}.html</td><td class="f">…/raw/320500-SZSTHJJ-ACCEPT-01/2026/09/…html</td></tr>
<tr><td>导出文件名</td><td>{case_code}_{报告|审批意见}_{阶段}_{发布日期}.pdf；带标记的行文件名前加「待复核_」；由视图生成，不回写、不解析</td><td class="f">EIA-320500-2026-000123_报告_PRE_APPROVAL_20260401.pdf</td></tr>
<tr><td>导出目录</td><td>{省}/{市}/{机关名}/{case_code}_{项目名前 40 字}/</td><td>江苏省/苏州市/苏州市生态环境局/EIA-320500-2026-000123_…/</td></tr>
</tbody></table></div>

<p><strong>飞书需求表怎么对到库里。</strong></p>
<div class="tbl"><table>
<thead><tr><th>需求表列</th><th>落到</th><th>说明</th></tr></thead>
<tbody>
<tr><td>网站名 / 主页URL</td><td class="f">site.name / site.root_url</td><td></td></tr>
<tr><td>来源等级（建议新增列）</td><td class="f">site.source_class</td><td>按上文四类填写并写明判定依据；第三方平台不交付</td></tr>
<tr><td>入口 / 入口URL</td><td class="f">entry.name / entry.list_url</td><td>一个栏目一行；同站多个栏目拆成多行</td></tr>
<tr><td>范围外专栏（建议新增列）</td><td class="f">entry.scope_hint</td><td>辐射、告知承诺专栏填上即不抓</td></tr>
<tr><td>Python文件</td><td class="f">entry.adapter</td><td>同一 CMS 家族共用一个</td></tr>
<tr><td>名称（如 sthj_shgov）</td><td class="f">entry_code</td><td>作为站点简拼部分</td></tr>
<tr><td>负责人 / 状态 / 最新爬取日期</td><td class="f">entry.owner / status / last_success_at</td><td></td></tr>
<tr><td>数据量级</td><td>由 <code>notice</code> 计数得出</td><td>不再手填</td></tr>
<tr><td>备注</td><td class="f">entry.note</td><td>文件问题改由 <code>attachment.fetch_status</code> 和 <code>review_task</code> 记录</td></tr>
<tr><td>上传oss（成功/失败）</td><td class="f">v_coverage_by_authority_month</td><td>分子 = COMPLETE + LINK_REVIEW，分母 = 范围内已决定事项</td></tr>
</tbody></table></div>

<h2 id="premises"><span class="num">四</span>这条路径成立的前提</h2>
<ol>
  <li><strong>批复在官方网站上可得</strong>（环保局、审批机关或政府门户栏目），文件或正文均可。只在网盘或第三方平台出现的，进缺口。</li>
  <li><strong>报告和批复能落在同一机关的发布范围内</strong>，而且 <code>entry_scope</code> 维护得起；审批权划转、机构改革有记录（<code>valid_from / valid_to</code>）。</li>
  <li><strong>强键出现率够高，或「建设单位 + 项目名」规范化后精确相等的比例够高</strong>，使得带标记交付的比例和复核量是人能处理的。这需要用标注样本测，现在不知道。</li>
  <li><strong>站点能按 CMS 家族复用 adapter。</strong>如果全国大多数入口都是定制系统，成本仍随站点数线性增长，需要改用站内搜索作为主入口。</li>
  <li><strong>「官方网站」能被核实。</strong>管委会、产业园的网站不一定是 gov.cn 域名，甚至用 IP 地址访问；登记时要能找到政府网站标识码、ICP 备案主体或上级政府网站的入口链接，找不到的按第三方处理。</li>
  <li><strong>范围规则的误判可控。</strong>辐射、告知承诺靠关键词和栏目判定，会有漏判和误判；需要在样本上核对，必要时加人工复核。</li>
  <li><strong>合规：</strong>只抓公开页面，遵守限速；需要登录或验证码的入口不绕过；第三方聚合数据（如南京明细的来源平台）只作线索，使用前确认授权。</li>
  <li><strong>存储预算：</strong>按内容哈希去重后，仍需估算 2021 年以来全国报告书的体积和 OCR 成本。</li>
</ol>

<h2 id="verify"><span class="num">五</span>验证它的第一步</h2>
<h3>已经做完的：合同逻辑冒烟测试</h3>
<p>用 SQLite 执行 <code>schema_pg.sql</code> 和 <code>refresh_pg.sql</code>，合成数据（非真实项目），跑两遍验证幂等。18 个场景和 15 种非法写入全部符合预期。</p>
<div class="tbl"><table>
<thead><tr><th>场景</th><th>期望</th><th>结果</th></tr></thead>
<tbody>
<tr><td>受理版 + 拟审批版 + 批复</td><td>COMPLETE，报告取拟审批版</td><td class="pass">通过</td></tr>
<tr><td>合并公示里的报批稿 + 单独受理公示里的公示文本</td><td>COMPLETE，报告取合并公示版，不算兜底</td><td class="pass">通过</td></tr>
<tr><td>受理阶段两份报告：较新的公示文本、较早的报批稿</td><td>取报批稿</td><td class="pass">通过</td></tr>
<tr><td>只有受理版，但拟审批公示已抓到且没附报告（昆山唯斯达的情况）</td><td>COMPLETE，兜底，<code>report_upgrade_possible = false</code></td><td class="pass">通过</td></tr>
<tr><td>先交付了受理版，之后抓到批复公告里附的报告</td><td>重跑后自动换成批复公告版，不再兜底</td><td class="pass">通过</td></tr>
<tr><td>只有受理版 + 批复，拟审批公示没抓到</td><td>COMPLETE，标记兜底，<code>report_upgrade_possible = true</code></td><td class="pass">通过</td></tr>
<tr><td>拟审批版文件损坏</td><td>自动落到受理版，COMPLETE</td><td class="pass">通过</td></tr>
<tr><td>批复在区政府门户的环保局栏目</td><td>COMPLETE，来源等级 EEB_PORTAL_COLUMN</td><td class="pass">通过</td></tr>
<tr><td>名称模糊匹配（0.72）</td><td>LINK_REVIEW，进入交付视图且 needs_review，开 1 个复核任务</td><td class="pass">通过</td></tr>
<tr><td>只有批复</td><td>MISSING_REPORT，缺口 NOT_YET_CRAWLED</td><td class="pass">通过</td></tr>
<tr><td>报告在行政审批局网站</td><td>COMPLETE，来源等级 OTHER_APPROVER_SITE</td><td class="pass">通过</td></tr>
<tr><td>报告只在第三方平台</td><td>MISSING_REPORT，缺口 NOT_OFFICIAL_SOURCE</td><td class="pass">通过</td></tr>
<tr><td>报告只有网盘链接</td><td>MISSING_REPORT，缺口 NOT_OFFICIAL_SOURCE</td><td class="pass">通过</td></tr>
<tr><td>只受理、未决定</td><td>PENDING_DECISION</td><td class="pass">通过</td></tr>
<tr><td>辐射类 · 告知承诺 · 不予批准 · 2020 年批复</td><td>OUT_OF_SCOPE，各自原因正确，不登记缺口</td><td class="pass">通过</td></tr>
<tr><td>15 种非法写入：去掉来源链、换成未校验文件、同一文件充当两份、0.72 标完整、带标记但 0.5 分、缺报告标完整、换成第三方平台文件、指向网盘、范围外标交付、接受 0.5 分关联、网盘标官方域名、官方等级没写判定依据、一行归两个事项、选中两份报告、重复文号</td><td>全部被数据库拒绝</td><td class="pass">通过</td></tr>
</tbody></table></div>

<h3>下一步：苏州数据上的「批复锚定回溯」小实验</h3>
<p>用苏州已有的 <span class="mono">6,707</span> 条公告，1 到 2 天，检验关键前提：批复适合当锚点、证据阶梯比名称相似度可靠、缺口能被归因。</p>
<ol class="steps">
  <li><div>把现有 SQLite 迁进新结构：<code>notices → notice</code>，<code>project_rows → notice_item</code>，<code>attachments → attachment + file_blob</code>，<code>sources.yaml → site / entry / entry_scope</code>；给 18 个栏目标上 <code>source_class</code> 和 <code>scope_hint</code>。</div></li>
  <li><div>从 2021 年以后、范围内的批复里随机抽 100 个，人工上网确认：报告是否在官方网站公开、在哪个栏目、哪个阶段。这是真值表。</div></li>
  <li><div>跑关联键阶梯和 <code>refresh_pg.sql</code>，对照真值表算：COMPLETE 的精确率（目标 ≥ 98%）、LINK_REVIEW 里真正对的比例、召回率、每个缺口是否都能归到一个 <code>reason_code</code>（目标 100%）、<code>NOT_OFFICIAL_SOURCE</code> 占缺口的比例。</div></li>
  <li><div>按缺口原因分布决定下一步投入：多数是「报告公开了但在未登记栏目」→ 先做入口普查；多数是「名字没对上」→ 先做规范化和强键抽取；多数是「只在网盘或第三方平台」→ 去找该机关的其他官方入口。</div></li>
</ol>
<p class="muted">已知的起点：苏州现有 18 个栏目按确认后的口径都可以算官方网站，其中 3 个是辐射或告知承诺专栏、不再抓取；常熟新材料产业园栏目用 IP 地址访问，需要先核实主办单位。532 个有批复没报告的项目中，报告栏目翻不到「批复日前 30 天」的有 68 个。</p>

<footer>
  <ul>
    <li>本地文件：<code>docs/national_eia_db/schema_pg.sql</code> · <code>refresh_pg.sql</code> · <code>smoke_test_sqlite.py</code> · <code>build_doc.py</code></li>
    <li>画板：<a href="%%FIGJAM%%">FigJam · 江苏省环评文件查找逻辑框架图</a></li>
    <li>数据依据：飞书「爬虫需求管理」表（2026-09-15 读取）、苏州爬虫 SQLite、南京受理明细 CSV；业务口径 2026-09-15 确认</li>
  </ul>
</footer>
</main>
</div>
"""


def main() -> None:
    sql = (HERE / "schema_pg.sql").read_text(encoding="utf-8")
    page = TEMPLATE.replace("%%FIELDS%%", field_dictionary(sql)).replace("%%FIGJAM%%", FIGJAM)
    out = HERE / "design.html"
    out.write_text(page, encoding="utf-8")
    print(out, len(page), "chars")


if __name__ == "__main__":
    main()
