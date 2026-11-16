"""核心计算逻辑的单元测试（Decimal 精确性、重复拒收、行号、公差边界）。"""
from decimal import Decimal

import pytest

from app.logic import (
    TOLERANCE,
    ValidationError,
    compute_results,
    format_decimal,
    parse_csv,
    validate_coefficient,
    validate_lab,
)


def csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def test_parse_csv_keeps_physical_line_numbers_with_blank_and_quoted_newline():
    content = csv_bytes(
        '样品号,铅\n'
        '\n'                       # 空行（物理第 2 行）
        'S1,0.1\n'
        '"S\n2",0.2\n'            # 带引号换行，物理占两行
        'S3,0.3\n'
    )
    headers, rows = parse_csv(content)
    assert headers == ["样品号", "铅"]
    line_nos = [ln for ln, _ in rows]
    assert line_nos == [3, 5, 6]  # 空行与跨行字段都不导致错位


def test_duplicate_sample_id_rejects_whole_file():
    content = csv_bytes("id,v\nS1,1\nS2,2\nS1,3\n")
    with pytest.raises(ValidationError, match="S1.*重复"):
        validate_lab(content, "id", "v", "1")


def test_value_and_sample_columns_must_differ():
    content = csv_bytes("id,v\nS1,1\n")
    with pytest.raises(ValidationError, match="不能选择同一列"):
        validate_lab(content, "id", "id", "1")


def test_missing_mapped_column_reports_available_headers():
    content = csv_bytes("id,v\nS1,1\n")
    with pytest.raises(ValidationError, match="不存在"):
        validate_lab(content, "nope", "v", "1")


def test_invalid_numeric_value_rejected_with_line_number():
    content = csv_bytes("id,v\nS1,1\nS2,abc\n")
    with pytest.raises(ValidationError, match="第 3 行"):
        validate_lab(content, "id", "v", "1")


def test_row_limit_enforced():
    lines = ["id,v"] + [f"S{i},{i}" for i in range(501)]
    content = csv_bytes("\n".join(lines) + "\n")
    with pytest.raises(ValidationError, match="超过上限 500"):
        validate_lab(content, "id", "v", "1")


def test_coefficient_rules():
    assert validate_coefficient(" 0.001 ") == Decimal("0.001")
    for bad in ("0", "-1", "abc", "", "1e999"):
        with pytest.raises(ValidationError):
            validate_coefficient(bad)


def _parsed(content, sc="id", vc="v", coeff="1"):
    return validate_lab(csv_bytes(content), sc, vc, coeff)


def test_coefficient_conversion_and_tolerance_boundary_exact():
    # 甲单位 mg/kg，乙单位 0.01 mg/kg（系数 0.01）：10 -> 0.10，与 0.05 差 0.05，恰好通过
    a, ca = _parsed("id,v\nS1,0.10\n", coeff="1")
    b, cb = _parsed("id,v\nS1,10\n", coeff="0.01")
    matched, excluded = compute_results(a, ca, b, cb)
    assert len(matched) == 1 and not excluded
    m = matched[0]
    assert format_decimal(m.value_a) == "0.10"
    assert format_decimal(m.value_b) == "0.10"
    assert format_decimal(m.mean) == "0.10"
    assert (m.row_a, m.row_b) == (2, 2)


def test_diff_just_over_tolerance_excluded():
    # 0.1001 vs 0.05，差 0.0501 > 0.05，拒收并给出原因
    a, ca = _parsed("id,v\nS1,0.1001\n")
    b, cb = _parsed("id,v\nS1,0.05\n")
    matched, excluded = compute_results(a, ca, b, cb)
    assert not matched
    assert len(excluded) == 1
    assert "超过允许差值" in excluded[0].reason
    assert excluded[0].row_a == 2 and excluded[0].row_b == 2


def test_missing_each_side_excluded_with_reason_and_line():
    a, ca = _parsed("id,v\nS1,0.1\nS2,0.2\n")
    b, cb = _parsed("id,v\nS1,0.1\nS3,0.3\n")
    matched, excluded = compute_results(a, ca, b, cb)
    assert [m.sample_id for m in matched] == ["S1"]
    reasons = {x.sample_id: x.reason for x in excluded}
    assert "缺少乙" in reasons["S2"]
    assert "缺少甲" in reasons["S3"]
    s2 = next(x for x in excluded if x.sample_id == "S2")
    assert s2.row_a == 3 and s2.row_b is None
    s3 = next(x for x in excluded if x.sample_id == "S3")
    assert s3.row_a is None and s3.row_b == 3


def test_mean_decimal_arithmetic_no_float_artifacts():
    # 0.11/0.19 差值 0.08 超差；改用 0.12/0.18（差 0.06 仍超差），
    # 最终取 0.11/0.15（差 0.04 通过），均值精确为 0.13，
    # 不得出现 0.13000000000000001 之类的二进制浮点尾数
    a, ca = _parsed("id,v\nS1,0.11\n")
    b, cb = _parsed("id,v\nS1,0.15\n")
    matched, _ = compute_results(a, ca, b, cb)
    assert len(matched) == 1
    assert format_decimal(matched[0].mean) == "0.13"
    assert TOLERANCE == Decimal("0.05")


def test_ordering_follows_file_a_then_b_only_samples():
    a, ca = _parsed("id,v\nX,1\nY,1\n")
    b, cb = _parsed("id,v\nY,1\nZ,1\n")
    _, excluded = compute_results(a, ca, b, cb)
    assert [x.sample_id for x in excluded] == ["X", "Z"]


def test_gb18030_encoded_file_supported():
    content = "样品号,测值\n甲01,0.1\n".encode("gb18030")
    headers, rows = parse_csv(content)
    assert headers == ["样品号", "测值"]
    assert rows[0][1] == ["甲01", "0.1"]
