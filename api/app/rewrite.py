"""列更名 SQL 改写核心（纯标准库，无第三方依赖，可离线单测）。

设计原则（对应需求）：
- 只做**词法/结构分析与文本级改写**，绝不执行提交的 SQL，也不依赖数据库连接；
- 仅接受单表（public.package_scan）只读 SELECT，可带表别名、**一层** CTE、
  表达式及 WHERE/GROUP BY/HAVING/WINDOW/ORDER BY/LIMIT 等；
- 只改写确实引用旧列的标识符位置：字符串、注释、关键字、函数名、CTE 输出别名、
  外层对 CTE 的引用一律不动；
- 未显式起别名的裸列输出列改写为 ``new AS old``，输出列名保持旧名不变；
- 星号、JOIN/多表、子查询、集合运算、多层/递归 CTE、行锁定等无法保证安全的
  结构一律返回 ``manual`` 并附中文原因，不产出任何改写 SQL。

PostgreSQL 名称解析要点（本模块据此处理“歧义”）：
- 裸标识符在单表作用域内只可能指向该表列；带限定名时校验限定符是否为
  已声明别名 / 表名 / schema.表名，否则不改写；
- ORDER BY / GROUP BY 中**独立的裸标识符**若命中投影输出名（含隐式列名），
  解析为输出列引用而非表列，保持原样（输出列名本就保留旧名，语义不变）；
- WHERE / HAVING / 表达式内部看不到输出别名，其中的标识符按表列处理。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------- Token 定义 ----------------

T_WS = "ws"
T_COMMENT = "comment"
T_STRING = "string"        # 含 dollar-quoted
T_QIDENT = "qident"        # 双引号界定标识符
T_NUMBER = "number"
T_WORD = "word"            # 裸标识符/关键字
T_PUNCT = "punct"          # ( ) , . ;
T_OP = "op"                # 运算符与 * 等

_SIMPLE_PUNCT = set("(),.;")
_MULTI_OPS = (
    "::", "<=", ">=", "<>", "!=", "->>", "->", "#>>", "#>",
    "?|", "?&", "||", "&&", ":=", "<<", ">>",
)

# 关键字（裸词命中即视为语法，不可能是裸列名；引号界定的同名标识符不受影响）
_KEYWORDS = frozenset({
    "SELECT", "FROM", "WHERE", "GROUP", "HAVING", "WINDOW", "ORDER", "BY",
    "LIMIT", "OFFSET", "FETCH", "FIRST", "LAST", "NEXT", "ROW", "ROWS",
    "ONLY", "TIES", "AS", "AND", "OR", "NOT", "IS", "NULL", "IN", "BETWEEN",
    "LIKE", "ILIKE", "SIMILAR", "TO", "DISTINCT", "ALL", "ANY", "SOME",
    "EXISTS", "CASE", "WHEN", "THEN", "ELSE", "END", "CAST", "OVER",
    "PARTITION", "FILTER", "WITH", "RECURSIVE", "MATERIALIZED", "UNION",
    "INTERSECT", "EXCEPT", "JOIN", "INNER", "LEFT", "RIGHT", "FULL",
    "OUTER", "CROSS", "NATURAL", "ON", "USING", "LATERAL", "FOR", "UPDATE",
    "SHARE", "OF", "NOWAIT", "SKIP", "LOCKED", "INTO", "DEFAULT", "TRUE",
    "FALSE", "UNKNOWN", "ASC", "DESC", "NULLS", "VALUES", "TABLE", "FROM",
    "BOTH", "LEADING", "TRAILING", "COLLATE", "ESCAPE", "VARIADIC",
    "GROUPING", "SETS", "CUBE", "ROLLUP", "ORDINALITY", "WITHIN", "FOLLOWING",
    "PRECEDING", "CURRENT", "RANGE", "GROUPS", "UNBOUNDED", "EXCLUDE",
    "TIES", "PIVOT", "UNPIVOT", "INTEGER", "BIGINT", "TEXT", "BOOLEAN",
    "DATE", "TIMESTAMP", "TIMESTAMPTZ", "INTERVAL", "NUMERIC", "REAL",
    "DOUBLE", "PRECISION", "VARCHAR", "CHAR", "FLOAT", "SMALLINT",
    "JSON", "JSONB", "UUID", "INT", "BOOL",
})

# SELECT 块的子句边界关键字（相对块深度 0）
_CLAUSE_KEYWORDS = frozenset({
    "WHERE", "GROUP", "HAVING", "WINDOW", "ORDER", "LIMIT", "OFFSET",
    "FETCH", "FOR", "UNION", "INTERSECT", "EXCEPT", "INTO",
})
_SET_OP_KEYWORDS = frozenset({"UNION", "INTERSECT", "EXCEPT"})
# FROM 中出现即不支持的语法标记
_FROM_FORBIDDEN = frozenset({
    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "NATURAL",
    "LATERAL", "ON", "USING",
})


@dataclass
class Token:
    kind: str
    text: str
    start: int
    end: int  # 半开区间


class RewriteParseError(ValueError):
    """词法/语法无法解析，调用方按 manual 处理。"""


# ---------------- 词法分析 ----------------


def tokenize(sql: str) -> list[Token]:
    n = len(sql)
    i = 0
    tokens: list[Token] = []

    def _is_word_start(ch: str) -> bool:
        return ch.isalpha() or ch == "_"

    def _is_word_char(ch: str) -> bool:
        return ch.isalnum() or ch in ("_", "$")

    while i < n:
        ch = sql[i]

        if ch in " \t\r\n\f\v":
            j = i + 1
            while j < n and sql[j] in " \t\r\n\f\v":
                j += 1
            tokens.append(Token(T_WS, sql[i:j], i, j))
            i = j
            continue

        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            j = n if j == -1 else j + 1
            tokens.append(Token(T_COMMENT, sql[i:j], i, j))
            i = j
            continue

        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            # PG 块注释可嵌套
            depth = 1
            j = i + 2
            while j < n and depth:
                if sql[j] == "/" and j + 1 < n and sql[j + 1] == "*":
                    depth += 1
                    j += 2
                elif sql[j] == "*" and j + 1 < n and sql[j + 1] == "/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise RewriteParseError("块注释未闭合")
            tokens.append(Token(T_COMMENT, sql[i:j], i, j))
            i = j
            continue

        if ch == "'" or (
            ch in "eEbBxXuU" and i + 1 < n and sql[i + 1] in "'\""
            and _is_word_start(ch)
        ):
            start = i
            prefix = ""
            if ch != "'":
                # E'...' / B'...' / X'...' / U&'...' 前缀
                if ch in "uU" and i + 1 < n and sql[i + 1] == "&":
                    prefix = sql[i:i + 2]
                    i += 2
                else:
                    prefix = ch
                    i += 1
                ch = sql[i]
            if ch != "'":
                raise RewriteParseError("无法识别的字符串前缀")
            quote = ch
            i += 1
            buf = [prefix, quote]
            escaped = prefix.lower().startswith("e")
            while i < n:
                c = sql[i]
                if escaped and c == "\\":
                    if i + 1 >= n:
                        raise RewriteParseError("E 字符串转义未闭合")
                    buf.append(sql[i:i + 2])
                    i += 2
                    continue
                if c == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        buf.append(sql[i:i + 2])
                        i += 2
                        continue
                    buf.append(c)
                    i += 1
                    break
                buf.append(c)
                i += 1
            else:
                raise RewriteParseError("字符串字面量未闭合")
            tokens.append(Token(T_STRING, "".join(buf), start, i))
            continue

        if ch == '"' or (ch in "uU" and i + 1 < n and sql[i + 1] == '&'
                         and i + 2 < n and sql[i + 2] == '"'):
            start = i
            if ch in "uU":
                i += 2
                ch = '"'
            i += 1
            buf = ['"']
            while i < n:
                c = sql[i]
                if c == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        buf.append('""')
                        i += 2
                        continue
                    buf.append('"')
                    i += 1
                    break
                buf.append(c)
                i += 1
            else:
                raise RewriteParseError("引号标识符未闭合")
            tokens.append(Token(T_QIDENT, "".join(buf), start, i))
            continue

        if ch == "$":
            # dollar-quoted: $tag$ ... $tag$
            m = i + 1
            while m < n and (sql[m].isalnum() or sql[m] == "_"):
                m += 1
            if m < n and sql[m] == "$":
                tag = sql[i:m + 1]
                close = sql.find(tag, m + 1)
                if close == -1:
                    raise RewriteParseError("dollar 引用未闭合")
                end = close + len(tag)
                tokens.append(Token(T_STRING, sql[i:end], i, end))
                i = end
                continue
            # 否则当作运算符处理
            tokens.append(Token(T_OP, ch, i, i + 1))
            i += 1
            continue

        if ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_."):
                j += 1
            tokens.append(Token(T_NUMBER, sql[i:j], i, j))
            i = j
            continue

        if _is_word_start(ch):
            j = i + 1
            while j < n and _is_word_char(sql[j]):
                j += 1
            # U& 前缀
            if sql[i:j].upper() == "U" and j < n and sql[j] == "&":
                j += 1
            tokens.append(Token(T_WORD, sql[i:j], i, j))
            i = j
            continue

        if ch in _SIMPLE_PUNCT:
            tokens.append(Token(T_PUNCT, ch, i, i + 1))
            i += 1
            continue

        matched = False
        for op in _MULTI_OPS:
            if sql.startswith(op, i):
                tokens.append(Token(T_OP, op, i, i + len(op)))
                i += len(op)
                matched = True
                break
        if matched:
            continue

        if ch in "+-*/%<>=!?&|#~^@:":
            tokens.append(Token(T_OP, ch, i, i + 1))
            i += 1
            continue

        raise RewriteParseError(f"无法识别的字符: {ch!r}")

    return tokens


# ---------------- Token 辅助 ----------------


def _is_sig(tok: Token) -> bool:
    return tok.kind not in (T_WS, T_COMMENT)


def _sig_indices(tokens: list[Token], lo: int = 0, hi: int | None = None) -> list[int]:
    if hi is None:
        hi = len(tokens)
    return [k for k in range(lo, hi) if _is_sig(tokens[k])]


def _word_upper(tok: Token) -> str:
    return tok.text.upper() if tok.kind == T_WORD else ""


def _is_kw(tok: Token | None, kw: str) -> bool:
    return tok is not None and tok.kind == T_WORD and tok.text.upper() == kw


def _is_ident(tok: Token | None) -> bool:
    return tok is not None and tok.kind in (T_WORD, T_QIDENT)


def _ident_name(tok: Token) -> str:
    """标识符在 PG 中的实际名字（裸词折叠小写，引号标识符去引号还原）。"""
    if tok.kind == T_WORD:
        return tok.text.lower()
    return tok.text[1:-1].replace('""', '"')


def _names_equal(tok: Token, name: str) -> bool:
    # PG 裸词折叠为小写；引号标识符保留原样，但与目录列名比较时按实际名字
    # 大小写不敏感匹配（单表作用域内不存在同名异列的歧义可能）
    return _ident_name(tok).lower() == name.lower()


def _find_matching(tokens: list[Token], open_idx: int) -> int:
    depth = 0
    for k in range(open_idx, len(tokens)):
        if tokens[k].kind == T_PUNCT and tokens[k].text == "(":
            depth += 1
        elif tokens[k].kind == T_PUNCT and tokens[k].text == ")":
            depth -= 1
            if depth == 0:
                return k
    raise RewriteParseError("括号未闭合")


def _split_top_commas(tokens: list[Token], lo: int, hi: int) -> list[tuple[int, int]]:
    """按深度 0 逗号切分，返回每段的 token 下标区间（含外围空白）。"""
    items: list[tuple[int, int]] = []
    depth = 0
    start = lo
    for k in range(lo, hi):
        t = tokens[k]
        if t.kind == T_PUNCT and t.text == "(":
            depth += 1
        elif t.kind == T_PUNCT and t.text == ")":
            depth -= 1
            if depth < 0:
                raise RewriteParseError("括号不匹配")
        elif depth == 0 and t.kind == T_PUNCT and t.text == ",":
            items.append((start, k))
            start = k + 1
    if depth != 0:
        raise RewriteParseError("括号不匹配")
    items.append((start, hi))
    return items


def _prev_sig(tokens: list[Token], idx: int, lo: int = 0) -> Token | None:
    for k in range(idx - 1, lo - 1, -1):
        if _is_sig(tokens[k]):
            return tokens[k]
    return None


def _next_sig(tokens: list[Token], idx: int, hi: int | None = None) -> Token | None:
    if hi is None:
        hi = len(tokens)
    for k in range(idx + 1, hi):
        if _is_sig(tokens[k]):
            return tokens[k]
    return None


def _next_sig_idx(tokens: list[Token], idx: int, hi: int | None = None) -> int | None:
    if hi is None:
        hi = len(tokens)
    for k in range(idx + 1, hi):
        if _is_sig(tokens[k]):
            return k
    return None


def _token_idx(tokens: list[Token], target: Token) -> int:
    for k, t in enumerate(tokens):
        if t is target:
            return k
    raise RewriteParseError("内部错误：token 不在列表中")


# ---------------- 结构分析 ----------------


@dataclass
class SelectItem:
    lo: int
    hi: int
    alias_tok: Token | None          # 显式/隐式输出别名
    bare_column: Token | None        # 整项即裸列（可带限定）时的列标识符
    chain: list[Token] | None        # 裸列的标识符链（a.b.col）


@dataclass
class Block:
    lo: int
    hi: int
    select_kw: int
    from_kw: int
    clauses: dict[str, tuple[int, int]]
    items: list[SelectItem]
    # FROM 目标
    relation_chain: list[Token]
    alias_tok: Token | None
    is_target_table: bool
    output_names: set[str] = field(default_factory=set)
    output_quoted: set[str] = field(default_factory=set)


def _parse_from_target(
    tokens: list[Token], lo: int, hi: int,
    schema: str, table: str, cte_name: str | None,
) -> tuple[list[Token], Token | None, bool]:
    """解析 FROM 子句的关系目标，返回 (标识符链, 别名, 是否目标表)。"""
    sig = _sig_indices(tokens, lo, hi)
    if not sig:
        raise RewriteParseError("FROM 子句为空")
    p = 0
    first = tokens[sig[p]]
    if first.kind == T_PUNCT and first.text == "(":
        raise _manual("FROM 中存在子查询，仅允许单表查询")
    if not _is_ident(first):
        raise _manual(f"FROM 目标不是普通表名: {first.text!r}")

    chain = [first]
    p += 1
    while p < len(sig):
        t = tokens[sig[p]]
        if t.kind == T_PUNCT and t.text == ".":
            p += 1
            if p >= len(sig):
                raise RewriteParseError("表名限定不完整")
            nxt = tokens[sig[p]]
            if not _is_ident(nxt):
                raise _manual("FROM 中存在 schema/函数调用等非普通表名结构")
            chain.append(nxt)
            p += 1
            continue
        break

    if len(chain) > 2:
        raise _manual("表名限定层级过多（仅支持 schema.table）")
    # 表名后紧跟 (  => 函数调用
    if p < len(sig) and tokens[sig[p]].kind == T_PUNCT and tokens[sig[p]].text == "(":
        raise _manual("FROM 中存在函数调用，仅允许普通表")

    rel_name = _ident_name(chain[-1])
    rel_schema = _ident_name(chain[-2]) if len(chain) == 2 else None
    # PostgreSQL 作用域规则：存在同名 CTE 时，裸表名优先解析到 CTE（CTE 遮蔽基表）；
    # 带 schema 限定的名称无法指向 CTE，仍按基表处理。
    cte_shadows = (
        cte_name is not None and rel_schema is None and rel_name == cte_name
    )
    is_target = (
        not cte_shadows
        and rel_name == table
        and (rel_schema is None or rel_schema == schema)
    )
    if not is_target and not cte_shadows:
        raise _manual(
            f"查询引用了非目标表 {'.'.join(_ident_name(t) for t in chain)}，"
            f"仅允许 {schema}.{table}"
        )

    alias: Token | None = None
    if p < len(sig):
        t = tokens[sig[p]]
        if _is_kw(t, "AS"):
            p += 1
            if p >= len(sig) or not _is_ident(tokens[sig[p]]):
                raise RewriteParseError("AS 后缺少别名")
            alias = tokens[sig[p]]
            p += 1
        elif _is_ident(t) and t.kind == T_QIDENT:
            alias = t
            p += 1
        elif _is_ident(t) and t.kind == T_WORD and t.text.upper() not in _KEYWORDS:
            alias = t
            p += 1
        elif _is_ident(t):
            raise _manual(f"FROM 中出现不支持的关键字 {t.text.upper()}")
        else:
            raise _manual("FROM 中存在不支持的结构（JOIN/多表/横向引用等）")

    if p < len(sig):
        t = tokens[sig[p]]
        word = _word_upper(t)
        if word in _FROM_FORBIDDEN:
            raise _manual("检测到 JOIN 或多表 FROM，仅允许单表查询")
        if t.kind == T_PUNCT and t.text == ",":
            raise _manual("FROM 中存在逗号多表，仅允许单表查询")
        raise _manual(f"FROM 子句存在不支持的结构: {t.text!r}")

    return chain, alias, is_target


class _Manual(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def _manual(reason: str) -> _Manual:
    return _Manual(reason)


def _parse_select_item(
    tokens: list[Token], lo: int, hi: int,
) -> SelectItem:
    sig = _sig_indices(tokens, lo, hi)
    if not sig:
        raise RewriteParseError("存在空的 SELECT 输出项")

    # 星号（深度 0）：* 或 tbl.*
    depth = 0
    for k in range(lo, hi):
        t = tokens[k]
        if t.kind == T_PUNCT and t.text == "(":
            depth += 1
        elif t.kind == T_PUNCT and t.text == ")":
            depth -= 1
        elif depth == 0 and t.kind == T_OP and t.text == "*":
            raise _manual("投影列表包含星号（* 或 表.*），输出列无法静态确认，需人工处理")

    # 尾随后置别名识别（body_hi 一律使用 token 下标）
    alias_tok: Token | None = None
    body_hi = hi
    last = tokens[sig[-1]]
    prev = _prev_sig(tokens, sig[-1], lo)
    if len(sig) >= 2 and _is_kw(tokens[sig[-2]], "AS") and _is_ident(last):
        # 显式 AS 别名
        alias_tok = last
        body_hi = sig[-2]
    elif _is_ident(last) and last.kind == T_QIDENT:
        # 引号界定的隐式别名（限定链尾的 ."x" 因前导点号被排除）
        if prev is not None and not (prev.kind == T_PUNCT and prev.text == "."):
            alias_tok = last
            body_hi = sig[-1]
    elif (
        _is_ident(last)
        and last.kind == T_WORD
        and last.text.upper() not in _KEYWORDS
        and prev is not None
        and not (prev.kind == T_PUNCT and prev.text == ".")
        and (
            prev.kind in (T_WORD, T_QIDENT, T_NUMBER, T_STRING)
            or (prev.kind == T_PUNCT and prev.text == ")")
        )
    ):
        # 隐式别名：count(*) total / 1 x / 'a' label / a b
        alias_tok = last
        body_hi = sig[-1]

    # 裸列判定：表达式主体仅为 标识符(.标识符)* 链
    body_sig = _sig_indices(tokens, lo, body_hi)
    chain: list[Token] = []
    ok_bare = True
    expect_ident = True
    for k in body_sig:
        t = tokens[k]
        if expect_ident:
            if not _is_ident(t):
                ok_bare = False
                break
            chain.append(t)
            expect_ident = False
        elif t.kind == T_PUNCT and t.text == ".":
            expect_ident = True
        else:
            ok_bare = False
            break
    if not ok_bare or not chain or expect_ident:
        chain = []
    bare = chain[-1] if chain else None

    return SelectItem(lo=lo, hi=hi, alias_tok=alias_tok,
                      bare_column=bare, chain=chain or None)


def _parse_block(
    tokens: list[Token], lo: int, hi: int,
    schema: str, table: str, cte_name: str | None,
) -> Block:
    sig = _sig_indices(tokens, lo, hi)
    if not sig or not _is_kw(tokens[sig[0]], "SELECT"):
        raise _manual("仅支持 SELECT 查询（或 WITH 开头的单层 CTE SELECT）")
    select_kw = sig[0]

    # 深度 0 扫描：子句边界 / 集合运算 / 子查询 / 锁定子句 / INTO
    depth = 0
    from_kw: int | None = None
    clauses: dict[str, tuple[int, int]] = {}
    for k in range(select_kw + 1, hi):
        t = tokens[k]
        if t.kind == T_PUNCT and t.text == "(":
            depth += 1
            continue
        if t.kind == T_PUNCT and t.text == ")":
            depth -= 1
            continue
        if depth > 0:
            if t.kind == T_WORD and t.text.upper() == "SELECT":
                raise _manual("检测到子查询（括号内 SELECT / EXISTS / IN(SELECT)），需人工处理")
            if t.kind == T_WORD and t.text.upper() == "WITH":
                raise _manual(
                    "CTE 内不允许再嵌套 CTE（仅支持一层 CTE），需人工处理"
                )
            continue
        word = _word_upper(t)
        if word == "FROM":
            if from_kw is not None:
                raise _manual("出现多个 FROM，结构不受支持")
            from_kw = k
        elif word in _SET_OP_KEYWORDS:
            raise _manual(f"检测到集合运算 {word}，仅支持单条 SELECT")
        elif word == "INTO":
            raise _manual("不支持 SELECT INTO（写操作）")
        elif word in ("FOR",) and _next_sig(tokens, k, hi) is not None and \
                _word_upper(_next_sig(tokens, k, hi)) in ("UPDATE", "SHARE"):
            raise _manual("不支持 FOR UPDATE / FOR SHARE 行锁定子句")
        elif word in _CLAUSE_KEYWORDS:
            name = word
            if word in ("GROUP", "ORDER"):
                nxt = _next_sig(tokens, k, hi)
                if not _is_kw(nxt, "BY"):
                    continue
                name = "GROUP BY" if word == "GROUP" else "ORDER BY"
            clauses.setdefault(name, (k, hi))

    if from_kw is None:
        raise _manual("缺少 FROM 子句，仅允许查询目标表的 SELECT")

    # 计算各子句的精确区间（到下一个边界为止）
    boundaries = sorted(
        ((start, name) for name, (start, _) in clauses.items()),
        key=lambda x: x[0],
    )
    clause_ranges: dict[str, tuple[int, int]] = {}
    for idx, (start, name) in enumerate(boundaries):
        end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else hi
        clause_ranges[name] = (start, end)

    # 投影区间：SELECT 之后到 FROM 之前
    proj_lo = select_kw + 1
    # 跳过 DISTINCT / ALL / DISTINCT ON (...)
    probe_idx = _next_sig_idx(tokens, select_kw, from_kw)
    if probe_idx is not None and (
        _is_kw(tokens[probe_idx], "DISTINCT") or _is_kw(tokens[probe_idx], "ALL")
    ):
        proj_lo = probe_idx + 1
        after_idx = _next_sig_idx(tokens, probe_idx, from_kw)
        if _is_kw(tokens[probe_idx], "DISTINCT") and after_idx is not None \
                and _is_kw(tokens[after_idx], "ON"):
            paren_idx = _next_sig_idx(tokens, after_idx, from_kw)
            if paren_idx is None or tokens[paren_idx].kind != T_PUNCT \
                    or tokens[paren_idx].text != "(":
                raise RewriteParseError("DISTINCT ON 后缺少括号")
            close = _find_matching(tokens, paren_idx)
            if close >= from_kw:
                raise RewriteParseError("DISTINCT ON 括号超出投影范围")
            proj_lo = close + 1

    item_ranges = _split_top_commas(tokens, proj_lo, from_kw)
    items = [_parse_select_item(tokens, a, b) for a, b in item_ranges]

    # FROM 目标
    from_lo = from_kw + 1
    from_hi = boundaries[0][0] if boundaries else hi
    chain, alias, is_target = _parse_from_target(
        tokens, from_lo, from_hi, schema, table, cte_name
    )

    # 输出名集合（裸词折叠小写；引号名保留精确形式）
    output_names: set[str] = set()
    output_quoted: set[str] = set()
    for item in items:
        if item.alias_tok is not None:
            name = _ident_name(item.alias_tok)
            if item.alias_tok.kind == T_QIDENT:
                output_quoted.add(name)
            else:
                output_names.add(name.lower())
        elif item.bare_column is not None:
            name = _ident_name(item.bare_column)
            if item.bare_column.kind == T_QIDENT:
                output_quoted.add(name)
            else:
                output_names.add(name.lower())

    return Block(
        lo=lo, hi=hi, select_kw=select_kw, from_kw=from_kw,
        clauses=clause_ranges, items=items,
        relation_chain=chain, alias_tok=alias, is_target_table=is_target,
        output_names=output_names, output_quoted=output_quoted,
    )


# ---------------- 改写 ----------------


@dataclass
class PreviewResult:
    status: str                      # "rewritten" | "manual"
    rewritten_sql: str | None
    reason: str | None
    renamed_reference_count: int


def _qualifier_ok(chain: list[Token], block: Block, schema: str, table: str) -> bool:
    """校验限定名 a.b.col / a.b.c.col 的前缀是否指向目标表或其别名。"""
    quals = chain[:-1]
    names = [_ident_name(t) for t in quals]
    alias = _ident_name(block.alias_tok) if block.alias_tok else None
    rel = _ident_name(block.relation_chain[-1])
    rel_schema = (
        _ident_name(block.relation_chain[-2])
        if len(block.relation_chain) == 2 else None
    )
    if alias is not None and names == [alias]:
        return True
    if names == [rel]:
        return True
    if names == [rel_schema or schema, rel]:
        return True
    return False


def _ref_matches(tok: Token, old: str) -> bool:
    return _names_equal(tok, old)


def _validate_dots(tokens: list[Token], lo: int, hi: int) -> None:
    """点号两侧（忽略空白）必须都是标识符，否则为非法限定（如 a..b / .b / a.）。"""
    for k in range(lo, hi):
        t = tokens[k]
        if t.kind != T_PUNCT or t.text != ".":
            continue
        left = _prev_sig(tokens, k, lo)
        right = _next_sig(tokens, k, hi)
        if not _is_ident(left) or not _is_ident(right):
            raise _Manual("存在非法的点号限定（如 a..b、.b、a.），需人工处理")


def _validate_qualifiers(
    tokens: list[Token], lo: int, hi: int, block: Block,
    schema: str, table: str,
) -> None:
    """扫描区间内所有带限定的标识符链，限定符无法解析到目标表/别名即转人工。"""
    k = lo
    while k < hi:
        t = tokens[k]
        if t.kind not in (T_WORD, T_QIDENT):
            k += 1
            continue
        prev = _prev_sig(tokens, k, lo)
        if prev is not None and prev.kind == T_PUNCT and prev.text == ".":
            k += 1
            continue
        chain = [t]
        probe = k
        while True:
            dot_idx = probe + 1
            while dot_idx < hi and not _is_sig(tokens[dot_idx]):
                dot_idx += 1
            if dot_idx >= hi or tokens[dot_idx].kind != T_PUNCT \
                    or tokens[dot_idx].text != ".":
                break
            col_idx = dot_idx + 1
            while col_idx < hi and not _is_sig(tokens[col_idx]):
                col_idx += 1
            if col_idx >= hi or not _is_ident(tokens[col_idx]):
                break
            chain.append(tokens[col_idx])
            probe = col_idx
        if len(chain) > 1 and not _qualifier_ok(chain, block, schema, table):
            raise _Manual(
                "存在无法解析的限定名 "
                f"{'.'.join(q.text for q in chain)}（限定符既不是表名也不是声明的别名），"
                "存在歧义，需人工处理"
            )
        k = probe + 1


def _plan_block_edits(
    tokens: list[Token], block: Block, schema: str, table: str,
    old: str, new: str,
) -> list[tuple[int, int, str]]:
    if not block.is_target_table:
        # 外层读取 CTE：CTE 输出别名与外层引用一律不动
        return []

    # 先做全块限定符校验：任何不合法限定名都整体转人工，绝不产出半成品
    for item in block.items:
        item_hi = item.hi if item.alias_tok is None \
            else _token_idx(tokens, item.alias_tok)
        _validate_dots(tokens, item.lo, item_hi)
        _validate_qualifiers(tokens, item.lo, item_hi, block, schema, table)
    for clause in ("WHERE", "HAVING", "WINDOW", "GROUP BY", "ORDER BY"):
        if clause in block.clauses:
            clo, chi = block.clauses[clause]
            _validate_dots(tokens, clo + 1, chi)
            _validate_qualifiers(tokens, clo + 1, chi, block, schema, table)

    edits: list[tuple[int, int, str]] = []

    def _output_name_visible(tok: Token) -> bool:
        name = _ident_name(tok)
        if tok.kind == T_QIDENT:
            return name in block.output_quoted
        return name.lower() in block.output_names

    def _rename_identifier(tok: Token) -> tuple[int, int, str]:
        return (tok.start, tok.end, new)

    def _rewrite_expr(lo: int, hi: int, *, alias_visible: bool,
                      standalone: Token | None = None) -> None:
        """在表达式区间内改写所有指向旧列的表列引用。"""
        if standalone is not None and alias_visible and _output_name_visible(standalone):
            # ORDER/GROUP BY 的独立裸标识符命中输出名：它是输出列引用，保持原样
            return
        k = lo
        while k < hi:
            t = tokens[k]
            if t.kind not in (T_WORD, T_QIDENT):
                k += 1
                continue
            prev = _prev_sig(tokens, k, lo)
            nxt = _next_sig(tokens, k, hi)
            # 函数名（关键字但函数式调用，如 count(*)；裸列即使名为 count 也无括号）
            if nxt is not None and nxt.kind == T_PUNCT and nxt.text == "(":
                k += 1
                continue
            # 语法关键字（裸词出现即非列名；引号界定的同名标识符走 QIDENT 不受影响）
            if t.kind == T_WORD and t.text.upper() in _KEYWORDS:
                k += 1
                continue
            if prev is not None and prev.kind == T_PUNCT and prev.text == ".":
                k += 1  # 限定链在列尾统一处理
                continue
            # 收集限定链
            chain = [t]
            probe = k
            while True:
                dot_idx = probe + 1
                while dot_idx < hi and not _is_sig(tokens[dot_idx]):
                    dot_idx += 1
                if dot_idx >= hi or tokens[dot_idx].kind != T_PUNCT \
                        or tokens[dot_idx].text != ".":
                    break
                col_idx = dot_idx + 1
                while col_idx < hi and not _is_sig(tokens[col_idx]):
                    col_idx += 1
                if col_idx >= hi or not _is_ident(tokens[col_idx]):
                    break
                chain.append(tokens[col_idx])
                probe = col_idx
            col_tok = chain[-1]
            if len(chain) == 1:
                # 裸标识符（standalone 的输出别名情形已在函数入口整体返回）
                if _ref_matches(col_tok, old):
                    edits.append(_rename_identifier(col_tok))
            else:
                if _ref_matches(col_tok, old) and _qualifier_ok(chain, block, schema, table):
                    edits.append(_rename_identifier(col_tok))
            k = probe + 1

    # 1) 投影项
    for item in block.items:
        if item.bare_column is not None and item.chain is not None \
                and _ref_matches(item.bare_column, old) \
                and (len(item.chain) == 1
                     or _qualifier_ok(item.chain, block, schema, table)):
            if item.alias_tok is not None:
                # 显式别名保留，只换列名
                edits.append(_rename_identifier(item.bare_column))
            else:
                # 未显式别名：new AS old，输出列名维持旧名；
                # 替换区间限定在项内首个/末个有效 token，保留项首尾空白
                first_sig = next(k for k in range(item.lo, item.hi) if _is_sig(tokens[k]))
                last_sig = next(k for k in range(item.hi - 1, item.lo - 1, -1)
                                if _is_sig(tokens[k]))
                chain_prefix = ".".join(
                    q.text for q in item.chain[:-1]
                )
                prefix = f"{chain_prefix}." if chain_prefix else ""
                replacement = f"{prefix}{new} AS {item.bare_column.text}"
                edits.append((tokens[first_sig].start, tokens[last_sig].end,
                              replacement))
        else:
            # 表达式输出项：只扫描表达式主体，显式/隐式别名 token 绝不改写
            _rewrite_expr(item.lo,
                          item.hi if item.alias_tok is None
                          else _token_idx(tokens, item.alias_tok),
                          alias_visible=False)

    # 2) 看不到输出别名的子句：WHERE / HAVING / WINDOW / DISTINCT ON
    for clause in ("WHERE", "HAVING", "WINDOW"):
        if clause in block.clauses:
            clo, chi = block.clauses[clause]
            _rewrite_expr(clo + 1, chi, alias_visible=False)
    # DISTINCT ON (...)：从投影区间重新定位
    probe_idx = _next_sig_idx(tokens, block.select_kw, block.from_kw)
    if probe_idx is not None and _is_kw(tokens[probe_idx], "DISTINCT"):
        on_idx = _next_sig_idx(tokens, probe_idx, block.from_kw)
        if on_idx is not None and _is_kw(tokens[on_idx], "ON"):
            paren_idx = _next_sig_idx(tokens, on_idx, block.from_kw)
            close = _find_matching(tokens, paren_idx)
            for a, b in _split_top_commas(tokens, paren_idx + 1, close):
                _rewrite_expr(a, b, alias_visible=False)

    # 3) ORDER BY / GROUP BY：独立裸标识符可能是输出列引用
    for clause in ("GROUP BY", "ORDER BY"):
        if clause not in block.clauses:
            continue
        clo, chi = block.clauses[clause]
        # 子句区间起点是 GROUP/ORDER 关键字本身，跳过它与 BY
        first_idx = _next_sig_idx(tokens, clo - 1, chi)
        by_idx = _next_sig_idx(tokens, first_idx, chi)
        list_lo = by_idx + 1
        for a, b in _split_top_commas(tokens, list_lo, chi):
            # 排序修饰关键字（ASC/DESC/NULLS/FIRST/LAST）不参与“独立裸标识符”判定
            order_modifiers = {"ASC", "DESC", "NULLS", "FIRST", "LAST"}
            body_sig = [
                k for k in _sig_indices(tokens, a, b)
                if not (clause == "ORDER BY"
                        and _word_upper(tokens[k]) in order_modifiers)
            ]
            standalone: Token | None = None
            if len(body_sig) == 1 and _is_ident(tokens[body_sig[0]]):
                standalone = tokens[body_sig[0]]
            _rewrite_expr(a, b, alias_visible=True, standalone=standalone)

    # 校验编辑不重叠后应用
    edits.sort(key=lambda e: e[0])
    for (s1, e1, _), (s2, _, _) in zip(edits, edits[1:]):
        if s2 < e1:
            raise RewriteParseError("内部错误：改写区间重叠")
    return edits


def _apply_edits(sql: str, edits: list[tuple[int, int, str]]) -> str:
    if not edits:
        return sql
    out: list[str] = []
    cursor = 0
    for start, end, repl in sorted(edits, key=lambda e: e[0]):
        out.append(sql[cursor:start])
        out.append(repl)
        cursor = end
    out.append(sql[cursor:])
    return "".join(out)


# ---------------- 对外入口 ----------------


def preview_rename(
    sql_text: str,
    *,
    schema: str = "public",
    table: str = "package_scan",
    old_column: str,
    new_column: str,
) -> PreviewResult:
    """对单条 SELECT 产出更名预览；不安全的结构返回 manual。"""
    try:
        sql = sql_text.strip()
        if not sql:
            return PreviewResult("manual", None, "SQL 为空", 0)

        tokens = tokenize(sql)
        sig = _sig_indices(tokens)
        if not sig:
            return PreviewResult("manual", None, "SQL 为空", 0)

        # 分号：仅允许整条语句末尾的一个
        semicolons = [
            k for k in sig
            if tokens[k].kind == T_PUNCT and tokens[k].text == ";"
        ]
        if len(semicolons) > 1 or (
            len(semicolons) == 1 and semicolons[0] != sig[-1]
        ):
            return PreviewResult(
                "manual", None, "检测到多条语句或语句中分号，仅允许单条 SELECT", 0
            )
        hard_hi = semicolons[0] if semicolons else len(tokens)

        # 全句括号配平：词法层先挡掉未闭合括号，避免后续结构分析静默接受
        depth = 0
        for k in range(hard_hi):
            t = tokens[k]
            if t.kind == T_PUNCT and t.text == "(":
                depth += 1
            elif t.kind == T_PUNCT and t.text == ")":
                depth -= 1
                if depth < 0:
                    return PreviewResult(
                        "manual", None, "SQL 解析失败：括号不匹配", 0
                    )
        if depth != 0:
            return PreviewResult(
                "manual", None, "SQL 解析失败：括号未闭合", 0
            )

        # 非顶层 WITH 即嵌套 CTE（仅支持一层单个 CTE），在分块前直接拒绝
        scan_depth = 0
        for k in range(hard_hi):
            t = tokens[k]
            if t.kind == T_PUNCT and t.text == "(":
                scan_depth += 1
            elif t.kind == T_PUNCT and t.text == ")":
                scan_depth -= 1
            elif scan_depth > 0 and t.kind == T_WORD and t.text.upper() == "WITH":
                return PreviewResult(
                    "manual", None,
                    "CTE 内不允许再嵌套 CTE（仅支持一层 CTE），需人工处理", 0,
                )

        pos = sig[0]
        cte_name: str | None = None
        cte_block_range: tuple[int, int] | None = None
        outer_lo = pos

        if _is_kw(tokens[pos], "WITH"):
            nxt_idx = _next_sig_idx(tokens, pos, hard_hi)
            if nxt_idx is None:
                return PreviewResult("manual", None, "WITH 后缺少 CTE 名称", 0)
            if _is_kw(tokens[nxt_idx], "RECURSIVE"):
                return PreviewResult(
                    "manual", None, "不支持 RECURSIVE 递归 CTE", 0
                )
            if not _is_ident(tokens[nxt_idx]):
                return PreviewResult("manual", None, "WITH 后缺少 CTE 名称", 0)
            cte_name = _ident_name(tokens[nxt_idx])
            as_idx = _next_sig_idx(tokens, nxt_idx, hard_hi)
            if as_idx is None or not _is_kw(tokens[as_idx], "AS"):
                return PreviewResult("manual", None, "CTE 名称后缺少 AS", 0)
            opt_idx = _next_sig_idx(tokens, as_idx, hard_hi)
            if opt_idx is not None and (
                _is_kw(tokens[opt_idx], "MATERIALIZED")
                or (
                    _is_kw(tokens[opt_idx], "NOT")
                    and _is_kw(_next_sig(tokens, opt_idx, hard_hi), "MATERIALIZED")
                )
            ):
                return PreviewResult(
                    "manual", None, "不支持 MATERIALIZED / NOT MATERIALIZED CTE 选项", 0
                )
            if opt_idx is None or tokens[opt_idx].kind != T_PUNCT \
                    or tokens[opt_idx].text != "(":
                return PreviewResult("manual", None, "CTE 主体必须是括号包裹的 SELECT", 0)
            close = _find_matching(tokens, opt_idx)
            cte_block_range = (opt_idx + 1, close)
            after_idx = _next_sig_idx(tokens, close, hard_hi)
            if after_idx is not None and tokens[after_idx].kind == T_PUNCT \
                    and tokens[after_idx].text == ",":
                return PreviewResult(
                    "manual", None, "检测到多个 CTE，仅支持一层单个 CTE", 0
                )
            if after_idx is None or not _is_kw(tokens[after_idx], "SELECT"):
                return PreviewResult(
                    "manual", None, "CTE 之后必须且只能跟随一条 SELECT", 0
                )
            outer_lo = after_idx
        elif not _is_kw(tokens[pos], "SELECT"):
            return PreviewResult(
                "manual", None, "仅支持 SELECT 或以 WITH 开头的只读查询", 0
            )

        blocks: list[Block] = []
        if cte_block_range is not None:
            blocks.append(_parse_block(
                tokens, cte_block_range[0], cte_block_range[1],
                schema, table, None,
            ))
        blocks.append(_parse_block(
            tokens, outer_lo, hard_hi, schema, table, cte_name,
        ))

        edits: list[tuple[int, int, str]] = []
        for block in blocks:
            edits.extend(_plan_block_edits(
                tokens, block, schema, table, old_column, new_column,
            ))

        rewritten = _apply_edits(sql, edits)
        return PreviewResult(
            status="rewritten",
            rewritten_sql=rewritten,
            reason=None,
            renamed_reference_count=len(edits),
        )
    except _Manual as exc:
        return PreviewResult("manual", None, exc.reason, 0)
    except RewriteParseError as exc:
        return PreviewResult("manual", None, f"SQL 解析失败：{exc}", 0)
