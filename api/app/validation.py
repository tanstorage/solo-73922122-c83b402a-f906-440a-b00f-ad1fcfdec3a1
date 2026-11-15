"""只读 SELECT 校验。

两层防线：
1. 解析层（本模块）：基于 sqlparse 的 token 类型做精确判断，确保是单条、
   纯只读 SELECT/WITH 语句；字符串字面量、注释、标识符中的敏感词不会被误判；
2. 会话层（db.py）：每条连接 SET default_transaction_read_only = on，
   任何写操作会被数据库自身拒绝。
"""
from __future__ import annotations

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import Comment, Keyword, Literal, Name, Punctuation, Whitespace

# 出现即拒绝的关键字（数据修改 / DDL / 管理命令）。仅在 token 本身是关键字时匹配，
# 因此字符串 'DELETE'、列名 updated_at 等不会误伤。
_FORBIDDEN_KEYWORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE",
    "CREATE", "ALTER", "DROP", "REINDEX", "CLUSTER", "VACUUM",
    "GRANT", "REVOKE", "REFRESH", "COPY", "CALL", "DO",
    "LOCK", "CHECKPOINT", "DISCARD", "RESET", "SET",
    "PREPARE", "EXECUTE", "DEALLOCATE", "COMMENT", "SECURITY",
})
# 只允许 SELECT，或以 WITH 开头的只读 CTE（CTE 内部仍受上面的关键字表约束）。
_ALLOWED_FIRST = frozenset({"SELECT", "WITH"})


class SQLValidationError(ValueError):
    pass


def _is_ignorable(token) -> bool:
    """空白、注释、字面量（字符串/数字）、普通标识符与标点不参与关键字判断。"""
    ttype = token.ttype
    return (
        ttype in Whitespace
        or ttype in Comment
        or ttype in Literal
        or ttype in Name
        or ttype in Punctuation
        or ttype is None
    )


def validate_read_only_select(sql_text: str) -> str:
    """返回规范化后的 SQL；不合法则抛 SQLValidationError。绝不执行非 SELECT。"""
    stripped = sql_text.strip().rstrip(";").strip()
    if not stripped:
        raise SQLValidationError("SQL 为空")

    statements = [
        s for s in sqlparse.parse(stripped)
        if any(not _is_ignorable(t) for t in s.flatten())
    ]
    if len(statements) != 1:
        raise SQLValidationError("只允许提交单条 SQL 语句，检测到多条语句")

    stmt: Statement = statements[0]
    first_keyword = None
    for token in stmt.flatten():
        if _is_ignorable(token):
            continue
        if token.ttype in Keyword or token.ttype in Keyword.DML:
            word = token.normalized.upper()
            if first_keyword is None:
                first_keyword = word
            if word in _FORBIDDEN_KEYWORDS:
                raise SQLValidationError(
                    f"检测到非只读关键字 {word!r}，该接口仅接受 SELECT"
                )
        else:
            # 首个有效 token 不是关键字（例如以数字、括号开头的表达式）
            if first_keyword is None:
                raise SQLValidationError(
                    "只允许 SELECT 或以 WITH 开头的只读查询"
                )

    if first_keyword not in _ALLOWED_FIRST:
        raise SQLValidationError(
            f"只允许 SELECT 或以 WITH 开头的只读查询，首关键字为 {first_keyword!r}"
        )

    # 多语句已在上面通过 sqlparse 的语句切分拦截；字符串字面量中的分号合法，不再单独检查
    return sqlparse.format(
        str(stmt), strip_comments=True, strip_whitespace=True
    )
