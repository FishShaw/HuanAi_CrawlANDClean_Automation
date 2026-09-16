"""统一转 PDF：PDF 校验、zip 解压、正文图片合并、doc/docx/wps/正文 HTML 用 LibreOffice 转换。"""
from __future__ import annotations

import hashlib
import io
import logging
import shutil
import sqlite3
import subprocess
import zipfile
from pathlib import Path

from pypdf import PdfReader

from . import classify, db

log = logging.getLogger("eia")

SOFFICE = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
OFFICE_EXT = {"doc", "docx", "wps", "rtf", "odt", "xls", "xlsx"}
BATCH = 15


def pdf_pages(path: Path) -> int:
    try:
        return len(PdfReader(str(path), strict=False).pages)
    except Exception:
        return 0


def _mark(c, att_id, status, pdf_path=None, pages=None, error=None):
    c.execute("UPDATE attachments SET status=?, pdf_path=?, pages=?, error=? WHERE id=?",
              (status, str(pdf_path) if pdf_path else None, pages, error, att_id))


def extract_archives(c: sqlite3.Connection, files_dir: Path) -> int:
    count = 0
    for att in c.execute("SELECT a.*, n.stage FROM attachments a JOIN notices n ON n.id=a.notice_id"
                         " WHERE a.status='downloaded' AND a.ext IN ('zip','rar','7z')").fetchall():
        if att["ext"] != "zip":
            _mark(c, att["id"], "unsupported", error=f"暂不支持 {att['ext']} 解压")
            continue
        try:
            with zipfile.ZipFile(att["path"]) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    if not info.flag_bits & 0x800:  # 非 UTF-8 标记的中文文件名通常是 GBK，被 zipfile 按 cp437 解码了
                        try:
                            name = name.encode("cp437").decode("gb18030")
                        except (UnicodeEncodeError, UnicodeDecodeError):
                            pass
                    base = name.rsplit("/", 1)[-1]
                    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
                    if ext not in OFFICE_EXT | {"pdf", "zip"}:
                        continue
                    data = z.read(info)
                    sha = hashlib.sha256(data).hexdigest()
                    dest = files_dir / sha[:2] / f"{sha}.{ext}"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        dest.write_bytes(data)
                    dt = classify.doc_type(base, att["stage"])
                    c.execute("INSERT OR IGNORE INTO attachments(notice_id,project_row_id,parent_id,kind,url,name,ext,doc_type,"
                              "status,sha256,path,size) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              (att["notice_id"], att["project_row_id"], att["id"], "member", f"{att['url']}#{name}", base,
                               ext, dt, "downloaded" if dt in ("report", "approval") else "skipped", sha, str(dest), len(data)))
            _mark(c, att["id"], "extracted")
            count += 1
        except Exception as e:  # 单个坏压缩包不能拖垮整批转换
            _mark(c, att["id"], "failed", error=f"解压失败：{e}")
    c.commit()
    return count


def images_to_pdf(paths: list[str], out: Path) -> None:
    import img2pdf
    from PIL import Image
    pages = []
    for p in paths:
        im = Image.open(p)
        if im.width < 300 or im.height < 300:
            continue
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90)
        pages.append(buf.getvalue())
    if not pages:
        raise ValueError("没有足够大的正文图片")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(img2pdf.convert(pages))


def soffice_batch(inputs: list[Path], outdir: Path, html: bool) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    profile = outdir.parent / "lo_profile"
    target = "pdf:writer_web_pdf_Export" if html else "pdf"
    cmd = [SOFFICE, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
           "--convert-to", target, "--outdir", str(outdir), *map(str, inputs)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300 + 90 * len(inputs), check=False)


def convert_pending(c: sqlite3.Connection, files_dir: Path, limit: int | None = None) -> dict:
    stats = {"archives": 0, "pdf": 0, "images": 0, "office": 0, "failed": 0}
    for _ in range(3):  # 压缩包里可能还有压缩包
        n = extract_archives(c, files_dir)
        stats["archives"] += n
        if not n:
            break
    pdf_dir = files_dir / "pdf"

    for att in c.execute("SELECT * FROM attachments WHERE status='downloaded' AND ext='pdf' AND kind!='images'"
                         + (f" LIMIT {int(limit)}" if limit else "")).fetchall():
        pages = pdf_pages(Path(att["path"]))
        if pages:
            _mark(c, att["id"], "converted", att["path"], pages)
            stats["pdf"] += 1
        else:
            _mark(c, att["id"], "failed", error="PDF 无法解析")
            stats["failed"] += 1
    c.commit()

    for att in c.execute("SELECT * FROM attachments WHERE status='downloaded' AND kind='images'").fetchall():
        out = pdf_dir / f"{att['id']}.pdf"
        try:
            images_to_pdf(db.loads(att["extra"]).get("paths", []), out)
            _mark(c, att["id"], "converted", out, pdf_pages(out))
            stats["images"] += 1
        except Exception as e:
            _mark(c, att["id"], "failed", error=f"图片合并失败：{e}")
            stats["failed"] += 1
    c.commit()

    pending = c.execute("SELECT * FROM attachments WHERE status='downloaded' AND (ext IN ({}) OR kind='body')"
                        .format(",".join(f"'{e}'" for e in OFFICE_EXT | {"html"}))
                        + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    if pending and not Path(SOFFICE).exists():
        log.error("[转换] 找不到 LibreOffice（soffice），%d 个 doc/docx/正文待转换", len(pending))
        return stats
    work = files_dir / "convert_work"
    for is_html in (False, True):
        group = [a for a in pending if (a["kind"] == "body" or a["ext"] == "html") == is_html]
        for i in range(0, len(group), BATCH):
            batch = group[i:i + BATCH]
            shutil.rmtree(work / "in", ignore_errors=True)
            shutil.rmtree(work / "out", ignore_errors=True)
            (work / "in").mkdir(parents=True, exist_ok=True)
            inputs = []
            for a in batch:
                ext = "html" if is_html else a["ext"]
                src = work / "in" / f"{a['id']}.{ext}"
                shutil.copyfile(a["path"], src)
                inputs.append(src)
            try:
                soffice_batch(inputs, work / "out", is_html)
            except subprocess.TimeoutExpired:
                log.warning("[转换] LibreOffice 超时，批次 %d 个文件", len(batch))
            for a in batch:
                produced = work / "out" / f"{a['id']}.pdf"
                pages = pdf_pages(produced) if produced.exists() else 0
                if pages:
                    final = pdf_dir / f"{a['id']}.pdf"
                    final.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(produced, final)
                    _mark(c, a["id"], "converted", final, pages)
                    stats["office"] += 1
                else:
                    _mark(c, a["id"], "failed", error="LibreOffice 转换失败")
                    stats["failed"] += 1
            c.commit()
            log.info("[转换] 已处理 %d/%d（%s）", min(i + BATCH, len(group)), len(group), "正文HTML" if is_html else "Office")
    return stats
