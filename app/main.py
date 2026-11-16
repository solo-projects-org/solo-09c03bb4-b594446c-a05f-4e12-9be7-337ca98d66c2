"""FastAPI 应用：上传映射配置、版本化重算、结果查询与快照下载。"""
from __future__ import annotations

import io
import csv
import json
from decimal import Decimal
from typing import Optional

from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import db
from .logic import (
    MAX_ROWS,
    ValidationError,
    compute_results,
    format_decimal,
    validate_lab,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="铅含量双实验室比对系统", version="1.0.0", lifespan=lifespan)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB，远大于 500 行 CSV 的合理体积


def _read_upload(file: UploadFile) -> bytes:
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"文件 {file.filename} 超过 20MB 上传限制")
    return content


def _parse_expected(raw: Optional[str]) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="expected_version 必须是整数或空")


def _validate_or_400(fn, *args):
    """把业务校验异常统一转为 400 响应。"""
    try:
        return fn(*args)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _version_payload(state: dict, current: Optional[int]) -> dict:
    return {
        "current_version": current,
        "version": state["version"],
        "is_current": current == state["version"],
        "created_at": state["created_at"],
        "reason": state["reason"],
        "note": state["note"],
        "mappings": state["mappings"],
        "files": state["files"],
        "results": state["results"],
        "exclusions": state["exclusions"],
        "stats": {
            "matched": len(state["results"]),
            "excluded": len(state["exclusions"]),
        },
    }


STATIC_INDEX = Path(__file__).resolve().parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(str(STATIC_INDEX))


@app.post("/api/preview")
async def preview(file: UploadFile = File(...)) -> JSONResponse:
    """仅解析表头与数据行数，用于网页动态生成列名下拉框，不落库。"""
    from .logic import parse_csv
    content = _read_upload(file)
    try:
        headers, rows = parse_csv(content)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({
        "filename": file.filename,
        "headers": headers,
        "data_rows": len(rows),
        "over_limit": len(rows) > MAX_ROWS,
        "max_rows": MAX_ROWS,
    })


@app.get("/api/state")
def state(version: Optional[int] = None) -> dict:
    conn = db.connect()
    try:
        current = db.get_current_version(conn)
        if current is None:
            return {"current_version": None, "version": None,
                    "results": [], "exclusions": [], "mappings": [],
                    "files": [], "stats": {"matched": 0, "excluded": 0}}
        target = version if version is not None else current
        s = db.get_version_state(conn, target)
        if s is None:
            raise HTTPException(status_code=404, detail=f"版本 v{target} 不存在")
        return _version_payload(s, current)
    finally:
        conn.close()


@app.post("/api/submit")
async def submit(
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
    sample_col_a: str = Form(...),
    value_col_a: str = Form(...),
    coefficient_a: str = Form(...),
    sample_col_b: str = Form(...),
    value_col_b: str = Form(...),
    coefficient_b: str = Form(...),
    expected_version: Optional[str] = Form(None),
) -> dict:
    """两份完整 CSV + 各自映射，一次性校验并原子创建新版本。"""
    expected = _parse_expected(expected_version)
    content_a = _read_upload(file_a)
    content_b = _read_upload(file_b)

    # 事务前完成全部解析与校验，非法输入绝不产生任何版本
    parsed_a, coeff_a = _validate_or_400(
        validate_lab, content_a, sample_col_a, value_col_a, coefficient_a
    )
    parsed_b, coeff_b = _validate_or_400(
        validate_lab, content_b, sample_col_b, value_col_b, coefficient_b
    )
    matched, excluded = compute_results(parsed_a, coeff_a, parsed_b, coeff_b)

    data = db.VersionInput(
        reason="submit",
        note=None,
        files={
            "A": (file_a.filename, content_a, parsed_a.headers),
            "B": (file_b.filename, content_b, parsed_b.headers),
        },
        mappings={
            "A": (sample_col_a.strip(), value_col_a.strip(), coeff_a),
            "B": (sample_col_b.strip(), value_col_b.strip(), coeff_b),
        },
        matched=matched,
        excluded=excluded,
    )
    conn = db.connect()
    try:
        # 整个版本切换在同一 IMMEDIATE 事务内，并发提交在此序列化
        db.begin_immediate(conn)
        try:
            vid = db.create_version(conn, data, expected)
            db.commit(conn)
        except db.StaleVersionError as exc:
            db.rollback(conn)
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception:
            db.rollback(conn)
            raise
        return _version_payload(db.get_version_state(conn, vid), db.get_current_version(conn))
    finally:
        conn.close()


class MappingSpec(BaseModel):
    lab: str = Field(pattern="^(A|B)$")
    sample_column: str
    value_column: str
    coefficient: str


class RemapRequest(BaseModel):
    mappings: list[MappingSpec]
    expected_version: int


@app.post("/api/remap")
def remap(req: RemapRequest) -> dict:
    """只改映射（系数/列名），从两个原始文件重新解析、计算并整体切换版本。"""
    if len(req.mappings) != 2 or {m.lab for m in req.mappings} != {"A", "B"}:
        raise HTTPException(status_code=400, detail="必须同时提供甲(A)、乙(B)两个实验室的映射")
    spec = {m.lab: m for m in req.mappings}

    conn = db.connect()
    try:
        # 先取写锁再做版本检查，消除“检查-提交”之间的 TOCTOU 窗口
        db.begin_immediate(conn)
        try:
            current = db.get_current_version(conn)
            if current is None:
                db.rollback(conn)
                raise HTTPException(status_code=404, detail="尚无任何版本，请先上传文件提交")
            if current != req.expected_version:
                db.rollback(conn)
                raise HTTPException(
                    status_code=409,
                    detail=f"提交基于 v{req.expected_version}，但当前版本已变为 v{current}",
                )

            loaded: dict[str, tuple] = {}
            for lab in ("A", "B"):
                info = db.get_lab_content(conn, current, lab)
                if info is None:
                    raise HTTPException(status_code=500,
                                        detail=f"版本 v{current} 缺少实验室 {lab} 的原始文件")
                loaded[lab] = info

            try:
                parsed: dict[str, tuple] = {}
                for lab in ("A", "B"):
                    filename, content, sc, vc, _old_coeff = loaded[lab]
                    m = spec[lab]
                    parsed[lab] = validate_lab(
                        content, m.sample_column, m.value_column, m.coefficient
                    )
            except ValidationError as exc:
                db.rollback(conn)
                raise HTTPException(status_code=400, detail=str(exc))

            (parsed_a, coeff_a) = parsed["A"]
            (parsed_b, coeff_b) = parsed["B"]
            matched, excluded = compute_results(parsed_a, coeff_a, parsed_b, coeff_b)

            data = db.VersionInput(
                reason="remap",
                note=json.dumps(
                    {m.lab: {"sample_column": m.sample_column,
                             "value_column": m.value_column,
                             "coefficient": m.coefficient} for m in req.mappings},
                    ensure_ascii=False,
                ),
                files={
                    "A": (loaded["A"][0], loaded["A"][1], parsed_a.headers),
                    "B": (loaded["B"][0], loaded["B"][1], parsed_b.headers),
                },
                mappings={
                    "A": (spec["A"].sample_column.strip(),
                          spec["A"].value_column.strip(), coeff_a),
                    "B": (spec["B"].sample_column.strip(),
                          spec["B"].value_column.strip(), coeff_b),
                },
                matched=matched,
                excluded=excluded,
            )
            try:
                vid = db.create_version(conn, data, current)
                db.commit(conn)
            except db.StaleVersionError as exc:
                db.rollback(conn)
                raise HTTPException(status_code=409, detail=str(exc))
        except HTTPException:
            # 已在分支内处理过回滚的业务异常直接上抛
            raise
        except Exception:
            db.rollback(conn)
            raise
        return _version_payload(db.get_version_state(conn, vid), db.get_current_version(conn))
    finally:
        conn.close()


@app.get("/api/download")
def download(version: Optional[int] = None) -> Response:
    """按指定版本（默认当前版本）导出其快照 CSV，下载内容不跨版本混用。"""
    conn = db.connect()
    try:
        current = db.get_current_version(conn)
        if current is None:
            raise HTTPException(status_code=404, detail="尚无任何结果可下载")
        target = version if version is not None else current
        s = db.get_version_state(conn, target)
        if s is None:
            raise HTTPException(status_code=404, detail=f"版本 v{target} 不存在")

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "样品号", "铅含量均值(mg/kg)",
            "甲换算值(mg/kg)", "乙换算值(mg/kg)",
            "甲原行号", "乙原行号",
        ])
        for r in s["results"]:
            writer.writerow([
                r["sample_id"], r["mean"], r["value_a"], r["value_b"],
                r["row_a"], r["row_b"],
            ])
        # utf-8-sig：带 BOM，Excel 直接打开不乱码
        payload = b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
        return Response(
            content=payload,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="lead_results_v{target}.csv"'
                )
            },
        )
    finally:
        conn.close()


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok"}
