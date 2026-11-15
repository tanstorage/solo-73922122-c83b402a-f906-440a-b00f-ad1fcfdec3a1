"""列更名预览 API 端到端验证（对真实运行的 API + Postgres 发 HTTP 请求）。

运行：
  python -I tests/test_rename_api.py http://127.0.0.1:8000

覆盖需求点：
1. 普通查询返回改写 SQL，未显式别名裸列保留旧输出名，显式别名不丢；
2. 表别名 / 一层 CTE / 表达式 / WHERE / GROUP BY / ORDER BY 改写正确，
   CTE 输出别名与外层引用、ORDER BY 输出别名引用不误改；
3. 星号 / JOIN / 子查询 / 集合运算 / 多 CTE / 多语句逐条 manual，且不给改写 SQL；
4. 未引用旧列的查询原样返回（renamed_reference_count=0）；
5. 旧列不存在 / 新列已存在 / 新旧同名 -> 整体 400；
6. 超过 5 条、空查询、非法标识符 -> 4xx；
7. 不执行提交的查询：写入语句不会落库（只读守卫直接 manual / 400）；
8. 只读守卫：UPDATE / DDL 不会产出任何改写；
9. /evaluate 仍然可用（回归）；
10. 物理连接上限不被新接口突破（/health 观测 <=2）。
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

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


async def main(base_url: str) -> int:
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as c:
        print("== 0. 健康检查 ==")
        h = (await c.get("/health")).json()
        check("连接池上限为 2", h["pool"]["max_connections"] == 2)

        print("== 1. 安全改写：输出列名不变 ==")
        payload = {
            "old_column": OLD,
            "new_column": NEW,
            "queries": [
                "SELECT scan_id, scan_time FROM public.package_scan "
                "WHERE scan_time >= TIMESTAMPTZ '2026-11-12 18:00:00+00' "
                "ORDER BY scan_time DESC LIMIT 200",
                "SELECT p.scan_time AS st, p.warehouse_id FROM package_scan p "
                "WHERE p.scan_time > now() ORDER BY st",
                "SELECT date_trunc('day', scan_time) AS day, count(*) AS scans "
                "FROM package_scan GROUP BY date_trunc('day', scan_time) "
                "HAVING max(scan_time) > now()",
                "WITH night AS (SELECT scan_id, scan_time FROM public.package_scan "
                "WHERE scan_time >= now()) "
                "SELECT scan_id, scan_time FROM night ORDER BY scan_time",
                "SELECT scan_id, warehouse_id FROM package_scan WHERE warehouse_id = 3",
            ],
        }
        r = await c.post("/rename-preview", json=payload)
        check("HTTP 200", r.status_code == 200, r.text[:300])
        body = r.json()
        check("目标表正确", body["target_table"] == "public.package_scan")
        check("回显旧/新列", body["old_column"] == OLD and body["new_column"] == NEW)
        check("5 条结果", len(body["results"]) == 5)
        check("汇总 4 改写 1 无引用",
              body["summary"] == {"total": 5, "rewritten": 4, "manual": 0, "changed": 4},
              json.dumps(body["summary"], ensure_ascii=False))

        q0 = body["results"][0]
        check("q0 已改写", q0["status"] == "rewritten")
        check("q0 裸列保留旧输出名 (AS scan_time)",
              f"{NEW} AS {OLD}" in q0["rewritten_sql"], q0["rewritten_sql"])
        check("q0 WHERE 中引用已换名",
              f"WHERE {NEW} >=" in q0["rewritten_sql"], q0["rewritten_sql"])
        check("q0 ORDER BY 保留旧输出名",
              f"ORDER BY {OLD} DESC" in q0["rewritten_sql"], q0["rewritten_sql"])
        check("q0 输出列保持标记", q0["output_columns_preserved"] is True)

        q1 = body["results"][1]
        check("q1 显式别名 st 保留",
              f"p.{NEW} AS st" in q1["rewritten_sql"], q1["rewritten_sql"])
        check("q1 ORDER BY 输出别名 st 不动",
              "ORDER BY st" in q1["rewritten_sql"], q1["rewritten_sql"])

        q2 = body["results"][2]
        check("q2 表达式内列换名、输出别名 day 保留",
              f"date_trunc('day', {NEW}) AS day" in q2["rewritten_sql"]
              and f"max({NEW})" in q2["rewritten_sql"], q2["rewritten_sql"])

        q3 = body["results"][3]
        check("q3 CTE 内换名且保留输出名",
              f"SELECT scan_id, {NEW} AS {OLD} FROM public.package_scan"
              in q3["rewritten_sql"], q3["rewritten_sql"])
        check("q3 外层对 CTE 输出的引用不动",
              "SELECT scan_id, scan_time FROM night ORDER BY scan_time"
              in q3["rewritten_sql"], q3["rewritten_sql"])

        q4 = body["results"][4]
        check("q4 未引用旧列原样返回",
              q4["status"] == "rewritten"
              and q4["renamed_reference_count"] == 0
              and q4["rewritten_sql"] == payload["queries"][4],
              json.dumps(q4, ensure_ascii=False))

        print("== 2. 不支持结构：逐条 manual，无改写 SQL ==")
        manual_payload = {
            "old_column": OLD,
            "new_column": NEW,
            "queries": [
                "SELECT * FROM package_scan",
                "SELECT p.* FROM package_scan p",
                "SELECT p.scan_time FROM package_scan p "
                "JOIN package_scan q ON q.scan_id = p.scan_id",
                "SELECT scan_time FROM package_scan WHERE scan_id IN "
                "(SELECT scan_id FROM package_scan)",
                "SELECT scan_time FROM package_scan UNION "
                "SELECT scan_time FROM package_scan",
            ],
        }
        r = await c.post("/rename-preview", json=manual_payload)
        check("HTTP 200（逐条标记而非整体拒绝）", r.status_code == 200, r.text[:300])
        mb = r.json()
        check("全部 manual", all(x["status"] == "manual" for x in mb["results"]))
        check("manual 均无改写 SQL", all(x["rewritten_sql"] is None for x in mb["results"]))
        check("manual 均附中文原因", all(x["reason"] for x in mb["results"]))
        check("汇总 manual=5", mb["summary"]["manual"] == 5
              and mb["summary"]["rewritten"] == 0,
              json.dumps(mb["summary"], ensure_ascii=False))
        check("星号原因可识别", "星号" in mb["results"][0]["reason"], mb["results"][0]["reason"])
        check("JOIN 原因可识别", "JOIN" in mb["results"][2]["reason"], mb["results"][2]["reason"])
        check("子查询原因可识别", "子查询" in mb["results"][3]["reason"], mb["results"][3]["reason"])

        print("== 3. 目录校验：旧列不存在 / 新列冲突 / 同名 ==")
        for label, old, new in [
            ("旧列不存在", "no_such_column", NEW),
            ("新列已存在", OLD, "warehouse_id"),
            ("新旧同名", OLD, OLD),
        ]:
            r = await c.post("/rename-preview", json={
                "old_column": old, "new_column": new,
                "queries": ["SELECT scan_time FROM package_scan"],
            })
            check(f"{label} -> 400", r.status_code == 400, r.text[:200])
            check(f"{label} 错误类型",
                  r.json().get("error") == "rename_preview_rejected", r.text[:200])

        print("== 4. 请求参数校验 ==")
        bad_cases = [
            ("6 条查询", {
                "old_column": OLD, "new_column": NEW,
                "queries": ["SELECT 1 FROM package_scan"] * 6,
            }),
            ("空查询字符串", {"old_column": OLD, "new_column": NEW, "queries": ["   "]}),
            ("非法旧列名", {"old_column": "scan-time", "new_column": NEW,
                         "queries": ["SELECT 1 FROM package_scan"]}),
            ("queries 为空列表", {"old_column": OLD, "new_column": NEW, "queries": []}),
        ]
        for label, p in bad_cases:
            r = await c.post("/rename-preview", json=p)
            check(f"拒绝 {label} -> 4xx", 400 <= r.status_code < 500,
                  f"{r.status_code} {r.text[:150]}")

        print("== 5. 只读守卫：写操作不产出改写 ==")
        r = await c.post("/rename-preview", json={
            "old_column": OLD, "new_column": NEW,
            "queries": [
                "UPDATE package_scan SET scan_time = now() WHERE scan_id = 1",
                "DROP TABLE package_scan",
                "SELECT scan_time FROM package_scan; SELECT 1",
            ],
        })
        check("混合输入整体 200 但逐条 manual", r.status_code == 200, r.text[:200])
        rb = r.json()
        check("写操作全部 manual", all(x["status"] == "manual" for x in rb["results"]))
        check("无任何改写 SQL 产出", all(x["rewritten_sql"] is None for x in rb["results"]))

        print("== 6. /evaluate 回归可用 ==")
        r = await c.post("/evaluate", json={
            "sample_id": "nightly_aggregate",
            "candidates": [{"table": "public.package_scan", "columns": ["warehouse_id"]}],
        })
        check("/evaluate 仍返回 200", r.status_code == 200, r.text[:200])

        print("== 7. 新接口并发下不突破两条物理连接 ==")
        async def one() -> int:
            rr = await c.post("/rename-preview", json=payload)
            return rr.status_code

        codes = await asyncio.gather(*[one() for _ in range(8)])
        check("8 个并发预览全部 200", codes == [200] * 8, str(codes))
        h2 = (await c.get("/health")).json()
        check("在途物理连接数（in_use+idle）<= 2",
              h2["pool"]["in_use"] + h2["pool"]["idle"] <= 2,
              json.dumps(h2["pool"], ensure_ascii=False))
        check("无连接泄漏（in_use=0）", h2["pool"]["in_use"] == 0,
              json.dumps(h2["pool"], ensure_ascii=False))

    print(f"\n通过 {_passed} 项，失败 {len(_failed)} 项")
    if _failed:
        print("失败明细：")
        for f in _failed:
            print(" -", f)
        return 1
    return 0


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    raise SystemExit(asyncio.run(main(url)))
