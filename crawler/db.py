"""SQLite 存储。每个线程用自己的连接（WAL 模式）。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    title TEXT,
    list_date TEXT,
    pub_date TEXT,
    stage TEXT,              -- S0/S1/S2/S3/EXCLUDE/IRRELEVANT/UNKNOWN
    stage_reason TEXT,
    authority TEXT,
    district TEXT,
    status TEXT NOT NULL DEFAULT 'new',   -- new/fetched/skipped/error
    raw_path TEXT,
    extra TEXT,
    error TEXT,
    first_seen TEXT DEFAULT (datetime('now','localtime')),
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notices_source ON notices(source_id, status);

CREATE TABLE IF NOT EXISTS project_rows (
    id INTEGER PRIMARY KEY,
    notice_id INTEGER NOT NULL REFERENCES notices(id),
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    company TEXT,
    location TEXT,
    eia_org TEXT,
    doc_no TEXT,
    doc_date TEXT,
    file_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_rows_key ON project_rows(name_key);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    notice_id INTEGER NOT NULL REFERENCES notices(id),
    project_row_id INTEGER REFERENCES project_rows(id),
    parent_id INTEGER REFERENCES attachments(id),
    kind TEXT NOT NULL,       -- file/images/body/member
    url TEXT NOT NULL,
    name TEXT,
    ext TEXT,
    doc_type TEXT,            -- report/approval/other/exclude
    status TEXT NOT NULL DEFAULT 'new',  -- new/downloaded/extracted/converted/failed/unsupported/skipped
    sha256 TEXT,
    path TEXT,
    size INTEGER,
    content_type TEXT,
    pdf_path TEXT,
    pages INTEGER,
    extra TEXT,
    error TEXT,
    UNIQUE(notice_id, url)
);
CREATE INDEX IF NOT EXISTS idx_att_status ON attachments(status);

CREATE TABLE IF NOT EXISTS baidupan_links (
    id INTEGER PRIMARY KEY,
    notice_id INTEGER NOT NULL REFERENCES notices(id),
    url TEXT NOT NULL,
    code TEXT,
    UNIQUE(notice_id, url)
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    city TEXT,
    name_key TEXT,
    name TEXT,
    company TEXT,
    authority TEXT,
    district TEXT,
    report_att_id INTEGER,
    report_stage TEXT,       -- S3/S2/S1S2(受理+拟审批合并)/S1，报告取自哪个阶段的公告
    report_date TEXT,
    report_is_fallback INTEGER,  -- 1 = 只有受理公示版（兜底）
    report_version TEXT,     -- 附件名版本字样：报批稿/送审稿/公示稿/未标版本
    approval_att_id INTEGER,
    approval_date TEXT,
    doc_no TEXT,
    stages TEXT,
    notice_ids TEXT
);
"""
# 老库补列（CREATE TABLE IF NOT EXISTS 不会加列）
MIGRATIONS = [("projects", "report_is_fallback", "INTEGER"), ("projects", "report_version", "TEXT")]


_init_lock = threading.Lock()
_initialized: set[str] = set()


def connect(path: Path) -> sqlite3.Connection:
    """多个线程同时首次打开新库时，切 WAL 和建表会互相锁住，所以串行创建连接、每个进程只初始化一次。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _init_lock:
        conn = sqlite3.connect(path, timeout=120, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=120000")
        if str(path) not in _initialized:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            for table, col, typ in MIGRATIONS:
                if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            _initialized.add(str(path))
    return conn


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def loads(text: str | None):
    return json.loads(text) if text else {}
