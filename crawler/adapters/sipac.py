"""苏州工业园区：公开页里嵌了带 ticket 的 iframe，列表、详情、附件都走 JSON 接口。
受理：getGongShiDataYQ → getGongShiDataYQById（附件用 getYuLanUrl 换成下载地址）
批准：getApprovalGongShiDataYQ → getLiuChengListById（批复 PDF 由 sealAddress 以 base64 返回）"""
from __future__ import annotations

import base64
import json
import re
import threading

from .. import db, extract
from ..http import FetchError, Fetcher

SERVICE = "https://zwyyone.sipac.gov.cn/siphbsl/shencai-siphbsl-web/service"
PAGES = "https://zwyyone.sipac.gov.cn/siphbsl/shencai-siphbsl-web/web/siphbsl/login/spsLogin"
FILE_BASE = "https://zwyyone.sipac.gov.cn/siphbsl/shencai-webupload/"
VIEW_METHOD = "/opt/apache-tomcat-8.5.73-3310/webapps/shencai-webupload/"
PREVIEW_TICKET = "414e8e5bc65746ffba5be0fea840c377"  # 详情页脚本里写死的预览 ticket
HOST = "zwyyone.sipac.gov.cn"

_tickets: dict[str, str] = {}
_lock = threading.Lock()


def _json(text: str) -> dict:
    return json.loads(text, strict=False)  # 返回内容里有未转义的控制字符


def _ticket(fetcher: Fetcher, portal_url: str, refresh: bool = False) -> str:
    with _lock:
        if refresh or portal_url not in _tickets:
            html, _ = fetcher.get_text(portal_url, save_raw=False)
            m = re.search(r"ticket=([0-9a-f]{16,})", html)
            if not m:
                raise FetchError(f"公开页里找不到 ticket：{portal_url}")
            _tickets[portal_url] = m.group(1)
        return _tickets[portal_url]


def _post(fetcher: Fetcher, api: str, data: dict) -> dict:
    resp = fetcher.request(f"{SERVICE}/{api}", method="POST", data=data, min_bytes=10)
    body = _json(resp.text)
    if body.get("status") != "1":
        raise FetchError(f"{api} 返回 status={body.get('status')}")
    return body.get("data") or {}


def _is_approval(src: dict) -> bool:
    return "Approval" in src["api"]


def _date(text: str | None) -> str | None:
    m = extract.DATE.search(text or "")
    return extract.fmt_date(m) if m else None


def iter_pages(src, fetcher, known, incremental, max_pages):
    ticket = _ticket(fetcher, src["portal_url"])
    page_name = "report2.html" if _is_approval(src) else "report.html"
    page = 1
    while True:
        data = _post(fetcher, f"lzhy/lzhyController/{src['api']}",
                     {"ticket": ticket, "pagingParams": json.dumps({"pageSize": 20, "pageIndex": page})})
        items = [extract.ListItem(url=f"{PAGES}/{page_name}?flowInstId={it['flowInstId']}", title=it.get("title") or "",
                                  date=_date(it.get("createTime")),
                                  extra={"flowInstId": it["flowInstId"], "type": it.get("type")})
                 for it in data.get("dataList") or []]
        if not items:
            break
        yield items
        total_pages = (data.get("pagingParams") or {}).get("totalPage") or page
        if incremental and all(i.url in known for i in items):
            break
        if (max_pages and page >= max_pages) or page >= total_pages:
            break
        page += 1


def _file_url(fetcher: Fetcher, file_id: str) -> str:
    for api in ("getYuLanUrl", "getWordYuLanUrl"):
        try:
            path = _post(fetcher, f"lzhy/lzhyController/{api}", {"ticket": PREVIEW_TICKET, "fileId": file_id})
        except FetchError:
            continue
        if isinstance(path, str) and path:
            return path.replace(VIEW_METHOD, FILE_BASE)
    raise FetchError(f"附件 {file_id} 换不到下载地址")


def fetch_detail(src: dict, fetcher: Fetcher, notice: dict):
    extra = db.loads(notice.get("extra"))
    fid = extra["flowInstId"]
    ticket = _ticket(fetcher, src["portal_url"])
    file_extra: dict[str, dict] = {}

    if _is_approval(src):
        data = _post(fetcher, "lzhy/lzhyController/getLiuChengListById", {"ticket": ticket, "flowInstId": fid})
        e = data.get("tbusLzhyBasicInfoEntity") or {}
        if e.get("isNormal") == "1":
            query = dict(approvalNo=e.get("approvalNo"), projectName=e.get("projectName"), applyInst=e.get("applyInst"),
                         projectAddress=e.get("projectAddress"), completeTime=data.get("completeTime"),
                         flowInstId=e.get("flowInstId"), type="1", orgLon=e.get("orgLon"), orgLat=e.get("orgLat"),
                         suggest=e.get("suggest"), isPrintCG=e.get("isPrintCG"))
        else:
            query = dict(approvalNo=e.get("approvalNo"), projectName=e.get("projectName"), applyInst=e.get("applyInst"),
                         projectAddress=e.get("projectAddress"),
                         envirnment=f"{e.get('environmentIndCodeFullName')}-{e.get('environmentName')}",
                         pollutant=f"{e.get('pollutantPermitCodeFullName')}-{e.get('sewageFormName')}",
                         suggest="", approvalTime=data.get("approvalTime"), flowInstId=e.get("flowInstId"))
        name = f"{e.get('projectName')}环境影响{e.get('environmentName') or '评价文件'}的批复（{e.get('approvalNo')}）.pdf"
        url = f"sipac-seal://{fid}"
        file_extra[url] = {"adapter": "sipac", "method": "seal", "query": query,
                           "portal_url": src["portal_url"], "host": HOST}
        rows = [{"name": e.get("projectName") or "", "company": e.get("applyInst"), "location": e.get("projectAddress"),
                 "doc_no": e.get("approvalNo"), "doc_date": _date(data.get("approvalTime")), "links": [(url, name)]}]
        files = [(url, name)]
        pub_date = _date(data.get("approvalTime")) or notice.get("list_date")
    else:
        fmk = "1.15" if extra.get("type") == "4" else "1.1"
        data = _post(fetcher, "lzhy/lzhyController/getGongShiDataYQById",
                     {"ticket": ticket, "queryParams": json.dumps({"keyword": fid, "fileMoKuai": fmk})})
        files = []
        for f in data.get("files") or []:
            try:
                files.append((_file_url(fetcher, f["fileId"]), f.get("fileName") or f["fileId"]))
            except FetchError:
                continue
        rows = [{"name": data.get("projectName") or "", "company": data.get("applyInst"),
                 "location": data.get("projectAddress"), "eia_org": data.get("hpjgName"), "links": files}]
        pub_date = notice.get("list_date")

    raw = fetcher.raw_path(notice["url"]).with_suffix(".json")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    d = extract.Detail(title=notice["title"] or "", pub_date=pub_date, rows=rows, files=files, images=[], baidu=[],
                       content_html="")
    d.file_extra = file_extra
    return d, raw


def download(fetcher: Fetcher, att: dict, extra: dict, dest) -> tuple[str, int]:
    if extra.get("method") != "seal":
        return fetcher.download(att["url"], dest)
    for refresh in (False, True):
        ticket = _ticket(fetcher, extra["portal_url"], refresh=refresh)
        try:
            data = _post(fetcher, "siphbsl/lzhy/tBusLzhyUserJurisdictionModel/sealAddress",
                         {"ticket": ticket, "queryParams": json.dumps(extra["query"], ensure_ascii=False)})
        except FetchError:
            continue
        raw = base64.b64decode(data) if isinstance(data, str) else b""
        if raw.startswith(b"%PDF"):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            return "application/pdf", len(raw)
    raise FetchError("批复 PDF 生成失败")
