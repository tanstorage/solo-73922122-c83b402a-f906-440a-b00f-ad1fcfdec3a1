"""列更名改写核心离线单测（纯标准库，无需数据库与第三方包）。

运行：  python3 -I tests/test_rewrite.py
覆盖：
- 表别名 / schema 限定 / 一层 CTE / 表达式 / WHERE / GROUP BY / HAVING / ORDER BY / WINDOW；
- 未显式别名裸列保留旧输出名、显式别名原样保留；
- ORDER/GROUP BY 输出别名引用不误改、CTE 输出别名与外层引用不误改；
- 星号 / JOIN / 子查询 / 集合运算 / 多层 CTE / 行锁定 / 多语句 / 非目标表 -> manual；
- 字符串字面量、注释、关键字、函数名、引号标识符大小写折叠。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from app.rewrite import preview_rename  # noqa: E402

OLD = "scan_time"
NEW = "scanned_at"

_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


def run(sql: str, old: str = OLD, new: str = NEW):
    return preview_rename(
        sql, schema="public", table="package_scan",
        old_column=old, new_column=new,
    )


def expect_rewrite(name: str, sql: str, expected: str, count: int | None = None) -> None:
    r = run(sql)
    ok = r.status == "rewritten" and r.rewritten_sql == expected
    check(name, ok, f"status={r.status} reason={r.reason} "
                    f"got={r.rewritten_sql!r} want={expected!r}")
    if count is not None:
        check(f"{name} 改写计数={count}", r.renamed_reference_count == count,
              f"got={r.renamed_reference_count}")


def expect_manual(name: str, sql: str, reason_contains: str | None = None) -> None:
    r = run(sql)
    ok = r.status == "manual" and r.rewritten_sql is None
    check(name, ok, f"status={r.status} sql={r.rewritten_sql!r}")
    if reason_contains is not None:
        check(f"{name} 原因含 {reason_contains!r}",
              bool(r.reason and reason_contains in r.reason), r.reason or "")


def main() -> int:
    print("== 1. 基础改写：裸列输出保留旧名 ==")
    expect_rewrite(
        "裸列未起别名 -> new AS old",
        "SELECT scan_id, scan_time FROM public.package_scan WHERE scan_id = 1",
        f"SELECT scan_id, {NEW} AS {OLD} FROM public.package_scan WHERE scan_id = 1",
        count=1,
    )
    expect_rewrite(
        "带表别名的限定裸列",
        f"SELECT p.scan_time FROM public.package_scan p WHERE p.{OLD} > now()",
        f"SELECT p.{NEW} AS {OLD} FROM public.package_scan p WHERE p.{NEW} > now()",
        count=2,
    )
    expect_rewrite(
        "schema.表限定",
        f"SELECT public.package_scan.scan_time FROM public.package_scan",
        f"SELECT public.package_scan.{NEW} AS {OLD} FROM public.package_scan",
        count=1,
    )
    expect_rewrite(
        "无 schema 前缀的表名",
        "SELECT scan_time FROM package_scan",
        f"SELECT {NEW} AS {OLD} FROM package_scan",
        count=1,
    )
    expect_rewrite(
        "显式别名保留，只换列名",
        f"SELECT scan_time AS st FROM package_scan ORDER BY st",
        f"SELECT {NEW} AS st FROM package_scan ORDER BY st",
        count=1,
    )
    expect_rewrite(
        "隐式别名（无 AS）同样保留",
        "SELECT scan_time st FROM package_scan ORDER BY st",
        f"SELECT {NEW} st FROM package_scan ORDER BY st",
        count=1,
    )
    expect_rewrite(
        "表达式内裸列被换、表达式输出名不动",
        "SELECT date_trunc('day', scan_time) FROM package_scan WHERE scan_time >= now()",
        f"SELECT date_trunc('day', {NEW}) FROM package_scan WHERE {NEW} >= now()",
        count=2,
    )
    expect_rewrite(
        "表达式显式别名保留",
        f"SELECT scan_time + INTERVAL '1 hour' AS scan_time FROM package_scan",
        f"SELECT {NEW} + INTERVAL '1 hour' AS scan_time FROM package_scan",
        count=1,
    )

    print("== 2. WHERE / GROUP BY / HAVING / ORDER BY / WINDOW / DISTINCT ON ==")
    expect_rewrite(
        "WHERE+GROUP BY+HAVING 全部改写",
        "SELECT warehouse_id, count(*) FROM package_scan "
        "WHERE scan_time > now() GROUP BY warehouse_id "
        "HAVING max(scan_time) > now()",
        f"SELECT warehouse_id, count(*) FROM package_scan "
        f"WHERE {NEW} > now() GROUP BY warehouse_id "
        f"HAVING max({NEW}) > now()",
        count=2,
    )
    expect_rewrite(
        "ORDER BY 裸列（无同名输出）改写",
        "SELECT scan_id FROM package_scan ORDER BY scan_time DESC NULLS LAST",
        f"SELECT scan_id FROM package_scan ORDER BY {NEW} DESC NULLS LAST",
        count=1,
    )
    expect_rewrite(
        "ORDER BY 输出别名引用不误改",
        "SELECT scan_time AS st FROM package_scan ORDER BY st",
        f"SELECT {NEW} AS st FROM package_scan ORDER BY st",
        count=1,
    )
    expect_rewrite(
        "ORDER BY 引用未改名裸列的旧输出名（裸列项保留旧名）",
        "SELECT scan_id, scan_time FROM package_scan ORDER BY scan_time",
        f"SELECT scan_id, {NEW} AS {OLD} FROM package_scan ORDER BY {OLD}",
        count=1,
    )
    expect_rewrite(
        "ORDER BY 限定列名一定改写（表列引用）",
        "SELECT scan_time FROM package_scan p ORDER BY p.scan_time",
        f"SELECT {NEW} AS {OLD} FROM package_scan p ORDER BY p.{NEW}",
        count=2,
    )
    expect_rewrite(
        "GROUP BY 输出别名引用保持原样",
        "SELECT scan_time grp, count(*) FROM package_scan GROUP BY grp",
        f"SELECT {NEW} grp, count(*) FROM package_scan GROUP BY grp",
        count=1,
    )
    expect_rewrite(
        "GROUP BY 裸列本身改写（无同名输出）",
        "SELECT warehouse_id, count(*) FROM package_scan GROUP BY scan_time",
        f"SELECT warehouse_id, count(*) FROM package_scan GROUP BY {NEW}",
        count=1,
    )
    expect_rewrite(
        "GROUP BY 命中输出名（裸列项保留旧名）则保持旧名",
        "SELECT scan_time, count(*) FROM package_scan GROUP BY scan_time",
        f"SELECT {NEW} AS {OLD}, count(*) FROM package_scan GROUP BY {OLD}",
        count=1,
    )
    expect_rewrite(
        "DISTINCT ON 内的列改写",
        "SELECT DISTINCT ON (scan_time) scan_id, scan_time FROM package_scan "
        "ORDER BY scan_time, scan_id",
        f"SELECT DISTINCT ON ({NEW}) scan_id, {NEW} AS {OLD} FROM package_scan "
        f"ORDER BY {OLD}, scan_id",
        count=2,
    )
    expect_rewrite(
        "WINDOW 子句中的列改写",
        "SELECT warehouse_id, row_number() OVER (PARTITION BY warehouse_id "
        "ORDER BY scan_time) FROM package_scan",
        f"SELECT warehouse_id, row_number() OVER (PARTITION BY warehouse_id "
        f"ORDER BY {NEW}) FROM package_scan",
        count=1,
    )
    expect_rewrite(
        "CASE / 类型转换 / 函数嵌套",
        "SELECT CASE WHEN scan_time IS NULL THEN 0 ELSE 1 END, scan_time::date "
        "FROM package_scan WHERE coalesce(scan_time, now()) > now()",
        f"SELECT CASE WHEN {NEW} IS NULL THEN 0 ELSE 1 END, {NEW}::date "
        f"FROM package_scan WHERE coalesce({NEW}, now()) > now()",
        count=3,
    )

    print("== 3. 一层 CTE：CTE 内改写，输出别名与外层引用不动 ==")
    expect_rewrite(
        "CTE 内裸列改写且保留输出名，外层引用不动",
        "WITH night AS (SELECT scan_id, scan_time FROM public.package_scan "
        "WHERE scan_time >= TIMESTAMPTZ '2026-11-12 18:00:00+00') "
        "SELECT scan_id, scan_time FROM night ORDER BY scan_time",
        f"WITH night AS (SELECT scan_id, {NEW} AS {OLD} FROM public.package_scan "
        f"WHERE {NEW} >= TIMESTAMPTZ '2026-11-12 18:00:00+00') "
        f"SELECT scan_id, scan_time FROM night ORDER BY scan_time",
        count=2,
    )
    expect_rewrite(
        "CTE 显式输出别名不动，外层按别名引用不动",
        "WITH n AS (SELECT scan_time AS st FROM package_scan) SELECT st FROM n",
        f"WITH n AS (SELECT {NEW} AS st FROM package_scan) SELECT st FROM n",
        count=1,
    )
    expect_manual("多 CTE 拒绝",
                  "WITH a AS (SELECT scan_time FROM package_scan), "
                  "b AS (SELECT 1) SELECT * FROM a", "多个 CTE")
    expect_manual("RECURSIVE CTE 拒绝",
                  "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM r) "
                  "SELECT x FROM r", "RECURSIVE")
    expect_manual("CTE 内嵌套 CTE 拒绝",
                  "WITH a AS (WITH b AS (SELECT 1) SELECT * FROM b) SELECT * FROM a",
                  "人工")

    print("== 4. 不支持结构：逐条 manual，不产出改写 ==")
    expect_manual("SELECT *",
                  "SELECT * FROM package_scan", "星号")
    expect_manual("表别名.* ",
                  "SELECT p.* FROM package_scan p", "星号")
    expect_manual("JOIN",
                  "SELECT p.scan_time FROM package_scan p "
                  "JOIN package_scan q ON q.scan_id = p.scan_id", "JOIN")
    expect_manual("逗号多表",
                  "SELECT a.scan_time FROM package_scan a, package_scan b", "多表")
    expect_manual("WHERE 子查询",
                  "SELECT scan_time FROM package_scan WHERE scan_id IN "
                  "(SELECT scan_id FROM package_scan)", "子查询")
    expect_manual("EXISTS 子查询",
                  "SELECT scan_time FROM package_scan p WHERE EXISTS "
                  "(SELECT 1 FROM package_scan q WHERE q.scan_id = p.scan_id)",
                  "子查询")
    expect_manual("FROM 子查询",
                  "SELECT x.scan_time FROM (SELECT scan_time FROM package_scan) x",
                  "子查询")
    expect_manual("UNION",
                  "SELECT scan_time FROM package_scan UNION "
                  "SELECT scan_time FROM package_scan", "UNION")
    expect_manual("FOR UPDATE",
                  "SELECT scan_time FROM package_scan FOR UPDATE", "FOR UPDATE")
    expect_manual("多语句",
                  "SELECT scan_time FROM package_scan; SELECT 1", "多条语句")
    expect_manual("非目标表",
                  "SELECT scan_time FROM other_table", "非目标表")
    expect_manual("FROM 函数",
                  "SELECT scan_time FROM generate_series(1,2)", "函数")
    expect_manual("无 FROM",
                  "SELECT 1", "FROM")
    expect_manual("SELECT INTO",
                  "SELECT scan_time INTO TEMP x FROM package_scan", "INTO")
    expect_manual("非 SELECT",
                  "UPDATE package_scan SET scan_time = now()", "SELECT")
    expect_manual("MATERIALIZED CTE",
                  "WITH x AS MATERIALIZED (SELECT scan_time FROM package_scan) "
                  "SELECT * FROM x", "MATERIALIZED")
    expect_manual("括号未闭合",
                  "SELECT scan_time FROM package_scan WHERE (scan_id = 1", "解析失败")

    print("== 5. 安全细节：字面量/注释/关键字/函数名/限定符 ==")
    expect_rewrite(
        "字符串与注释中的同名文本不动",
        "SELECT 'scan_time' AS lit, /* scan_time */ scan_time FROM package_scan "
        "-- scan_time\nWHERE scan_time = 'x'",
        f"SELECT 'scan_time' AS lit, /* scan_time */ {NEW} AS {OLD} FROM package_scan "
        f"-- scan_time\nWHERE {NEW} = 'x'",
        count=2,
    )
    expect_rewrite(
        "E 字符串/dollar 字符串不动",
        "SELECT E'x''scan_time' AS e, $body$scan_time$body$ AS d, scan_time "
        "FROM package_scan",
        f"SELECT E'x''scan_time' AS e, $body$scan_time$body$ AS d, {NEW} AS {OLD} "
        f"FROM package_scan",
        count=1,
    )
    r = run(
        "SELECT count, count(*) FROM package_scan WHERE count > 1",
        old="count", new="renamed",
    )
    check(
        "关键字/函数名同名不误伤",
        r.status == "rewritten"
        and r.rewritten_sql == "SELECT renamed AS count, count(*) "
                               "FROM package_scan WHERE renamed > 1"
        and r.renamed_reference_count == 2,
        f"{r.status} {r.rewritten_sql!r} {r.reason}",
    )
    expect_manual(
        "无法识别的限定符 -> 歧义人工处理",
        "SELECT no_such_alias.scan_time FROM package_scan",
        "歧义",
    )
    r = run("SELECT no_such_alias.scan_time FROM package_scan")
    check("歧义条目不给改写 SQL", r.rewritten_sql is None and r.status == "manual",
          f"{r.status}")
    expect_rewrite(
        "未引用旧列的查询原样返回且计数 0",
        "SELECT scan_id, warehouse_id FROM package_scan WHERE warehouse_id = 3",
        "SELECT scan_id, warehouse_id FROM package_scan WHERE warehouse_id = 3",
        count=0,
    )
    expect_rewrite(
        "引号标识符大小写折叠匹配，输出名保留引号形式",
        'SELECT "SCAN_TIME" FROM package_scan WHERE "SCAN_TIME" > now()',
        f'SELECT {NEW} AS "SCAN_TIME" FROM package_scan WHERE {NEW} > now()',
        count=2,
    )
    expect_rewrite(
        "大小写不敏感裸词匹配",
        "SELECT SCAN_TIME FROM package_scan WHERE Scan_Time > now()",
        f"SELECT {NEW} AS SCAN_TIME FROM package_scan WHERE {NEW} > now()",
        count=2,
    )
    expect_rewrite(
        "尾部分号允许并保留",
        "SELECT scan_time FROM package_scan;",
        f"SELECT {NEW} AS {OLD} FROM package_scan;",
        count=1,
    )
    expect_rewrite(
        "count(*) 中的星号不是投影星号",
        "SELECT count(*) FROM package_scan WHERE scan_time > now()",
        f"SELECT count(*) FROM package_scan WHERE {NEW} > now()",
        count=1,
    )
    expect_rewrite(
        "LIMIT/OFFSET/FETCH 与未引用列共存",
        "SELECT scan_id FROM package_scan WHERE scan_time > now() "
        "ORDER BY scan_id LIMIT 10 OFFSET 5",
        f"SELECT scan_id FROM package_scan WHERE {NEW} > now() "
        f"ORDER BY scan_id LIMIT 10 OFFSET 5",
        count=1,
    )

    print("== 5b. 别名可见性边界回归 ==")
    expect_rewrite(
        "ORDER BY 表达式内含同名输出别名但引用表列，仍需改写",
        "SELECT scan_id, scan_time FROM package_scan ORDER BY scan_time + interval '1s'",
        f"SELECT scan_id, {NEW} AS {OLD} FROM package_scan "
        f"ORDER BY {NEW} + interval '1s'",
        count=2,
    )
    expect_rewrite(
        "GROUP BY 表达式中的表列改写（无输出别名可命中）",
        "SELECT warehouse_id FROM package_scan GROUP BY date_trunc('day', scan_time)",
        f"SELECT warehouse_id FROM package_scan GROUP BY date_trunc('day', {NEW})",
        count=1,
    )
    expect_rewrite(
        "ORDER BY 独立输出别名加 ASC 仍识别为输出引用",
        "SELECT scan_time st FROM package_scan ORDER BY st ASC",
        f"SELECT {NEW} st FROM package_scan ORDER BY st ASC",
        count=1,
    )
    expect_rewrite(
        "ORDER BY 独立输出别名加 NULLS FIRST 仍识别为输出引用",
        "SELECT scan_time st FROM package_scan ORDER BY st NULLS FIRST",
        f"SELECT {NEW} st FROM package_scan ORDER BY st NULLS FIRST",
        count=1,
    )
    expect_rewrite(
        "CTE 外层 ORDER BY 引用 CTE 输出名不动",
        "WITH n AS (SELECT scan_time FROM package_scan) "
        "SELECT scan_time FROM n ORDER BY scan_time",
        f"WITH n AS (SELECT {NEW} AS {OLD} FROM package_scan) "
        f"SELECT scan_time FROM n ORDER BY scan_time",
        count=1,
    )
    expect_rewrite(
        "CTE 与基表同名时外层按 CTE 作用域处理，不改写外层",
        "WITH package_scan AS (SELECT scan_id, scan_time FROM public.package_scan) "
        "SELECT scan_time FROM package_scan",
        f"WITH package_scan AS (SELECT scan_id, {NEW} AS {OLD} FROM public.package_scan) "
        f"SELECT scan_time FROM package_scan",
        count=1,
    )
    expect_rewrite(
        "CTE 遮蔽基表时，带 schema 限定仍指向基表并改写",
        "WITH package_scan AS (SELECT scan_id AS scan_time FROM public.package_scan) "
        "SELECT public.package_scan.scan_time FROM public.package_scan",
        f"WITH package_scan AS (SELECT scan_id AS scan_time FROM public.package_scan) "
        f"SELECT public.package_scan.{NEW} AS {OLD} FROM public.package_scan",
        count=1,
    )
    expect_manual(
        "三部分限定名拒绝",
        "SELECT a.b.scan_time FROM package_scan", "歧义",
    )
    expect_manual("双点限定拒绝",
                  "SELECT p..scan_time FROM package_scan p", "点号")
    expect_manual("前导点号拒绝",
                  "SELECT .scan_time FROM package_scan", "点号")
    expect_rewrite(
        "数字小数中的点号不被误判",
        "SELECT scan_time FROM package_scan WHERE weight_kg > 1.5",
        f"SELECT {NEW} AS {OLD} FROM package_scan WHERE weight_kg > 1.5",
        count=1,
    )
    expect_rewrite(
        "空字符串字面量与转义引号不影响识别",
        "SELECT '' AS empty, scan_time FROM package_scan WHERE scan_time <> ''",
        f"SELECT '' AS empty, {NEW} AS {OLD} FROM package_scan WHERE {NEW} <> ''",
        count=2,
    )

    print("== 6. 复杂真实报表 SQL 端到端 ==")
    nightly = (
        "SELECT warehouse_id, date_trunc('hour', scan_time) AS hour_bucket, "
        "count(*) AS scans, avg(weight_kg) AS avg_weight "
        "FROM public.package_scan p "
        "WHERE p.warehouse_id = 3 AND p.scan_time >= TIMESTAMPTZ '2026-11-12 18:00:00+00' "
        "GROUP BY warehouse_id, date_trunc('hour', p.scan_time) "
        "HAVING count(*) > 10 "
        "ORDER BY hour_bucket DESC, p.scan_time "
        "LIMIT 200"
    )
    expected = (
        "SELECT warehouse_id, date_trunc('hour', scanned_at) AS hour_bucket, "
        "count(*) AS scans, avg(weight_kg) AS avg_weight "
        "FROM public.package_scan p "
        "WHERE p.warehouse_id = 3 AND p.scanned_at >= TIMESTAMPTZ '2026-11-12 18:00:00+00' "
        "GROUP BY warehouse_id, date_trunc('hour', p.scanned_at) "
        "HAVING count(*) > 10 "
        "ORDER BY hour_bucket DESC, p.scanned_at "
        "LIMIT 200"
    )
    expect_rewrite("夜间聚合报表完整改写", nightly, expected, count=4)
    r = run(nightly)
    check("输出别名 hour_bucket/scans/avg_weight 全部保留",
          all(name in r.rewritten_sql for name in
              ("hour_bucket", "scans", "avg_weight")),
          r.rewritten_sql or "")

    print(f"\n通过 {_passed} 项，失败 {len(_failed)} 项")
    if _failed:
        print("失败明细：")
        for f in _failed:
            print(" -", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
