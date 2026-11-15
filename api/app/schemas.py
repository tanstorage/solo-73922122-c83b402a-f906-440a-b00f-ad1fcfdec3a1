"""请求/响应模型。"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
MAX_CANDIDATES = 3
MAX_SQL_LENGTH = 20_000


class IndexCandidate(BaseModel):
    """一个 B-tree 候选索引：表名 + 有序列名（顺序即索引键顺序）。"""

    table: str = Field(..., description="表名，可带 schema 前缀，如 public.package_scan")
    columns: list[str] = Field(
        ..., min_length=1, max_length=8, description="有序索引列名，顺序即 B-tree 键顺序"
    )

    @field_validator("table")
    @classmethod
    def _validate_table(cls, v: str) -> str:
        parts = [p.strip() for p in v.split(".")]
        if len(parts) not in (1, 2) or any(not _IDENT_RE.match(p) for p in parts):
            raise ValueError("表名必须是合法标识符，形如 name 或 schema.name")
        return ".".join(parts)

    @field_validator("columns")
    @classmethod
    def _validate_columns(cls, v: list[str]) -> list[str]:
        cleaned = [c.strip() for c in v]
        if any(not _IDENT_RE.match(c) for c in cleaned):
            raise ValueError("列名必须是合法标识符（字母/下划线开头，最长 63）")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("同一候选内列名不能重复")
        return cleaned


class EvaluateRequest(BaseModel):
    sql_text: str | None = Field(None, description="只读 SELECT；与 sample_id 二选一")
    sample_id: str | None = Field(None, description="预置样例 ID，见 GET /samples")
    candidates: list[IndexCandidate] = Field(
        ..., min_length=1, max_length=MAX_CANDIDATES, description="最多 3 个 B-tree 候选"
    )

    @field_validator("sql_text")
    @classmethod
    def _validate_sql_text(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("sql_text 不能为空字符串")
        if len(v) > MAX_SQL_LENGTH:
            raise ValueError(f"SQL 长度超过上限 {MAX_SQL_LENGTH} 字符")
        return v

    @field_validator("candidates")
    @classmethod
    def _validate_unique_candidates(cls, v: list[IndexCandidate]) -> list[IndexCandidate]:
        keys = [(c.table, tuple(c.columns)) for c in v]
        if len(set(keys)) != len(keys):
            raise ValueError("候选索引之间不能完全重复（相同表名 + 相同有序列名）")
        return v


class CandidateResult(BaseModel):
    indexrelid: int
    index_name: str
    definition: str
    used_in_plan: bool


class EvaluateResponse(BaseModel):
    baseline_plan: dict[str, Any]
    baseline_total_cost: float
    hypothetical_plan: dict[str, Any]
    hypothetical_total_cost: float
    cost_delta: float
    improvement_ratio: float | None
    plan_changed: bool
    adopted_candidates: list[CandidateResult]
    rejected_candidates: list[CandidateResult]
    # 观测字段：证明并发隔离与基线不继承旧候选
    session_hypothetical_indexes_before_baseline: int
    session_hypothetical_indexes_after_reset: int


class HealthResponse(BaseModel):
    status: str
    pool: dict[str, Any]
    db: dict[str, Any]


# ---------------- 列更名预览 ----------------

MAX_RENAME_QUERIES = 5
TARGET_SCHEMA = "public"
TARGET_TABLE = "package_scan"


class RenamePreviewRequest(BaseModel):
    """列更名预览：旧列名、新列名 + 最多 5 条只读 SELECT。"""

    old_column: str = Field(..., description="计划改名的旧列（必须存在于目标表）")
    new_column: str = Field(..., description="改名后的新列名（不得与现有列重名）")
    queries: list[str] = Field(
        ..., min_length=1, max_length=MAX_RENAME_QUERIES,
        description="待检查的只读 SELECT，最多 5 条；服务不会执行这些 SQL",
    )

    @field_validator("old_column", "new_column")
    @classmethod
    def _validate_ident(cls, v: str) -> str:
        v = v.strip()
        if not _IDENT_RE.match(v):
            raise ValueError("列名必须是合法标识符（字母/下划线开头，最长 63）")
        return v

    @field_validator("queries")
    @classmethod
    def _validate_queries(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for q in v:
            if not isinstance(q, str):
                raise ValueError("每条查询必须是字符串")
            q = q.strip()
            if not q:
                raise ValueError("查询不能为空字符串")
            if len(q) > MAX_SQL_LENGTH:
                raise ValueError(f"单条 SQL 长度超过上限 {MAX_SQL_LENGTH} 字符")
            cleaned.append(q)
        return cleaned


class RenamePreviewItem(BaseModel):
    index: int = Field(..., description="查询在提交列表中的序号（从 0 开始）")
    status: str = Field(..., description="rewritten=已安全改写；manual=需人工处理")
    original_sql: str
    rewritten_sql: str | None = Field(
        None, description="status=manual 时为 null，绝不给出半成品改写"
    )
    reason: str | None = Field(None, description="manual 时的中文原因说明")
    output_columns_preserved: bool | None = Field(
        None, description="rewritten 时恒为 true：输出列名与原查询保持一致"
    )
    renamed_reference_count: int = Field(
        0, description="实际改写的旧列引用位置数（0 表示该查询未引用旧列，原样返回）"
    )


class RenamePreviewResponse(BaseModel):
    target_table: str
    old_column: str
    new_column: str
    results: list[RenamePreviewItem]
    summary: dict[str, Any] = Field(
        ..., description="统计：total / rewritten / manual / changed"
    )
