"""API 端到端测试：版本提交、冲突、重算、快照下载、并发隔离。"""
import io
import os
import tempfile

import pytest

# 必须在导入应用前指定独立临时数据库
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["APP_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # 锁定本模块自己的数据库，避免与其它测试模块的 db.DB_PATH 互相覆盖
    db.DB_PATH = os.environ["APP_DB_PATH"]
    db.init_db()
    # 每个模块使用全新数据库
    conn = db.connect()
    conn.execute("DELETE FROM delivery_impacts")
    conn.execute("DELETE FROM delivery_items")
    conn.execute("DELETE FROM deliveries")
    conn.execute("DELETE FROM exclusions")
    conn.execute("DELETE FROM results")
    conn.execute("DELETE FROM mappings")
    conn.execute("DELETE FROM lab_files")
    conn.execute("DELETE FROM versions")
    conn.execute("UPDATE meta SET current_version = NULL WHERE id = 1")
    conn.close()
    with TestClient(app) as c:
        yield c


def make_csv(rows, header=("sample", "pb")):
    buf = io.StringIO()
    buf.write(",".join(header) + "\n")
    for r in rows:
        buf.write(",".join(str(x) for x in r) + "\n")
    return buf.getvalue().encode("utf-8")


def form_files(client, rows_a, rows_b, coeff_a="1", coeff_b="1",
               expected="__auto__"):
    data = {
        "sample_col_a": "sample", "value_col_a": "pb", "coefficient_a": coeff_a,
        "sample_col_b": "sample", "value_col_b": "pb", "coefficient_b": coeff_b,
    }
    if expected == "__auto__":
        # 默认携带所见的当前版本号，模拟网页“所见版本提交”
        expected = client.get("/api/state").json()["version"]
    if expected is not None:
        data["expected_version"] = str(expected)
    return client.post(
        "/api/submit",
        data=data,
        files={
            "file_a": ("a.csv", make_csv(rows_a), "text/csv"),
            "file_b": ("b.csv", make_csv(rows_b), "text/csv"),
        },
    )


def test_preview_returns_headers(client):
    r = client.post(
        "/api/preview",
        files={"file": ("x.csv", make_csv([("S1", 0.1)]), "text/csv")},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["headers"] == ["sample", "pb"] and d["data_rows"] == 1


def test_state_empty(client):
    d = client.get("/api/state").json()
    assert d["current_version"] is None and d["results"] == []


def test_happy_flow_submit_and_download(client):
    r = form_files(client, [("S1", 0.10), ("S2", 0.30)],
                   [("S1", 0.12), ("S2", 0.31)])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["version"] == 1 and d["is_current"] is True
    assert d["stats"] == {"matched": 2, "excluded": 0}
    means = {x["sample_id"]: x["mean"] for x in d["results"]}
    assert means == {"S1": "0.11", "S2": "0.305"}
    # 原行号：表头第 1 行，S1 在第 2 行，S2 在第 3 行
    row = d["results"][0]
    assert row["row_a"] == 2 and row["row_b"] == 2

    dl = client.get("/api/download")
    assert dl.status_code == 200
    text = dl.content.decode("utf-8-sig")
    assert "样品号,铅含量均值(mg/kg)" in text
    assert "S1,0.11,0.1,0.12,2,2" in text
    assert dl.headers["content-disposition"].startswith(
        'attachment; filename="lead_results_v1.csv"')


def test_duplicate_rejected_no_version_created(client):
    before = client.get("/api/state").json()["version"]
    r = form_files(client, [("S1", 1), ("S1", 2)], [("S1", 1)])
    assert r.status_code == 400
    assert "重复" in r.json()["detail"]
    # 当前可用版本保持不变
    after = client.get("/api/state").json()["version"]
    assert after == before


def test_missing_and_over_tolerance_listed_with_reasons(client):
    r = form_files(
        client,
        [("S1", 0.10), ("S2", 0.20), ("S3", 0.20)],
        [("S1", 0.10), ("S2", 0.10), ("S4", 0.40)],
    )
    assert r.status_code == 200
    d = r.json()
    assert [x["sample_id"] for x in d["results"]] == ["S1"]
    excl = {x["sample_id"]: x["reason"] for x in d["exclusions"]}
    assert set(excl) == {"S2", "S3", "S4"}
    # 0.20 vs 0.10 差值恰好 0.10 > 0.05
    assert "超过允许差值 0.05" in excl["S2"]
    assert "0.1" in excl["S2"]
    assert "缺少乙" in excl["S3"]
    assert "缺少甲" in excl["S4"]


def test_stale_submit_conflicts_and_keeps_old_version(client):
    # 先确保存在一个当前版本（自包含，不依赖其它用例的执行顺序）
    base = form_files(client, [("S1", 0.10)], [("S1", 0.11)])
    if base.status_code == 409:
        current = client.get("/api/state").json()["version"]
    else:
        assert base.status_code == 200
        current = base.json()["version"]
    # 用过期的 expected_version 提交 -> 409
    r = form_files(client, [("S1", 0.1)], [("S1", 0.1)], expected=current - 1)
    assert r.status_code == 409
    assert "v" in r.json()["detail"]
    # 不带 expected（期望首版）也应冲突
    r2 = form_files(client, [("S1", 0.1)], [("S1", 0.1)], expected=None)
    assert r2.status_code == 409
    # 旧版本仍可用、内容未被破坏
    d = client.get("/api/state").json()
    assert d["version"] == current
    assert {x["sample_id"] for x in d["results"]} == {"S1"}


def test_remap_recomputes_from_originals_and_switches_version(client):
    # 当前版本原始文件：乙的 100 配合系数 1 会超差，先把当前基线推进到一个已知版本
    base = form_files(
        client, [("S1", 0.10)], [("S1", 100)], coeff_b="0.001",
        expected=client.get("/api/state").json()["version"],
    )
    assert base.status_code == 200
    v = base.json()["version"]
    assert base.json()["stats"]["matched"] == 1  # 100*0.001=0.10 与 0.10 一致

    # 只改系数（不改文件）：100*0.002=0.20，差值 0.10 超差 -> 拒收
    r = client.post("/api/remap", json={
        "expected_version": v,
        "mappings": [
            {"lab": "A", "sample_column": "sample", "value_column": "pb", "coefficient": "1"},
            {"lab": "B", "sample_column": "sample", "value_column": "pb", "coefficient": "0.002"},
        ],
    })
    assert r.status_code == 200
    d = r.json()
    assert d["version"] == v + 1 and d["reason"] == "remap"
    assert d["stats"] == {"matched": 0, "excluded": 1}
    assert "0.1" in d["exclusions"][0]["reason"]

    # remap 的乐观锁：再用旧版本号提交 -> 409
    r2 = client.post("/api/remap", json={
        "expected_version": v,
        "mappings": [
            {"lab": "A", "sample_column": "sample", "value_column": "pb", "coefficient": "1"},
            {"lab": "B", "sample_column": "sample", "value_column": "pb", "coefficient": "0.001"},
        ],
    })
    assert r2.status_code == 409
    # 失败后当前仍为超差版本，原可用结果未被覆盖
    cur = client.get("/api/state").json()
    assert cur["version"] == v + 1 and cur["stats"]["excluded"] == 1


def test_invalid_remap_rejected_without_losing_current(client):
    v = client.get("/api/state").json()["version"]
    r = client.post("/api/remap", json={
        "expected_version": v,
        "mappings": [
            {"lab": "A", "sample_column": "sample", "value_column": "pb", "coefficient": "0"},
            {"lab": "B", "sample_column": "sample", "value_column": "pb", "coefficient": "1"},
        ],
    })
    assert r.status_code == 400
    assert client.get("/api/state").json()["version"] == v


def test_version_pinned_download_does_not_mix_versions(client):
    # 旧版本 v1 的下载必须始终返回 v1 快照，即使当前已推进多版
    dl1 = client.get("/api/download?version=1")
    assert dl1.status_code == 200
    assert 'filename="lead_results_v1.csv"' in dl1.headers["content-disposition"]
    assert b"S1,0.11" in dl1.content
    # 不存在的版本 -> 404
    assert client.get("/api/download?version=9999").status_code == 404
    assert client.get("/api/state?version=9999").status_code == 404
    # 显式查看旧版本状态
    old = client.get("/api/state?version=1").json()
    assert old["is_current"] is False and old["stats"]["matched"] == 2


def test_concurrent_submits_only_one_wins(client):
    """两个并发提交携带相同 expected_version，仅一个成功，另一个 409，当前版本唯一。"""
    import concurrent.futures

    v = client.get("/api/state").json()["version"]

    def do_submit(i):
        return form_files(
            client, [(f"C{i}", 0.1)], [(f"C{i}", 0.1)], expected=v
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(do_submit, [1, 2]))
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409]
    d = client.get("/api/state").json()
    assert d["version"] == v + 1  # 只新增了一个版本
    # 赢家的提交内容生效，输家的数据没有混入
    assert d["results"][0]["sample_id"] in {"C1", "C2"}
    assert len(d["results"]) == 1


def test_concurrent_remaps_only_one_wins(client):
    """两个并发 remap 携带相同 expected_version：严格一个成功，且无 TOCTOU 双写。"""
    import concurrent.futures

    v = client.get("/api/state").json()["version"]
    payload = {
        "expected_version": v,
        "mappings": [
            {"lab": "A", "sample_column": "sample", "value_column": "pb", "coefficient": "1"},
            {"lab": "B", "sample_column": "sample", "value_column": "pb", "coefficient": "1"},
        ],
    }

    def do_remap(_):
        return client.post("/api/remap", json=payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(do_remap, range(4)))
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(200) == 1
    assert statuses.count(409) == 3
    d = client.get("/api/state").json()
    assert d["version"] == v + 1  # 仍然只新增一个版本
