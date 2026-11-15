"""FastAPI 入口：候选索引成本评估（/evaluate）与列更名预览（/rename-preview）。

两组接口均为只读：
- 评估接口查询不执行（EXPLAIN 无 ANALYZE），索引不落地（HypoPG 会话级假想）；
- 更名预览只读取 information_schema 列目录，提交的 SQL 仅做静态分析，不执行、不改表。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

from .config import get_settings
from .db import TwoConnectionPool
from .samples import SAMPLES
from .schemas import (
    EvaluateRequest,
    EvaluateResponse,
    HealthResponse,
    RenamePreviewRequest,
    RenamePreviewResponse,
)
from .rename_service import RenamePreviewError, preview_column_rename
from .service import EvaluationError, evaluate
from .validation import SQLValidationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("hypopg_api")

settings = get_settings()
pool = TwoConnectionPool(settings)

app = FastAPI(
    title="夜间包裹扫描查询 · 索引评估与列更名预览 API",
    description=(
        "两组只读能力：\n"
        "1. `/evaluate`：提交只读 SELECT 与最多 3 个 B-tree 候选索引，"
        "基于 PostgreSQL + HypoPG 返回 EXPLAIN JSON 与成本对比（只估算，不执行、不建真实索引）；\n"
        "2. `/rename-preview`：为 public.package_scan 的列更名预览夜间报表 SQL 改写，"
        "只读取列目录、不执行提交的查询、不修改表；星号/歧义/不支持结构逐条标记人工处理。"
    ),
    version="1.1.0",
)


@app.on_event("startup")
async def _startup() -> None:
    await pool.open()
    log.info("连接池已就绪，物理连接上限 = %s", settings.pool_max_connections)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await pool.close()
    log.info("连接池已关闭")


@app.exception_handler(SQLValidationError)
async def _sql_validation_handler(_: Request, exc: SQLValidationError):
    return JSONResponse(status_code=400, content={"error": "invalid_sql", "detail": str(exc)})


@app.exception_handler(EvaluationError)
async def _evaluation_handler(_: Request, exc: EvaluationError):
    return JSONResponse(status_code=400, content={"error": "evaluation_rejected", "detail": str(exc)})


@app.exception_handler(RenamePreviewError)
async def _rename_handler(_: Request, exc: RenamePreviewError):
    return JSONResponse(status_code=400, content={"error": "rename_preview_rejected", "detail": str(exc)})


@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health() -> HealthResponse:
    """存活探针 + 连接池观测（演示两条连接上限与复用/丢弃计数）。"""
    db_info: dict = {"connected": False}
    try:
        async with pool.lease() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT current_database() AS db, current_user AS usr, "
                    "       (SELECT extversion FROM pg_extension WHERE extname='hypopg') AS hypopg"
                )
                db_info = {"connected": True, **(await cur.fetchone())}
    except Exception as exc:  # 健康检查不应抛 500 掩盖状态
        db_info = {"connected": False, "error": repr(exc)}
    return HealthResponse(status="ok", pool=pool.stats(), db=db_info)


@app.get("/samples", tags=["evaluation"])
async def list_samples() -> dict:
    """预置样例 SQL 及能/不能改善计划的候选示例。"""
    return {
        "samples": [
            {
                "sample_id": s.sample_id,
                "title": s.title,
                "sql_text": s.sql_text,
                "helpful_candidates": [c.__dict__ for c in s.helpful_candidates],
                "useless_candidates": [c.__dict__ for c in s.useless_candidates],
            }
            for s in SAMPLES
        ]
    }


@app.post(
    "/evaluate",
    response_model=EvaluateResponse,
    tags=["evaluation"],
    responses={
        400: {"description": "SQL 非法、表/列不存在或候选参数错误（不返回部分比较）"},
        500: {"description": "数据库内部错误；连接已清理后归还或丢弃"},
    },
)
async def evaluate_query(req: EvaluateRequest, request: Request) -> EvaluateResponse:
    """执行一次完整的基线 vs 全候选成本评估。

    客户端在处理中断连时，服务端仍会等待服务端查询取消、事务回滚与
    hypopg_reset 全部完成后才归还连接（详见连接池实现）。
    """
    request_id = uuid.uuid4().hex
    pause_seconds = 0.0
    raw_pause = request.headers.get("x-demo-pause-seconds")
    if raw_pause:
        try:
            pause_seconds = float(raw_pause)
            if pause_seconds < 0:
                raise ValueError
        except ValueError:
            raise SQLValidationError(
                "X-Demo-Pause-Seconds 必须是非负数字（单位秒）"
            )
    pause_mode = request.headers.get("x-demo-pause-mode", "asyncio").lower()
    if pause_mode not in ("asyncio", "pg_sleep"):
        raise SQLValidationError(
            "X-Demo-Pause-Mode 只允许 asyncio 或 pg_sleep"
        )

    log.info(
        "评估请求 %s 开始：候选数=%d 演示暂停=%.1fs 模式=%s",
        request_id, len(req.candidates), pause_seconds, pause_mode,
    )
    try:
        result = await evaluate(
            pool, settings, req,
            pause_seconds=pause_seconds,
            pause_mode=pause_mode,
            is_disconnected=request.is_disconnected,
        )
    except (SQLValidationError, EvaluationError):
        raise
    except Exception as exc:
        # 失败不返回部分比较；租约上下文已保证清理完成
        log.exception("评估请求 %s 失败: %r", request_id, exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": "evaluation_failed",
                "request_id": request_id,
                "detail": "数据库评估失败，未产生比较结果；连接已完成清理",
            },
        )
    log.info(
        "评估请求 %s 完成：基线成本=%.2f 候选成本=%.2f 采纳=%d 拒绝=%d",
        request_id,
        result.baseline_total_cost,
        result.hypothetical_total_cost,
        len(result.adopted_candidates),
        len(result.rejected_candidates),
    )
    return result


@app.post(
    "/rename-preview",
    response_model=RenamePreviewResponse,
    tags=["rename-preview"],
    responses={
        400: {"description": "旧列不存在 / 新列名已存在 / 新旧同名 / 请求参数非法"},
        500: {"description": "数据库内部错误；连接已清理后归还或丢弃"},
    },
)
async def rename_preview(req: RenamePreviewRequest) -> RenamePreviewResponse:
    """列更名预览：静态改写最多 5 条只读 SELECT。

    - 只读取 information_schema 的现有列目录，**不执行**提交的查询，也不修改表；
    - 可安全处理的查询返回改写 SQL，未显式起别名的裸列输出列保留旧输出名；
    - 星号、歧义或不支持结构逐条返回 status=manual 与中文原因，不给改写 SQL。
    """
    try:
        return await preview_column_rename(pool, req)
    except RenamePreviewError:
        raise
    except SQLValidationError:
        raise
    except Exception as exc:
        log.exception("更名预览请求失败: %r", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": "rename_preview_failed",
                "detail": "更名预览失败，未产生改写结果；连接已完成清理",
            },
        )
