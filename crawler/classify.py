"""标题 → 阶段标签；附件名 → 文件类型；项目名规范化。规则来自飞书数据源表里整理的 60+ 种标题写法。"""
from __future__ import annotations

import re
import unicodedata

RELEVANT = re.compile(r"环评|环境影响|报告书|报告表|批复")

EXCLUDE_RULES = [
    (re.compile(r"竣工|验收"), "验收"),
    (re.compile(r"第一次公示|首次公示|一次信息公示"), "第一次公示"),
    (re.compile(r"规划.{0,30}(环境影响|环评)|(环境影响|环评).{0,6}规划|规划批前|规划修编|规划.{0,4}审查意见"), "规划/规划环评"),
    (re.compile(r"危险废物|排污许可|辐射安全许可|危险化学品|经营许可"), "其他许可"),
    (re.compile(r"机构的?名单|工作流程图|办事指南|收费标准|信息公开登记表"), "非公示"),
]
S2 = re.compile(r"拟.{0,6}(?:作出|审批|批准|审查|同意)|拟对")
S3 = re.compile(r"审批决定|批复|批准项目公告|批准公告|作出.{0,30}决定|审批情况|告知承诺|审批意见的公告|批准的")
S1 = re.compile(r"受理")
S0 = re.compile(r"全本|报批前|报批公示|第二次公示|二次公示|征求意见稿")
# 合并公示在公告表里仍记 S1（其他代码按 S1 走详情/附件），靠 stage_reason 区分；取报告时算拟审批一级
MERGED_REASON = "受理+拟审批合并公示"

# 报告取用：公告阶段 → 优先级（越小越优先），不在表里的阶段（S0 建设单位报批前全本等）不作报告来源。
# 业主 2026-09-15 确认，同 docs/national_eia_db/schema_pg.sql 的 dict_stage.report_rank。S1S2 = 受理+拟审批合并公示
REPORT_RANK = {"S3": 1, "S2": 2, "S1S2": 2, "S1": 3}
FALLBACK_STAGES = {"S1"}
# 同阶段内按附件名里的版本字样排序，同 dict_report_version。“重新报批”“报批前公示”不算报批稿
VERSION_RULES = [
    (re.compile(r"报批终?[稿版]|审批[稿版]"), 1, "报批稿"),
    (re.compile(r"送审"), 2, "送审稿"),
    (re.compile(r"公示|征求意见|全本"), 3, "公示稿"),
]
# 不交付：告知承诺、辐射类（专栏整栏排除 + 标题命中）
OUT_OF_SCOPE_SOURCES = {"sz_sthjj_gzcn": "告知承诺", "sz_sthjj_fs_sl": "辐射", "sz_sthjj_fs_pz": "辐射"}
OUT_OF_SCOPE_TITLE = re.compile(r"告知承诺|承诺制|辐射")


def stage(title: str, hint: str | None = None, require_keyword: bool = True) -> tuple[str, str]:
    """返回 (阶段, 命中原因)。require_keyword 只对混杂栏目（公示公告、通知公告）开启；环评专栏里的标题可能不带环评字样。"""
    t = re.sub(r"\s+", "", title or "")
    if require_keyword and not RELEVANT.search(t):
        return "IRRELEVANT", "标题不含环评关键词"
    for rx, why in EXCLUDE_RULES:
        if rx.search(t):
            return "EXCLUDE", why
    if S1.search(t) and S2.search(t):
        return "S1", MERGED_REASON
    if S2.search(t):
        return "S2", "拟审批"
    if S3.search(t):
        return "S3", "审批决定"
    if S1.search(t):
        return "S1", "受理"
    if S0.search(t):
        return "S0", "报批前/全本"
    if hint:
        return hint, "栏目默认阶段"
    return "UNKNOWN", "规则未命中"


DOC_EXCLUDE = re.compile(r"公众参与|意见表|意见调查|登记表|承诺书|委托书|验收|监测报告|试生产|听证")
DOC_APPROVAL = re.compile(r"批复|审批意见|审批决定|环审|环建|管审|〔\d{4}〕|\[\d{4}\]|【\d{4}】")
DOC_REPORT = re.compile(r"报告书|报告表|公示版|公示稿|公示文本|全本|报批|征求意见稿|环评文件|环境影响")


def doc_type(name: str, notice_stage: str) -> str:
    n = name or ""
    if DOC_EXCLUDE.search(n):
        return "exclude"
    if DOC_APPROVAL.search(n):
        return "approval"
    if DOC_REPORT.search(n):
        return "report"
    if notice_stage == "S3":
        return "approval"
    if notice_stage in ("S0", "S1"):
        return "report"
    return "other"


def report_source_stage(notice_stage: str | None, reason: str | None) -> str | None:
    """公告阶段 → 报告来源阶段（REPORT_RANK 的键）；None 表示这个公告里的报告不能用。"""
    s = "S1S2" if notice_stage == "S1" and reason == MERGED_REASON else notice_stage
    return s if s in REPORT_RANK else None


def version_label(name: str) -> tuple[int, str]:
    n = clean(name)
    return next(((rank, label) for rx, rank, label in VERSION_RULES if rx.search(n)), (4, "未标版本"))


def out_of_scope(source_id: str, title: str) -> str | None:
    if source_id in OUT_OF_SCOPE_SOURCES:
        return OUT_OF_SCOPE_SOURCES[source_id]
    m = OUT_OF_SCOPE_TITLE.search(clean(title))
    return ("辐射" if m.group(0) == "辐射" else "告知承诺") if m else None


def report_version(notice_stage: str, text: str) -> str:
    if notice_stage == "S3":
        return "批复公告附件"
    if notice_stage == "S1S2":
        return "受理及拟审批公示版"
    if notice_stage == "S1":
        return "受理公示版"
    if notice_stage == "S0":
        return "征求意见稿" if re.search(r"第二次|二次公示|征求意见", text or "") else "报批前公示版"
    if notice_stage == "S2":
        return "拟审批公示附件"
    return "未知版本"


def report_kind(name: str, title: str = "") -> str:
    """先看附件名，再看公告标题；“报告书（表）”这种两者都提的不算数。"""
    for text in (clean(name), clean(title)):
        if re.search(r"报告书[(（]表[)）]", text):
            continue
        has_book, has_table = "报告书" in text, "报告表" in text
        if has_book != has_table:
            return "报告书" if has_book else "报告表"
    return "报告"


_KEY_STRIP = re.compile(r"[\s《》“”\"'‘’【】\[\]()（）·、，,。.:：;；\-—_/]")
_KEY_NOISE = re.compile(r"(生态)?环境影响(评价)?(报告[书表]|文件)?|环评(报告[书表]|文件)?|公示版|公示稿|公示文本|全本公示|报批前|征求意见稿|附件\d*|\.(pdf|docx?|wps|zip|rar)$")


def clean(s: str | None) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).strip()


def name_key(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "")
    s = re.sub(r"\.(pdf|docx?|wps|zip|rar)$", "", s, flags=re.I)
    s = _KEY_NOISE.sub("", s)
    s = _KEY_STRIP.sub("", s)
    return s


_FROM_APPROVAL = re.compile(r"关于(?:对)?[《“\"]?([^关，。；]{4,80}?)[》”\"]?的?(?:生态)?环境影响(?:评价)?(?:报告[书表]|文件|登记表)?》?的?(?:批复|审批意见|审批决定|审批)")
_FROM_BRACKET = re.compile(r"【([^】]{4,})】")
_ATTACH_NOISE = re.compile(r"^(附件\d*[:：、]?|\d+[-、.]\d*[、.]?)|^(?:环评)?公示(?:稿|版|文本)?[-—_:：]|[-—_]公示$"
                           r"|[-—_（(]?(公示版|公示稿|公示文本|全本公示稿?|报批前公示稿?|征求意见稿)[)）]?|\.(pdf|docx?|wps|zip|rar)$", re.I)
_COMPANY_SUFFIX = re.compile(r"(股份有限公司|有限责任公司|有限公司|分公司|集团|公司)$")
_REGION_PREFIX = re.compile(r"^(中国|江苏省?|苏州市?|昆山市?|常熟市?|太仓市?|张家港市?|吴江区?|吴中区?|相城区?|姑苏区?|虎丘区?)")


def company_core(company: str | None) -> str:
    """公司名去掉地区前缀和公司后缀，用来在附件名里找简称，如“苏州伟亿祥塑胶有限公司”→“伟亿祥塑胶”。"""
    k = name_key(company or "")
    k = _COMPANY_SUFFIX.sub("", k)
    return _REGION_PREFIX.sub("", k)


def project_name_from_text(text: str) -> str | None:
    t = clean(text)
    m = _FROM_BRACKET.search(t)
    if m:
        return m.group(1)
    m = _FROM_APPROVAL.search(t)
    if m:
        return m.group(1)
    m = _FROM_BRACKET.search(t)
    if m:
        return m.group(1)
    return None


def project_names_in_text(text: str) -> list[str]:
    """正文里“关于对 X 项目环境影响报告表的批复”这类句子，用于没有表格的公告。"""
    return list(dict.fromkeys(m.group(1) for m in _FROM_APPROVAL.finditer(clean(text))))


def project_name_from_attachment(name: str) -> str:
    n = clean(name)
    prev = None
    while prev != n:
        prev, n = n, _ATTACH_NOISE.sub("", n)
    return n


_DOC_NO = re.compile(r"[一-龥]{1,8}[〔\[【(（]\s*20\d{2}\s*[〕\]】)）]\s*(?:\d+\s*)?(?:第\s*)?\d+\s*号")


def doc_no(text: str) -> str | None:
    m = _DOC_NO.search(unicodedata.normalize("NFKC", text or ""))
    return re.sub(r"\s+", "", m.group(0)) if m else None


_AUTH = re.compile(r"(?:\d{1,2}日|^)(.{2,30}?(?:管理委员会|管委会|生态环境局|行政审批局|数据局|政务服务管理办公室|审批局))(?:关于|受理|作出|拟|对)")
_DISTRICT = re.compile(r"[（(]([^（）()]{2,10}?(?:市|区|县|园区|开发区))[)）]\s*$")


def authority_from_title(title: str) -> str | None:
    t = re.sub(r"^\d{4}年\d{1,2}月\d{1,2}日", "", clean(title))
    m = _AUTH.search(t)
    return m.group(1) if m else None


def district_from_title(title: str) -> str | None:
    m = _DISTRICT.search(unicodedata.normalize("NFKC", title or "").strip())
    return m.group(1) if m else None
