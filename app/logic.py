"""纯计算逻辑：CSV 解析、单文件校验、双实验室结果比对。

所有数值使用 Decimal 精确运算，避免浮点误差影响 0.05 mg/kg 的临界判定。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

MAX_ROWS = 500
TOLERANCE = Decimal("0.05")


class ValidationError(Exception):
    """输入数据或映射配置不合法，整份提交应被拒收。"""


@dataclass
class ParsedRow:
    line_no: int          # 原文件中的物理行号（含表头，从 1 开始）
    sample_id: str
    value: Decimal


@dataclass
class ParsedFile:
    headers: list[str]
    rows: list[ParsedRow]


@dataclass
class MatchResult:
    sample_id: str
    mean: Decimal
    value_a: Decimal
    value_b: Decimal
    row_a: int
    row_b: int


@dataclass
class Excluded:
    sample_id: str
    reason: str
    value_a: Optional[Decimal]
    value_b: Optional[Decimal]
    row_a: Optional[int]
    row_b: Optional[int]


def format_decimal(d: Decimal) -> str:
    """以普通十进制字符串输出 Decimal（不使用科学计数法）。"""
    s = str(d)
    if "E" in s or "e" in s:
        s = format(d, "f")
    return s


def decode_bytes(content: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValidationError("文件编码无法识别（支持 UTF-8/GB18030）")


def parse_csv(content: bytes) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """解析 CSV，返回 (表头, [(物理行号, 字段列表), ...])。

    物理行号用 csv.reader.line_num 取得，空行也计入行号，
    因此带引号的跨行字段与空行都不会导致行号错位。
    """
    if not content or not content.strip():
        raise ValidationError("CSV 文件为空")
    text = decode_bytes(content)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValidationError("CSV 文件缺少表头")
    headers = [h.strip() for h in header]
    if any(h == "" for h in headers):
        raise ValidationError("表头中存在空列名")
    if len(set(headers)) != len(headers):
        raise ValidationError("表头中存在重复列名")
    raw_rows: list[tuple[int, list[str]]] = []
    for row in reader:
        # 完全空白的行跳过；line_num 已经计入该物理行，行号不会错位
        if not row or all((c or "").strip() == "" for c in row):
            continue
        raw_rows.append((reader.line_num, row))
    return headers, raw_rows


def validate_coefficient(raw: str) -> Decimal:
    if raw is None or str(raw).strip() == "":
        raise ValidationError("换算系数不能为空")
    try:
        coeff = Decimal(str(raw).strip())
    except InvalidOperation:
        raise ValidationError(f"换算系数 {raw!r} 不是合法数字")
    if not coeff.is_finite():
        raise ValidationError("换算系数必须为有限数字")
    if coeff <= 0:
        raise ValidationError("换算系数必须大于 0")
    # 拒绝指数写法与位数过长的系数，避免换算结果出现科学计数法
    normalized = format_decimal(coeff)
    if "e" in str(raw).strip().lower() or len(normalized.replace("-", "").replace(".", "")) > 30:
        raise ValidationError("换算系数请使用普通十进制写法（不支持科学计数法）")
    return coeff


def validate_lab(
    content: bytes,
    sample_column: str,
    value_column: str,
    coefficient: str,
) -> tuple[ParsedFile, Decimal]:
    """校验单个实验室文件及其映射配置，返回解析结果与系数。"""
    coeff = validate_coefficient(coefficient)
    sample_column = (sample_column or "").strip()
    value_column = (value_column or "").strip()
    if not sample_column or not value_column:
        raise ValidationError("样品号列与测值列必须指定")
    if sample_column == value_column:
        raise ValidationError("样品号列与测值列不能选择同一列")

    headers, raw_rows = parse_csv(content)
    if sample_column not in headers:
        raise ValidationError(
            f"样品号列 {sample_column!r} 不存在，可用列：{', '.join(headers)}"
        )
    if value_column not in headers:
        raise ValidationError(
            f"测值列 {value_column!r} 不存在，可用列：{', '.join(headers)}"
        )
    if len(raw_rows) == 0:
        raise ValidationError("文件没有任何数据行")
    if len(raw_rows) > MAX_ROWS:
        raise ValidationError(f"数据行数 {len(raw_rows)} 超过上限 {MAX_ROWS} 行")

    idx_s = headers.index(sample_column)
    idx_v = headers.index(value_column)
    seen: dict[str, int] = {}
    rows: list[ParsedRow] = []
    for line_no, row in raw_rows:
        if max(idx_s, idx_v) >= len(row):
            raise ValidationError(f"第 {line_no} 行列数不足，缺少映射字段")
        sid = row[idx_s].strip()
        raw_val = row[idx_v].strip()
        if not sid:
            raise ValidationError(f"第 {line_no} 行样品号为空")
        try:
            val = Decimal(raw_val)
        except InvalidOperation:
            raise ValidationError(f"第 {line_no} 行测值 {raw_val!r} 不是合法数字")
        if not val.is_finite():
            raise ValidationError(f"第 {line_no} 行测值必须为有限数字（不允许 NaN/Infinity）")
        if sid in seen:
            raise ValidationError(
                f"样品号 {sid!r} 在同一文件内重复"
                f"（第 {seen[sid]} 行与第 {line_no} 行），整份文件拒收"
            )
        seen[sid] = line_no
        rows.append(ParsedRow(line_no, sid, val))
    return ParsedFile(headers, rows), coeff


def compute_results(
    a: ParsedFile,
    coeff_a: Decimal,
    b: ParsedFile,
    coeff_b: Decimal,
) -> tuple[list[MatchResult], list[Excluded]]:
    """按业务规则比对：双方均上报且换算差 <= 0.05 才取均值。"""
    map_a = {r.sample_id: r for r in a.rows}
    map_b = {r.sample_id: r for r in b.rows}
    # 并集顺序：先甲文件顺序，再乙中独有的样品
    ordered = list(
        dict.fromkeys([r.sample_id for r in a.rows] + [r.sample_id for r in b.rows])
    )
    matched: list[MatchResult] = []
    excluded: list[Excluded] = []
    for sid in ordered:
        ra = map_a.get(sid)
        rb = map_b.get(sid)
        if ra is not None and rb is None:
            excluded.append(Excluded(
                sample_id=sid,
                reason="仅甲实验室上报，缺少乙实验室数据",
                value_a=ra.value * coeff_a,
                value_b=None,
                row_a=ra.line_no,
                row_b=None,
            ))
            continue
        if rb is not None and ra is None:
            excluded.append(Excluded(
                sample_id=sid,
                reason="仅乙实验室上报，缺少甲实验室数据",
                value_a=None,
                value_b=rb.value * coeff_b,
                row_a=None,
                row_b=rb.line_no,
            ))
            continue
        assert ra is not None and rb is not None
        va = ra.value * coeff_a
        vb = rb.value * coeff_b
        diff = abs(va - vb)
        if diff <= TOLERANCE:
            matched.append(MatchResult(
                sample_id=sid,
                mean=(va + vb) / 2,
                value_a=va,
                value_b=vb,
                row_a=ra.line_no,
                row_b=rb.line_no,
            ))
        else:
            excluded.append(Excluded(
                sample_id=sid,
                reason=(
                    f"十进制换算后差值 {format_decimal(diff)} mg/kg，"
                    f"超过允许差值 0.05 mg/kg"
                ),
                value_a=va,
                value_b=vb,
                row_a=ra.line_no,
                row_b=rb.line_no,
            ))
    return matched, excluded
