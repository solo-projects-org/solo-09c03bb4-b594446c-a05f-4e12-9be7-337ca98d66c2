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

-- 交付：一次“把所见当前版本交给分析组”的登记。每个版本只保留一个交付号，
-- 同版重试直接沿用，不产生新交付。
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    note TEXT,
    status TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'needs_recall')),
    UNIQUE (version_id)
);

-- 交付时的纳入样品快照（即交付 CSV 的逐行内容），后续比对永远以它为基线。
CREATE TABLE IF NOT EXISTS delivery_items (
    delivery_id INTEGER NOT NULL REFERENCES deliveries(id),
    sample_id TEXT NOT NULL,
    mean TEXT NOT NULL,
    value_a TEXT NOT NULL,
    value_b TEXT NOT NULL,
    row_a INTEGER NOT NULL,
    row_b INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (delivery_id, sample_id)
);

-- 影响记录：一个样品的一段“受影响”历史。首次受影响时写入旧（交付基线）
-- 与新（首次受影响版本）行；恢复时补 resolved_* 并保留整条记录，绝不删除。
-- 若恢复后再次受影响，另起一条新记录。
CREATE TABLE IF NOT EXISTS delivery_impacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(id),
    sample_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('changed', 'removed')),
    old_mean TEXT NOT NULL,
    old_value_a TEXT NOT NULL,
    old_value_b TEXT NOT NULL,
    old_row_a INTEGER NOT NULL,
    old_row_b INTEGER NOT NULL,
    new_mean TEXT,
    new_value_a TEXT,
    new_value_b TEXT,
    new_row_a INTEGER,
    new_row_b INTEGER,
    first_version INTEGER NOT NULL REFERENCES versions(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_version INTEGER REFERENCES versions(id),
    resolved_mean TEXT,
    resolved_value_a TEXT,
    resolved_value_b TEXT,
    resolved_row_a INTEGER,
    resolved_row_b INTEGER,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_results_version ON results(version_id);
CREATE INDEX IF NOT EXISTS idx_excl_version ON exclusions(version_id);
CREATE INDEX IF NOT EXISTS idx_delivery_items_delivery ON delivery_items(delivery_id);
CREATE INDEX IF NOT EXISTS idx_delivery_impacts_delivery ON delivery_impacts(delivery_id);
CREATE INDEX IF NOT EXISTS idx_delivery_impacts_open
    ON delivery_impacts(delivery_id, resolved_version);
-- 同一交付的同一品，同一时间最多只能有一条未恢复记录
CREATE UNIQUE INDEX IF NOT EXISTS ux_delivery_impacts_open_sample
    ON delivery_impacts(delivery_id, sample_id) WHERE resolved_version IS NULL;
"""


def init_db(db_path: Optional[str] = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


class StaleVersionError(Exception):
    """expected_version 与当前版本不一致（乐观锁冲突）。"""


class StaleDeliveryVersionError(StaleVersionError):
    """登记交付时所见版本已过期，且该版本从未登记过交付。"""


class VersionNotFoundError(Exception):
    """请求的版本不存在。"""


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

    # 与版本创建同一事务：逐交付比对并写影响记录 + 同步状态。
    # 任何一步失败都随事务回滚，不会留下半份影响记录。
    apply_delivery_impacts(conn, vid)

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


# ---------------------------------------------------------------------------
# 交付登记与追回（recall）判定
# ---------------------------------------------------------------------------

def _version_results_map(conn: sqlite3.Connection, version_id: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT sample_id, mean, value_a, value_b, row_a, row_b FROM results"
        " WHERE version_id = ? ORDER BY seq",
        (version_id,),
    ).fetchall()
    return {r["sample_id"]: dict(r) for r in rows}


def _dec_equal(a: str, b: str) -> bool:
    """按 Decimal 数值比较，避免 0.10 与 0.100 这类等值写法被误判为变化。"""
    try:
        return Decimal(a) == Decimal(b)
    except (ArithmeticError, ValueError):
        return a == b


def _row_equal(old: dict, new: Optional[dict]) -> bool:
    """交付行是否等价（均值、两侧换算值按数值，原行号按精确值）。"""
    if new is None:
        return False
    return (
        _dec_equal(old["mean"], new["mean"])
        and _dec_equal(old["value_a"], new["value_a"])
        and _dec_equal(old["value_b"], new["value_b"])
        and old["row_a"] == new["row_a"]
        and old["row_b"] == new["row_b"]
    )


def register_delivery(
    conn: sqlite3.Connection,
    version_id: int,
    expected_version: Optional[int],
    note: Optional[str],
) -> int:
    """在调用方的 IMMEDIATE 事务内登记交付；返回交付号。

    规则：
    - 同版重试沿用已有交付号（幂等），不再新建；
    - 过期版本不能新登记：expected_version 必须与当前版本一致，
      或该版本已经登记过交付（此前合法登记过的交付仍可再次取号/查看）；
    - 登记的是该版本的纳入结果快照，之后任何版本变动都以它为基线比对。

    若版本不存在抛 VersionNotFoundError。
    """
    state = get_version_state(conn, version_id)
    if state is None:
        raise VersionNotFoundError(f"版本 v{version_id} 不存在")

    existing = conn.execute(
        "SELECT id FROM deliveries WHERE version_id = ?", (version_id,)
    ).fetchone()
    if existing is not None:
        return existing["id"]

    current = get_current_version(conn)
    # 新登记必须仍是所见版本：不带 expected_version（表示“未见版本”）
    # 不能用于在任何版本上补登记交付。
    if expected_version is None:
        raise StaleDeliveryVersionError(
            "登记交付必须携带所见版本号（expected_version）"
        )
    if expected_version != current:
        raise StaleDeliveryVersionError(
            f"登记基于 v{expected_version}，但当前版本已变为 v{current}，不能新登记交付"
        )
    if version_id != current:
        raise StaleDeliveryVersionError(
            f"版本 v{version_id} 已过期（当前 v{current}），不能新登记交付"
        )

    cur = conn.execute(
        "INSERT INTO deliveries (version_id, note) VALUES (?, ?)",
        (version_id, note),
    )
    delivery_id = cur.lastrowid
    # 交付基线：该版本纳入的每一行，逐字段冻结（seq 保全交付当时的 CSV 行序）
    for seq, r in enumerate(state["results"]):
        conn.execute(
            "INSERT INTO delivery_items"
            " (delivery_id, sample_id, mean, value_a, value_b, row_a, row_b, seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (delivery_id, r["sample_id"], r["mean"], r["value_a"],
             r["value_b"], r["row_a"], r["row_b"], seq),
        )
    return delivery_id


def _delivery_baseline(conn: sqlite3.Connection, delivery_id: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT sample_id, mean, value_a, value_b, row_a, row_b FROM delivery_items"
        " WHERE delivery_id = ? ORDER BY sample_id",
        (delivery_id,),
    ).fetchall()
    return {r["sample_id"]: dict(r) for r in rows}


def _open_impacts(conn: sqlite3.Connection, delivery_id: int) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM delivery_impacts"
        " WHERE delivery_id = ? AND resolved_version IS NULL",
        (delivery_id,),
    ).fetchall()
    return {r["sample_id"]: r for r in rows}


def _refresh_delivery_status(
    conn: sqlite3.Connection, delivery_id: int, touched: set[str]
) -> None:
    """只更新本次写入涉及的交付状态，避免全表扫描且保证状态与记录同步。"""
    if not touched:
        return
    status = "needs_recall" if _open_impacts(conn, delivery_id) else "valid"
    conn.execute(
        "UPDATE deliveries SET status = ? WHERE id = ? AND status != ?",
        (status, delivery_id, status),
    )


def apply_delivery_impacts(conn: sqlite3.Connection, new_version: int) -> None:
    """新版本结果可见前，在一个事务内同步所有交付的追回状态。

    对每个已经登记过的交付，用其“交付基线”与新版本纳入结果比对：
    - 样品不再纳入，或均值 / 甲换算值 / 乙换算值 / 任一侧原行号变化
      -> 若该样品当前没有未恢复的影响记录，则记录首次受影响时的旧行与新行；
    - 样品的交付行恢复为与基线完全一致 -> 给未恢复记录补 resolved_*，保留记录；
    - 仅版号变化而交付行不变 -> 不记录影响，交付仍有效。

    只有真正写入影响记录或完成恢复的交付才重算状态，
    其余（无变化）保持原状态，因此反复重算不会产生重复记录。
    """
    deliveries = conn.execute(
        "SELECT id FROM deliveries ORDER BY id"
    ).fetchall()
    current = _version_results_map(conn, new_version)
    for d in deliveries:
        delivery_id = d["id"]
        baseline = _delivery_baseline(conn, delivery_id)
        open_impacts = _open_impacts(conn, delivery_id)
        touched: set[str] = set()

        for sample_id, old in baseline.items():
            new = current.get(sample_id)
            affected = new is None or not _row_equal(old, new)
            open_row = open_impacts.get(sample_id)
            if affected and open_row is None:
                kind = "removed" if new is None else "changed"
                conn.execute(
                    "INSERT INTO delivery_impacts"
                    " (delivery_id, sample_id, kind,"
                    "  old_mean, old_value_a, old_value_b, old_row_a, old_row_b,"
                    "  new_mean, new_value_a, new_value_b, new_row_a, new_row_b,"
                    "  first_version)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        delivery_id, sample_id, kind,
                        old["mean"], old["value_a"], old["value_b"],
                        old["row_a"], old["row_b"],
                        None if new is None else new["mean"],
                        None if new is None else new["value_a"],
                        None if new is None else new["value_b"],
                        None if new is None else new["row_a"],
                        None if new is None else new["row_b"],
                        new_version,
                    ),
                )
                touched.add(sample_id)
            elif not affected and open_row is not None:
                conn.execute(
                    "UPDATE delivery_impacts"
                    " SET resolved_version = ?, resolved_mean = ?, resolved_value_a = ?,"
                    "     resolved_value_b = ?, resolved_row_a = ?, resolved_row_b = ?,"
                    "     resolved_at = datetime('now')"
                    " WHERE id = ? AND resolved_version IS NULL",
                    (
                        new_version, new["mean"], new["value_a"], new["value_b"],
                        new["row_a"], new["row_b"], open_row["id"],
                    ),
                )
                touched.add(sample_id)

        _refresh_delivery_status(conn, delivery_id, touched)


def get_delivery(conn: sqlite3.Connection, delivery_id: int) -> Optional[dict]:
    d = conn.execute(
        "SELECT id, version_id, created_at, note, status FROM deliveries WHERE id = ?",
        (delivery_id,),
    ).fetchone()
    if d is None:
        return None
    items = conn.execute(
        "SELECT sample_id, mean, value_a, value_b, row_a, row_b FROM delivery_items"
        " WHERE delivery_id = ? ORDER BY seq",
        (delivery_id,),
    ).fetchall()
    impacts = conn.execute(
        "SELECT * FROM delivery_impacts WHERE delivery_id = ? ORDER BY id",
        (delivery_id,),
    ).fetchall()
    current = get_current_version(conn)
    return {
        "delivery": {
            "id": d["id"],
            "version": d["version_id"],
            "created_at": d["created_at"],
            "note": d["note"],
            "status": d["status"],
            "is_version_current": current == d["version_id"],
        },
        "items": [dict(i) for i in items],
        "impacts": [dict(x) for x in impacts],
    }


def list_deliveries(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT d.id, d.version_id, d.created_at, d.note, d.status,"
        "       (SELECT COUNT(*) FROM delivery_impacts i"
        "         WHERE i.delivery_id = d.id AND i.resolved_version IS NULL) AS open_impacts,"
        "       (SELECT COUNT(*) FROM delivery_items it WHERE it.delivery_id = d.id) AS item_count"
        " FROM deliveries d ORDER BY d.id"
    ).fetchall()
    return [dict(r) for r in rows]

