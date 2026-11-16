"""交付登记与追回判定测试：同版重试、过期登记、首次影响、恢复留档、下载冻结、并发与失败回滚。"""
import io
import os
import tempfile

import pytest

from app import db

# 本模块使用独立数据库，避免与其它测试模块共享状态
_tmpdir = tempfile.mkdtemp(prefix="lead-delivery-")
db.DB_PATH = os.path.join(_tmpdir, "delivery.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # 锁定本模块自己的数据库，避免与其它测试模块的 db.DB_PATH 互相覆盖
    db.DB_PATH = os.path.join(_tmpdir, "delivery.db")
    db.init_db()
    # raise_server_exceptions=False：模拟真实 HTTP 服务，注入的 500 能作为响应返回
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db(client):
    """每个用例从空库开始，避免版本/交付历史互相污染断言。"""
    conn = db.connect()
    try:
        conn.execute("DELETE FROM delivery_impacts")
        conn.execute("DELETE FROM delivery_items")
        conn.execute("DELETE FROM deliveries")
        conn.execute("DELETE FROM exclusions")
        conn.execute("DELETE FROM results")
        conn.execute("DELETE FROM mappings")
        conn.execute("DELETE FROM lab_files")
        conn.execute("DELETE FROM versions")
        conn.execute("UPDATE meta SET current_version = NULL WHERE id = 1")
    finally:
        conn.close()
    yield


def make_csv(rows, header=("sample", "pb")):
    buf = io.StringIO()
    buf.write(",".join(header) + "\n")
    for r in rows:
        buf.write(",".join(str(x) for x in r) + "\n")
    return buf.getvalue().encode("utf-8")


def current_version(client):
    return client.get("/api/state").json()["version"]


def submit(client, rows_a, rows_b, coeff_a="1", coeff_b="1"):
    v = current_version(client)
    data = {
        "sample_col_a": "sample", "value_col_a": "pb", "coefficient_a": coeff_a,
        "sample_col_b": "sample", "value_col_b": "pb", "coefficient_b": coeff_b,
    }
    if v is not None:
        data["expected_version"] = str(v)
    return client.post(
        "/api/submit", data=data,
        files={
            "file_a": ("a.csv", make_csv(rows_a), "text/csv"),
            "file_b": ("b.csv", make_csv(rows_b), "text/csv"),
        },
    )


def remap(client, coeff_a="1", coeff_b="1", expected=None):
    v = current_version(client) if expected is None else expected
    return client.post("/api/remap", json={
        "expected_version": v,
        "mappings": [
            {"lab": "A", "sample_column": "sample", "value_column": "pb",
             "coefficient": coeff_a},
            {"lab": "B", "sample_column": "sample", "value_column": "pb",
             "coefficient": coeff_b},
        ],
    })


def register(client, version=None, expected="__auto__", note=""):
    v = current_version(client) if version is None else version
    if expected == "__auto__":
        expected = current_version(client)
    data = {"version": str(v)}
    if expected is not None:
        data["expected_version"] = str(expected)
    if note:
        data["note"] = note
    return client.post("/api/deliveries", data=data)


def detail(client, delivery_id):
    return client.get(f"/api/deliveries/{delivery_id}").json()


def impact_by_sample(d, sample_id):
    return [x for x in d["impacts"] if x["sample_id"] == sample_id]


@pytest.fixture
def base(client):
    """建立一个已知基线版本并登记交付。"""
    r = submit(client, [("S1", "0.10"), ("S2", "0.20")],
               [("S1", "0.10"), ("S2", "0.20")])
    assert r.status_code == 200, r.text
    v = r.json()["version"]
    reg = register(client, version=v, note="首版交付")
    assert reg.status_code == 200, reg.text
    return {"version": v, "delivery_id": reg.json()["delivery"]["id"]}


def test_register_returns_frozen_items_and_same_version_retry_reuses_id(client, base):
    d = detail(client, base["delivery_id"])
    assert d["delivery"]["version"] == base["version"]
    assert d["delivery"]["status"] == "valid"
    assert d["delivery"]["is_version_current"] is True
    assert {i["sample_id"] for i in d["items"]} == {"S1", "S2"}
    # 同版重试：沿用交付号，不新增交付
    r2 = register(client, version=base["version"])
    assert r2.status_code == 200
    assert r2.json()["delivery"]["id"] == base["delivery_id"]
    listed = client.get("/api/deliveries").json()["deliveries"]
    assert [x["id"] for x in listed] == [base["delivery_id"]]


def test_expired_version_cannot_register_new_delivery(client, base):
    # 推进版本，使 v2 过期且从未交付
    r = remap(client)
    assert r.status_code == 200
    v2 = r.json()["version"]
    r2 = remap(client)
    assert r2.status_code == 200
    reg = register(client, version=v2, expected=current_version(client))
    assert reg.status_code == 409
    assert "不能新登记交付" in reg.json()["detail"]
    # 不带所见版本号的新登记同样拒绝
    reg_missing = register(client, version=v2, expected=None)
    assert reg_missing.status_code == 409
    assert "expected_version" in reg_missing.json()["detail"]
    # 已登记过的旧版本同版重试：沿用交付号，不受过期影响
    reg2 = register(client, version=base["version"], expected=base["version"])
    assert reg2.status_code == 200
    assert reg2.json()["delivery"]["id"] == base["delivery_id"]
    # 已登记过的旧版本重试仍可返回原交付号
    ok = register(client, version=base["version"], expected=current_version(client))
    assert ok.status_code == 200
    assert ok.json()["delivery"]["version"] == base["version"]


def test_version_only_change_keeps_delivery_valid(client, base):
    # 仅改映射但结果行完全相同 -> 版号变化、交付行不变，仍有效且不产生影响
    r = remap(client)
    assert r.status_code == 200
    assert r.json()["version"] != base["version"]
    d = detail(client, base["delivery_id"])
    assert d["delivery"]["status"] == "valid"
    assert d["delivery"]["is_version_current"] is False
    assert d["impacts"] == []
    s = client.get(f"/api/state?version={base['version']}").json()
    assert s["stats"]["matched"] == 2


def test_changed_value_records_first_old_new_and_sets_recall(client, base):
    # 只改乙系数 1.1：S1 0.10->0.11、S2 0.20->0.22，仍纳入 -> changed
    r = remap(client, coeff_b="1.1")
    assert r.status_code == 200
    v = r.json()["version"]
    d = detail(client, base["delivery_id"])
    assert d["delivery"]["status"] == "needs_recall"
    for sid in ("S1", "S2"):
        imp = impact_by_sample(d, sid)
        assert len(imp) == 1 and imp[0]["kind"] == "changed"
        assert imp[0]["first_version"] == v
        assert imp[0]["new_mean"] is not None
        assert imp[0]["new_row_a"] == imp[0]["old_row_a"]
        assert imp[0]["resolved_version"] is None
    s1 = impact_by_sample(d, "S1")[0]
    assert s1["old_mean"] == "0.10" and s1["new_mean"] == "0.105"
    assert s1["old_value_b"] == "0.10" and s1["new_value_b"] == "0.110"

    # 再改一次（仍受影响）：保留“首次影响”记录，不重复
    r2 = remap(client, coeff_b="1.2")
    assert r2.status_code == 200
    d2 = detail(client, base["delivery_id"])
    assert len(impact_by_sample(d2, "S1")) == 1
    assert impact_by_sample(d2, "S1")[0]["new_mean"] == "0.105"
    assert d2["delivery"]["status"] == "needs_recall"


def test_removed_sample_records_none_new_row(client, base):
    # 乙系数 2：S1 0.20 与 0.10 差 0.10 超差，S2 0.40 与 0.20 差 0.20 超差 -> 均不再纳入
    r = remap(client, coeff_b="2")
    assert r.status_code == 200
    v = r.json()["version"]
    d = detail(client, base["delivery_id"])
    assert d["delivery"]["status"] == "needs_recall"
    for sid in ("S1", "S2"):
        imp = impact_by_sample(d, sid)
        assert len(imp) == 1 and imp[0]["kind"] == "removed"
        assert imp[0]["first_version"] == v
        assert imp[0]["new_mean"] is None and imp[0]["new_row_a"] is None
        assert imp[0]["old_mean"] is not None


def test_restore_keeps_record_and_clears_recall(client, base):
    remap(client, coeff_b="2")
    assert detail(client, base["delivery_id"])["delivery"]["status"] == "needs_recall"
    # 恢复到交付基线：记录补 resolved_*，状态回到 valid，记录保留
    r = remap(client, coeff_b="1")
    assert r.status_code == 200
    v = r.json()["version"]
    d = detail(client, base["delivery_id"])
    assert d["delivery"]["status"] == "valid"
    assert len(d["impacts"]) == 2
    for imp in d["impacts"]:
        assert imp["resolved_version"] == v
        assert imp["resolved_at"]
        assert imp["resolved_mean"] is not None
    s1 = impact_by_sample(d, "S1")[0]
    assert s1["resolved_mean"] == "0.10"
    assert s1["resolved_row_a"] == 2 and s1["resolved_row_b"] == 2

    # 恢复后再次受影响：另起一条记录，历史仍保留
    r2 = remap(client, coeff_b="2")
    assert r2.status_code == 200
    d2 = detail(client, base["delivery_id"])
    s1_impacts = impact_by_sample(d2, "S1")
    assert len(s1_impacts) == 2
    assert s1_impacts[0]["resolved_version"] == v
    assert s1_impacts[1]["resolved_version"] is None
    assert d2["delivery"]["status"] == "needs_recall"


def test_row_number_change_marks_recall(client, base):
    # 重新提交文件：S2 移到前面（两侧行号均变化），值不变
    r = submit(client, [("S2", "0.20"), ("S1", "0.10")],
               [("S2", "0.20"), ("S1", "0.10")])
    assert r.status_code == 200
    v = r.json()["version"]
    d = detail(client, base["delivery_id"])
    assert d["delivery"]["status"] == "needs_recall"
    s1 = impact_by_sample(d, "S1")[0]
    assert (s1["old_row_a"], s1["old_row_b"]) == (2, 2)
    assert (s1["new_row_a"], s1["new_row_b"]) == (3, 3)
    assert s1["old_mean"] == s1["new_mean"] == "0.10"
    assert s1["first_version"] == v


def test_equal_decimal_spelling_is_not_a_change(client, base):
    # 0.10 与 0.100 数值等价，且行号不变 -> 不得误报影响
    r = submit(client, [("S1", "0.100"), ("S2", "0.200")],
               [("S1", "0.100"), ("S2", "0.200")])
    assert r.status_code == 200
    d = detail(client, base["delivery_id"])
    assert d["delivery"]["status"] == "valid"
    assert d["impacts"] == []


def test_new_samples_do_not_affect_existing_delivery(client, base):
    # 新增样品只出现在新版本，不改变交付原先纳入的样品 -> 交付保持有效
    r = submit(client, [("S1", "0.10"), ("S2", "0.20"), ("S3", "0.30")],
               [("S1", "0.10"), ("S2", "0.20"), ("S3", "0.30")])
    assert r.status_code == 200
    assert r.json()["stats"]["matched"] == 3
    d = detail(client, base["delivery_id"])
    assert d["delivery"]["status"] == "valid"
    assert d["impacts"] == []
    assert {i["sample_id"] for i in d["items"]} == {"S1", "S2"}


def test_register_nonexistent_version_is_404(client, base):
    r = register(client, version=999999, expected=current_version(client))
    assert r.status_code == 404
    # 也不产生任何交付
    assert len(client.get("/api/deliveries").json()["deliveries"]) == 1


def test_init_db_is_idempotent_for_existing_volume(client):
    """已有数据库再次 init 不会破坏数据（新表为 IF NOT EXISTS，可直接升级旧卷）。"""
    v = current_version(client)
    db.init_db()
    db.init_db()
    assert current_version(client) == v


def test_registration_is_atomic_with_caller_transaction(client, base):
    """登记在调用方事务内，回滚后不留下任何交付/交付行。"""
    r = remap(client, coeff_b="1.0001")
    assert r.status_code == 200
    v = r.json()["version"]

    conn = db.connect()
    try:
        db.begin_immediate(conn)
        did = db.register_delivery(conn, v, v, "临时")
        assert db.get_delivery(conn, did) is not None
        db.rollback(conn)
    finally:
        conn.close()
    assert client.get(f"/api/deliveries/{did}").status_code == 404
    assert all(x["id"] != did for x in client.get("/api/deliveries").json()["deliveries"])
    # 回滚不影响版本本身，随后仍可正常登记（回滚后自增号可能被复用，属正常语义）
    reg = register(client, version=v)
    assert reg.status_code == 200
    assert reg.json()["delivery"]["version"] == v
    assert reg.json()["delivery"]["status"] == "valid"


def test_delivery_download_is_frozen_snapshot(client, base):
    dl = client.get(f"/api/deliveries/{base['delivery_id']}/download")
    assert dl.status_code == 200
    text = dl.content.decode("utf-8-sig")
    assert "S1,0.10,0.10,0.10,2,2" in text
    assert "S2,0.20,0.20,0.20,3,3" in text
    # 推进版本后，交付下载仍为交付当时内容
    remap(client, coeff_b="2")
    dl2 = client.get(f"/api/deliveries/{base['delivery_id']}/download")
    assert dl2.content == dl.content
    assert client.get("/api/deliveries/999999").status_code == 404
    assert client.get("/api/deliveries/999999/download").status_code == 404


def test_status_and_impact_records_stay_in_sync(client, base):
    remap(client, coeff_b="2")
    remap(client, coeff_b="1")
    remap(client, coeff_b="1.1")
    summaries = client.get("/api/deliveries").json()["deliveries"]
    for summary in summaries:
        d = detail(client, summary["id"])
        assert d["delivery"]["status"] == summary["status"]
        open_count = sum(1 for x in d["impacts"] if x["resolved_version"] is None)
        assert summary["open_impacts"] == open_count
        assert (d["delivery"]["status"] == "needs_recall") == (open_count > 0)


def test_failure_during_impact_write_rolls_back_whole_version(client, base, monkeypatch):
    """影响记录写入中途失败：版本不创建、交付状态与记录均不留下半份。"""
    before_state = client.get("/api/state").json()
    before_detail = detail(client, base["delivery_id"])
    before_summaries = client.get("/api/deliveries").json()["deliveries"]
    conn = db.connect()
    try:
        before_versions = conn.execute("SELECT COUNT(*) c FROM versions").fetchone()["c"]
    finally:
        conn.close()

    real_refresh = db._refresh_delivery_status

    def boom(conn, delivery_id, touched):
        if touched:
            raise RuntimeError("injected impact write failure")
        return real_refresh(conn, delivery_id, touched)

    monkeypatch.setattr(db, "_refresh_delivery_status", boom)
    r = remap(client, coeff_b="2")
    assert r.status_code == 500
    monkeypatch.undo()

    after_state = client.get("/api/state").json()
    after_detail = detail(client, base["delivery_id"])
    after_summaries = client.get("/api/deliveries").json()["deliveries"]
    assert after_state["version"] == before_state["version"]
    assert after_detail == before_detail
    assert after_summaries == before_summaries
    # 失败后失败提交自身也不得留下版本
    conn = db.connect()
    try:
        n_versions = conn.execute("SELECT COUNT(*) c FROM versions").fetchone()["c"]
        latest = conn.execute("SELECT MAX(id) m FROM versions").fetchone()["m"]
        n_impacts = conn.execute("SELECT COUNT(*) c FROM delivery_impacts").fetchone()["c"]
    finally:
        conn.close()
    assert latest == before_state["version"]
    assert n_versions == before_versions
    assert n_impacts == len(before_detail["impacts"])

    # 失败后系统仍可正常创建下一版本并同步
    ok = remap(client, coeff_b="2")
    assert ok.status_code == 200
    assert detail(client, base["delivery_id"])["delivery"]["status"] == "needs_recall"


def test_concurrent_register_and_remap_never_leaves_stale_delivery(client, base):
    """并发登记与修订：要么登记成功并同步影响，要么登记 409；不存在过期交付。"""
    import concurrent.futures

    v = current_version(client)
    # 用新版本作为竞态起点，且该版本尚未登记交付
    r = remap(client, coeff_b="1.0001")
    assert r.status_code == 200
    v = r.json()["version"]

    def do_register(_):
        return register(client, version=v, expected=v)

    def do_remap(_):
        return remap(client, coeff_b="2", expected=v)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(do_register, 0)
        f2 = pool.submit(do_remap, 1)
        reg, rem = f1.result(), f2.result()

    if reg.status_code == 200:
        assert rem.status_code == 200
        d = detail(client, reg.json()["delivery"]["id"])
        # 若修订落库，状态必须已同步：needs_recall 时必有一条未恢复影响
        if d["delivery"]["is_version_current"]:
            open_count = sum(1 for x in d["impacts"] if x["resolved_version"] is None)
            assert (d["delivery"]["status"] == "needs_recall") == (open_count > 0)
            assert d["delivery"]["status"] == "needs_recall"
    else:
        assert reg.status_code == 409
        # 登记失败则当前版本必然已不是登记所见版本
        assert current_version(client) != v
    # 无论竞态如何，当前版本唯一且交付号唯一
    st = client.get("/api/state").json()
    assert st["version"] is not None
    ids = [x["id"] for x in client.get("/api/deliveries").json()["deliveries"]]
    assert len(ids) == len(set(ids))
