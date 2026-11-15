"""列更名预览服务：只读列目录校验 + 纯静态 SQL 改写。

安全边界：
- 仅查询 information_schema.columns 读取现有列目录，不执行任何提交的查询，
  不修改表结构（连接本身也处于 default_transaction_read_only=on）；
- 旧列不存在、新列名已存在（或新旧同名）在进入逐条改写前整体拒绝（HTTP 400）；
- 每条查询先过 sqlparse 只读守卫，再交给纯标准库的 rewrite 模块做静态改写；
- 不支持的结构逐条标记 manual，不产出任何可用改写 SQL。
"""
from __future__ import annotations

import logging

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .db import TwoConnectionPool
from .rewrite import preview_rename
from .schemas import (
    RenamePreviewItem,
    RenamePreviewRequest,
    RenamePreviewResponse,
    TARGET_SCHEMA,
    TARGET_TABLE,
)
from .validation import SQLValidationError, validate_read_only_select

log = logging.getLogger("hypopg_api.rename")


class RenamePreviewError(RuntimeError):
    """业务可预期错误（旧列缺失/新列冲突等），映射为 HTTP 400。"""


async def _fetch_columns(conn: AsyncConnection) -> list[str]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position",
            (TARGET_SCHEMA, TARGET_TABLE),
        )
        rows = await cur.fetchall()
    if not rows:
        raise RenamePreviewError(
            f"目标表 {TARGET_SCHEMA}.{TARGET_TABLE} 不存在或无列目录，拒绝预览"
        )
    return [r["column_name"] for r in rows]


async def preview_column_rename(
    pool: TwoConnectionPool, req: RenamePreviewRequest
) -> RenamePreviewResponse:
    # 解析层只读校验放在连接外，非法输入不占用两条宝贵连接
    validated: list[str] = []
    for idx, sql_text in enumerate(req.queries):
        try:
            validated.append(validate_read_only_select(sql_text))
        except SQLValidationError as exc:
            # 守卫不过的查询不影响其他条目的改写判断，但本条只能人工处理
            log.info("更名预览第 %d 条未通过只读守卫: %s", idx, exc)
            validated.append("")

    async with pool.lease() as conn:
        columns = await _fetch_columns(conn)

    existing = set(columns)
    if req.old_column not in existing:
        raise RenamePreviewError(
            f"旧列 {req.old_column!r} 在 {TARGET_SCHEMA}.{TARGET_TABLE} 上不存在，拒绝预览"
        )
    if req.new_column.lower() == req.old_column.lower():
        raise RenamePreviewError("新列名与旧列名相同，无需更名")
    # PG 列名折叠为小写比较；引号差异不改变“已存在”的事实
    if req.new_column.lower() in {c.lower() for c in existing}:
        raise RenamePreviewError(
            f"新列名 {req.new_column!r} 已存在于目标表，拒绝更名（列名不允许重复）"
        )

    results: list[RenamePreviewItem] = []
    rewritten_count = 0
    manual_count = 0
    changed_count = 0

    for idx, sql_text in enumerate(req.queries):
        if not validated[idx]:
            results.append(RenamePreviewItem(
                index=idx,
                status="manual",
                original_sql=sql_text,
                rewritten_sql=None,
                reason="未通过只读 SELECT 守卫（多语句/写操作/非 SELECT），需人工处理",
            ))
            manual_count += 1
            continue

        outcome = preview_rename(
            sql_text,
            schema=TARGET_SCHEMA,
            table=TARGET_TABLE,
            old_column=req.old_column,
            new_column=req.new_column,
        )
        if outcome.status == "manual":
            results.append(RenamePreviewItem(
                index=idx,
                status="manual",
                original_sql=sql_text,
                rewritten_sql=None,
                reason=outcome.reason,
            ))
            manual_count += 1
            continue

        results.append(RenamePreviewItem(
            index=idx,
            status="rewritten",
            original_sql=sql_text,
            rewritten_sql=outcome.rewritten_sql,
            reason=None,
            output_columns_preserved=True,
            renamed_reference_count=outcome.renamed_reference_count,
        ))
        rewritten_count += 1
        if outcome.renamed_reference_count > 0:
            changed_count += 1

    return RenamePreviewResponse(
        target_table=f"{TARGET_SCHEMA}.{TARGET_TABLE}",
        old_column=req.old_column,
        new_column=req.new_column,
        results=results,
        summary={
            "total": len(req.queries),
            "rewritten": rewritten_count,
            "manual": manual_count,
            "changed": changed_count,
        },
    )
