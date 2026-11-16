"""SQLite 持久化：以不可变版本快照保存每次提交，整体切换当前版本。

并发控制要点：
- 所有写事务使用 BEGIN IMMEDIATE，提交序列化；
- 提交前在事务内比对 expected_version，过期则 409 冲突，旧版本原样保留；
- 每个版本的文件、映射、结果、拒收明细均为独立行数据，
  下载按 version_id 读取，绝不与当前版本混用。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .logic import Excluded, MatchResult, ParsedFile, format_decimal

DB_PATH = os.environ.get("APP_DB_PATH", "/data/app.db")


def get_db_path() -> str:
    return DB_PATH


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.isolation_level = None  # 手动事务
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_version INTEGER
);
INSERT OR IGNORE INTO meta (id, current_version) VALUES (1, NULL);

CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    reason TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS lab_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    lab TEXT NOT NULL CHECK (lab IN ('A', 'B')),
    filename TEXT NOT NULL,
    content BLOB NOT NULL,
    headers_json TEXT NOT NULL,
    UNIQUE (version_id, lab)
);

CREATE TABLE IF NOT EXISTS mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    lab TEXT NOT NULL,
    sample_column TEXT NOT NULL,
    value_column TEXT NOT NULL,
    coefficient TEXT NOT NULL,
    UNIQUE (version_id, lab)
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    sample_id TEXT NOT NULL,
    mean TEXT NOT NULL,
    value_a TEXT NOT NULL,
    value_b TEXT NOT NULL,
    row_a INTEGER NOT NULL,
    row_b INTEGER NOT NULL,
    seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    sample_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    value_a TEXT,
    value_b TEXT,
    row_a INTEGER,
    row_b INTEGER,
    seq INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_results_version ON results(version_id);
CREATE INDEX IF NOT EXISTS idx_excl_version ON exclusions(version_id);
"""


def init_db(db_path: Optional[str] = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


class StaleVersionError(Exception):
    """expected_version 与当前版本不一致（乐观锁冲突）。"""


@dataclass
class VersionInput:
    reason: str
    note: Optional[str]
    files: dict          # {'A': (filename, bytes, headers), 'B': ...}
    mappings: dict       # {'A': (sample_col, value_col, Decimal), ...}
    matched: list[MatchResult]
    excluded: list[Excluded]


def get_current_version(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute("SELECT current_version FROM meta WHERE id = 1").fetchone()
    return row["current_version"]


def begin_immediate(conn: sqlite3.Connection) -> None:
    """开启写事务并立即获取 RESERVED 锁，序列化所有写操作。"""
    conn.execute("BEGIN IMMEDIATE")


def commit(conn: sqlite3.Connection) -> None:
    conn.execute("COMMIT")


def rollback(conn: sqlite3.Connection) -> None:
    conn.execute("ROLLBACK")


def create_version(
    conn: sqlite3.Connection,
    data: VersionInput,
    expected_version: Optional[int],
) -> int:
    """在调用方已持有的 IMMEDIATE 事务内创建版本并整体切换当前版本。

    expected_version 为 None 表示仅允许在尚无任何版本时创建首版；
    否则必须与当前版本完全一致，不一致抛 StaleVersionError，
    由调用方回滚，原有可用版本不受影响。
    """
    current = get_current_version(conn)
    if expected_version is None:
        if current is not None:
            raise StaleVersionError(
                f"当前已有版本 v{current}，请基于最新版本提交"
            )
    elif current != expected_version:
        raise StaleVersionError(
            f"提交基于 v{expected_version}，但当前版本已变为 v{current}"
        )

    cur = conn.execute(
        "INSERT INTO versions (reason, note) VALUES (?, ?)",
        (data.reason, data.note),
    )
    vid = cur.lastrowid

    import json
    for lab in ("A", "B"):
        filename, content, headers = data.files[lab]
        conn.execute(
            "INSERT INTO lab_files (version_id, lab, filename, content, headers_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (vid, lab, filename, content, json.dumps(headers, ensure_ascii=False)),
        )
        sample_col, value_col, coeff = data.mappings[lab]
        conn.execute(
            "INSERT INTO mappings (version_id, lab, sample_column, value_column, coefficient)"
            " VALUES (?, ?, ?, ?, ?)",
            (vid, lab, sample_col, value_col, format_decimal(coeff)),
        )

    for seq, m in enumerate(data.matched):
        conn.execute(
            "INSERT INTO results (version_id, sample_id, mean, value_a, value_b, row_a, row_b, seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, m.sample_id, format_decimal(m.mean),
             format_decimal(m.value_a), format_decimal(m.value_b),
             m.row_a, m.row_b, seq),
        )
    for seq, x in enumerate(data.excluded):
        conn.execute(
            "INSERT INTO exclusions (version_id, sample_id, reason, value_a, value_b, row_a, row_b, seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, x.sample_id, x.reason,
             format_decimal(x.value_a) if x.value_a is not None else None,
             format_decimal(x.value_b) if x.value_b is not None else None,
             x.row_a, x.row_b, seq),
        )

    conn.execute("UPDATE meta SET current_version = ? WHERE id = 1", (vid,))
    return vid


def get_version_state(conn: sqlite3.Connection, version_id: int) -> Optional[dict]:
    v = conn.execute(
        "SELECT id, created_at, reason, note FROM versions WHERE id = ?",
        (version_id,),
    ).fetchone()
    if v is None:
        return None
    mappings = conn.execute(
        "SELECT lab, sample_column, value_column, coefficient FROM mappings"
        " WHERE version_id = ? ORDER BY lab",
        (version_id,),
    ).fetchall()
    files = conn.execute(
        "SELECT lab, filename, headers_json FROM lab_files WHERE version_id = ? ORDER BY lab",
        (version_id,),
    ).fetchall()
    results = conn.execute(
        "SELECT sample_id, mean, value_a, value_b, row_a, row_b FROM results"
        " WHERE version_id = ? ORDER BY seq",
        (version_id,),
    ).fetchall()
    exclusions = conn.execute(
        "SELECT sample_id, reason, value_a, value_b, row_a, row_b FROM exclusions"
        " WHERE version_id = ? ORDER BY seq",
        (version_id,),
    ).fetchall()
    import json
    return {
        "version": v["id"],
        "created_at": v["created_at"],
        "reason": v["reason"],
        "note": v["note"],
        "mappings": [dict(m) for m in mappings],
        "files": [
            {"lab": f["lab"], "filename": f["filename"],
             "headers": json.loads(f["headers_json"])}
            for f in files
        ],
        "results": [dict(r) for r in results],
        "exclusions": [dict(e) for e in exclusions],
    }


def get_lab_content(conn: sqlite3.Connection, version_id: int, lab: str) -> Optional[tuple[str, bytes, str, str, Decimal]]:
    row = conn.execute(
        "SELECT lf.filename, lf.content, m.sample_column, m.value_column, m.coefficient"
        " FROM lab_files lf JOIN mappings m ON m.version_id = lf.version_id AND m.lab = lf.lab"
        " WHERE lf.version_id = ? AND lf.lab = ?",
        (version_id, lab),
    ).fetchone()
    if row is None:
        return None
    return (row["filename"], row["content"], row["sample_column"],
            row["value_column"], Decimal(row["coefficient"]))
