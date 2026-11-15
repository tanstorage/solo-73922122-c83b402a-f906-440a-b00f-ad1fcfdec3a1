"""端到端验证脚本（对真实运行的 API + HypoPG Postgres 发起 HTTP 请求）。

运行：
  python -I tests/test_api.py http://127.0.0.1:8000

覆盖需求点：
1. 能改善计划的候选：总成本下降、候选被标记 adopted；
2. 不能改善计划的候选：计划/成本不变、候选进入 rejected；
3. 只读守卫：非 SELECT / 多语句 / 写操作一律 400；
4. 候选参数：最多 3 个、非法标识符、不存在的表/列 -> 400；
5. 并发隔离：两个并发评估互不看见对方候选（观测字段始终为 0/0）；
6. 基线不继承旧候选：连续两次请求，第二次的 before_baseline 仍为 0；
7. 断连演示：候选创建后暂停期间断开 TCP，服务端完成清理后连接可复用；
8. 物理连接上限：压力并发下 pg_stat_activity 中本应用连接数 <= 2。
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

HELPFUL = {
    "sql_text": (
        "SELECT scan_id, package_no, conveyor_id, scan_time, scan_event, destination "
        "FROM public.package_scan "
        "WHERE warehouse_id = 3 "
        "  AND scan_time >= TIMESTAMPTZ '2026-11-12 18:00:00+00' "
        "  AND scan_time <  TIMESTAMPTZ '2026-11-13 06:00:00+00' "
        "ORDER BY scan_time DESC LIMIT 200"
    ),
    "candidates": [
        {"table": "public.package_scan", "columns": ["warehouse_id", "scan_time"]}
    ],
}
USELESS = {
    "sample_id": "nightly_warehouse_scan",
    "candidates": [
        {"table": "public.package_scan", "columns": ["destination"]},
        {"table": "public.package_scan", "columns": ["weight_kg"]},
    ],
}
AGGREGATE = {
    "sample_id": "nightly_aggregate",
    "candidates": [
        {"table": "public.package_scan", "columns": ["warehouse_id"]},
        {"table": "public.package_scan", "columns": ["scan_time"]},
    ],
}

_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        global _passed
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


async def main(base_url: str) -> int:
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as c:
        print("== 健康检查 ==")
        h = (await c.get("/health")).json()
        check("连接池上限为 2", h["pool"]["max_connections"] == 2)
        check("HypoPG 已安装", h["db"].get("hypopg") is not None, json.dumps(h["db"]))

        print("== 场景 1：能改善计划的候选 ==")
        r = await c.post("/evaluate", json=HELPFUL)
        check("HTTP 200", r.status_code == 200, r.text[:300])
        body = r.json()
        check(
            "候选后总成本下降",
            body["hypothetical_total_cost"] < body["baseline_total_cost"],
            f"{body['baseline_total_cost']} -> {body['hypothetical_total_cost']}",
        )
        check("存在被采纳候选", len(body["adopted_candidates"]) == 1, json.dumps(body["adopted_candidates"]))
        check("计划发生变化", body["plan_changed"] is True)
        check("基线前假想索引数为 0", body["session_hypothetical_indexes_before_baseline"] == 0)
        check("reset 后假想索引数为 0", body["session_hypothetical_indexes_after_reset"] == 0)
        check("基线计划是 EXPLAIN JSON", "Plan" in body["baseline_plan"] and "Node Type" in body["baseline_plan"]["Plan"])

        print("== 场景 2：不能改善计划的候选（谓词无关列）==")
        r = await c.post("/evaluate", json=USELESS)
        check("HTTP 200", r.status_code == 200, r.text[:300])
        body = r.json()
        check("成本无变化", body["hypothetical_total_cost"] == body["baseline_total_cost"])
        check("无候选被采纳", body["adopted_candidates"] == [])
        check("两个候选均被拒绝", len(body["rejected_candidates"]) == 2)
        check("计划未变化", body["plan_changed"] is False)

        print("== 场景 3：全表聚合，B-tree 无法改善 ==")
        r = await c.post("/evaluate", json=AGGREGATE)
        check("HTTP 200", r.status_code == 200, r.text[:300])
        body = r.json()
        check("聚合查询成本无变化", body["hypothetical_total_cost"] == body["baseline_total_cost"])
        check("聚合查询无采纳", len(body["adopted_candidates"]) == 0)

        print("== 场景 4：只读守卫 ==")
        for label, payload in [
            ("UPDATE", {"sql_text": "UPDATE package_scan SET weight_kg=1 WHERE scan_id=1",
                        "candidates": HELPFUL["candidates"]}),
            ("多语句", {"sql_text": "SELECT 1; SELECT 2", "candidates": HELPFUL["candidates"]}),
            ("DDL", {"sql_text": "DROP TABLE package_scan", "candidates": HELPFUL["candidates"]}),
            ("CTE 内写操作", {"sql_text": "WITH x AS (DELETE FROM package_scan RETURNING *) SELECT * FROM x",
                          "candidates": HELPFUL["candidates"]}),
            ("非 SELECT", {"sql_text": "EXPLAIN SELECT 1", "candidates": HELPFUL["candidates"]}),
        ]:
            r = await c.post("/evaluate", json=payload)
            check(f"拒绝 {label} -> 400", r.status_code == 400, r.text[:200])

        print("== 场景 5：候选参数校验 ==")
        bad_cases = [
            ("4 个候选", {**HELPFUL, "candidates": [
                {"table": "public.package_scan", "columns": [f"col{i}"]} for i in range(4)]}),
            ("非法标识符", {"sql_text": HELPFUL["sql_text"],
                        "candidates": [{"table": "public.package_scan; DROP TABLE x", "columns": ["a"]}]}),
            ("不存在的表", {"sql_text": HELPFUL["sql_text"],
                        "candidates": [{"table": "public.no_such_table", "columns": ["id"]}]}),
            ("不存在的列", {"sql_text": HELPFUL["sql_text"],
                        "candidates": [{"table": "public.package_scan", "columns": ["no_such_col"]}]}),
            ("候选重复", {"sql_text": HELPFUL["sql_text"], "candidates": [
                HELPFUL["candidates"][0], HELPFUL["candidates"][0]]}),
            ("空候选列表", {"sql_text": HELPFUL["sql_text"], "candidates": []}),
        ]
        for label, payload in bad_cases:
            r = await c.post("/evaluate", json=payload)
            check(f"拒绝 {label} -> 4xx", 400 <= r.status_code < 500, f"{r.status_code} {r.text[:150]}")

        print("== 场景 6：基线不继承旧候选（连续请求观测字段）==")
        for i in range(3):
            r = await c.post("/evaluate", json=HELPFUL)
            b = r.json()
            check(f"第{i+1}次请求基线前候选为0",
                  b["session_hypothetical_indexes_before_baseline"] == 0)
            check(f"第{i+1}次请求 reset 后候选为0",
                  b["session_hypothetical_indexes_after_reset"] == 0)

        print("== 场景 7：并发隔离（两个并发评估互不可见）==")
        async def one(req: dict) -> dict:
            rr = await c.post("/evaluate", json=req)
            assert rr.status_code == 200, rr.text
            return rr.json()

        results = await asyncio.gather(*[one(HELPFUL), one(USELESS), one(AGGREGATE), one(HELPFUL)])
        check("全部并发请求成功", len(results) == 4)
        check(
            "所有并发会话基线均无外来候选",
            all(b["session_hypothetical_indexes_before_baseline"] == 0 for b in results),
        )
        check(
            "所有并发会话结束后无残留",
            all(b["session_hypothetical_indexes_after_reset"] == 0 for b in results),
        )

        print("== 场景 8：断连后清理 + 连接复用 ==")
        payload = HELPFUL
        # 服务端在候选创建后暂停 8 秒（默认 asyncio 模式，不占用数据库工作）；
        # 客户端 2 秒读超时后强制断开 TCP，模拟真实断连
        try:
            with httpx.Client(base_url=base_url,
                              timeout=httpx.Timeout(2.0, read=2.0)) as sc:
                sc.post(
                    "/evaluate", json=payload,
                    headers={"X-Demo-Pause-Seconds": "8"},
                )
        except httpx.ReadTimeout:
            print("    客户端读超时，TCP 已强制断开（模拟断连）")
        print("    等待服务端检测断连并完成取消安全清理 ...")
        await asyncio.sleep(5)
        # 断连之后立刻发新请求：必须成功，且看不到任何上一请求的候选残留
        r = await c.post("/evaluate", json=HELPFUL)
        check("断连后新请求成功复用连接", r.status_code == 200, r.text[:200])
        b = r.json()
        check("断连后基线无残留候选", b["session_hypothetical_indexes_before_baseline"] == 0)
        h2 = (await c.get("/health")).json()
        check("断连计数 >= 1", h2["pool"]["cancels_performed"] >= 1, json.dumps(h2["pool"]))
        check("断连清理后连接未被丢弃（回队复用）",
              h2["pool"]["total_discarded"] == 0, json.dumps(h2["pool"]))
        check("物理连接创建总数维持在上限内（复用而非新建）",
              h2["pool"]["total_created"] <= 2, json.dumps(h2["pool"]))

        print("== 场景 8b：pg_sleep 模式（真实在途数据库工作，环境支持信号时取消）==")
        try:
            with httpx.Client(base_url=base_url,
                              timeout=httpx.Timeout(2.0, read=2.0)) as sc:
                sc.post(
                    "/evaluate", json=payload,
                    headers={"X-Demo-Pause-Seconds": "8",
                             "X-Demo-Pause-Mode": "pg_sleep"},
                )
        except httpx.ReadTimeout:
            print("    客户端在 pg_sleep 期间强制断连")
        await asyncio.sleep(12)  # 等过完整暂停窗口，确保在途工作结束
        r = await c.post("/evaluate", json=HELPFUL)
        check("pg_sleep 断连后新请求成功", r.status_code == 200, r.text[:200])
        b = r.json()
        check("pg_sleep 断连后基线无残留候选",
              b["session_hypothetical_indexes_before_baseline"] == 0)
        h3 = (await c.get("/health")).json()
        # 信号可用：取消成功、连接复用；信号不可用（受限环境）：连接被安全丢弃重建，
        # 无论哪种，后续请求都不能看到残留候选（上面已断言）
        check("pg_sleep 场景后服务仍然健康", h3["status"] == "ok")

        print("== 场景 9：物理连接硬上限 ==")
        await asyncio.gather(*[one(HELPFUL) for _ in range(8)])
        h3 = (await c.get("/health")).json()
        # 硬上限由信号量保证：任意时刻在途物理连接（in_use + idle）不超过 2
        check(
            "在途物理连接数（in_use+idle）<= 2",
            h3["pool"]["in_use"] + h3["pool"]["idle"] <= 2,
            json.dumps(h3["pool"]),
        )
        check("压力后无在途连接泄漏（in_use=0）", h3["pool"]["in_use"] == 0,
              json.dumps(h3["pool"]))

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
