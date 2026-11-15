"""HypoPG 评估服务：只读 EXPLAIN（不执行查询、不建真实索引）。

流程（全部在同一条租约连接上，会话级假想索引天然与其他并发请求隔离）：
1. 借出连接时已完成 ROLLBACK + hypopg_reset 校验；
2. 先跑基线 EXPLAIN（此时会话里必须没有任何假想索引）；
3. 逐个 hypopg_create_index 建立假想 B-tree（不触碰真实系统目录）；
4. 再跑一次 EXPLAIN，比较总成本并遍历计划找出被采用/未采用的候选。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .config import Settings
from .db import TwoConnectionPool
from .schemas import CandidateResult, EvaluateRequest, EvaluateResponse, IndexCandidate
from .validation import SQLValidationError, validate_read_only_select

log = logging.getLogger("hypopg_api.service")


class EvaluationError(RuntimeError):
    """业务可预期错误（表/列不存在等），映射为 HTTP 400。"""


# ---------------- 标识符与目录校验 ----------------


def _split_table(table: str) -> tuple[str, str]:
    parts = table.split(".")
    if len(parts) == 1:
        return "public", parts[0]
    return parts[0], parts[1]


async def _check_relations_exist(
    conn: AsyncConnection, candidates: list[IndexCandidate]
) -> None:
    """在创建假想索引前校验 schema/表/列真实存在，错误一律 400。"""
    async with conn.cursor(row_factory=dict_row) as cur:
        for cand in candidates:
            schema, table = _split_table(cand.table)
            await cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                (schema, table),
            )
            if await cur.fetchone() is None:
                raise EvaluationError(f"候选引用的表不存在: {cand.table}")
            await cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (schema, table),
            )
            existing = {row["column_name"] for row in await cur.fetchall()}
            missing = [c for c in cand.columns if c not in existing]
            if missing:
                raise EvaluationError(
                    f"候选 {cand.table} 上不存在列: {', '.join(missing)}"
                )


def _build_create_index_sql(cand: IndexCandidate) -> psycopg.sql.Composed:
    schema, table = _split_table(cand.table)
    cols = psycopg.sql.SQL(", ").join(psycopg.sql.Identifier(c) for c in cand.columns)
    # 仅 B-tree（DEFAULT 访问方法），标识符全部经过 Identifier 转义，杜绝注入
    return psycopg.sql.SQL("CREATE INDEX ON {}.{} ({})").format(
        psycopg.sql.Identifier(schema),
        psycopg.sql.Identifier(table),
        cols,
    )


# ---------------- EXPLAIN 与计划解析 ----------------


async def _explain_json(conn: AsyncConnection, sql_text: str) -> dict:
    """只做 EXPLAIN（无 ANALYZE）：规划器估算，查询本身不会被执行。"""
    stmt = psycopg.sql.SQL("EXPLAIN (FORMAT JSON) {}").format(
        psycopg.sql.SQL(sql_text)
    )
    async with conn.cursor() as cur:
        await cur.execute(stmt)
        rows = await cur.fetchall()
    # EXPLAIN JSON 返回单行数组：[{"Plan": {...}, "Total Cost": ...}]
    return rows[0][0][0]


def _total_cost(plan: dict) -> float:
    return float(plan["Plan"]["Total Cost"])


def _collect_hypo_names(plan_node: dict, found: set[str]) -> None:
    """递归遍历计划树，收集所有 HypoPG 假想索引名（形如 <oid>btree_...）。"""
    name = plan_node.get("Index Name")
    if isinstance(name, str) and name.startswith("<") and ">" in name:
        found.add(name)
    for key in ("Plans", "Inner Subplan", "Subplan Name"):
        child = plan_node.get(key)
        if isinstance(child, dict):
            _collect_hypo_names(child, found)
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, dict):
                    _collect_hypo_names(item, found)


async def _list_hypo_indexes(conn: AsyncConnection) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT indexrelid, index_name, schema_name, table_name "
            "FROM hypopg_list_indexes ORDER BY indexrelid"
        )
        return await cur.fetchall()


async def _count_hypo_indexes(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM hypopg_list_indexes")
        return (await cur.fetchone())[0]


# ---------------- 主评估流程 ----------------


async def evaluate(
    pool: TwoConnectionPool,
    settings: Settings,
    req: EvaluateRequest,
    pause_seconds: float = 0.0,
    pause_mode: str = "asyncio",
    is_disconnected: "Callable[[], Awaitable[bool]] | None" = None,
) -> EvaluateResponse:
    # SQL 解析校验在连接外完成，非法输入不占用宝贵的两条连接
    if req.sample_id is not None:
        from .samples import get_sample

        sample = get_sample(req.sample_id)
        if sample is None:
            raise EvaluationError(f"未知 sample_id: {req.sample_id}")
        sql_text = validate_read_only_select(sample.sql_text)
    else:
        if not req.sql_text:
            raise SQLValidationError("sql_text 与 sample_id 必须提供其中之一")
        sql_text = validate_read_only_select(req.sql_text)

    async with pool.lease() as conn:
        # —— 基线：此时会话必须零假想索引（借出时已 reset，这里再观测一次留证）——
        before_baseline = await _count_hypo_indexes(conn)
        if before_baseline != 0:
            # 理论不可达：借出校验已保证；到达说明状态被污染，拒绝产出比较结果
            raise EvaluationError("会话存在残留假想索引，基线已中止以避免继承旧候选")

        await _check_relations_exist(conn, req.candidates)
        baseline_plan = await _explain_json(conn, sql_text)

        # —— 创建全部候选假想索引（仅后端内存，不创建真实索引）——
        # HypoPG 按调用顺序返回每个假想索引的 (indexrelid, indexname)，
        # 记录 OID 以便与候选定义精确对应。
        created_defs: list[str] = []
        created_oids: list[int] = []
        async with conn.cursor() as cur:
            for cand in req.candidates:
                ddl = _build_create_index_sql(cand)
                definition = ddl.as_string(conn)
                await cur.execute(
                    psycopg.sql.SQL("SELECT * FROM hypopg_create_index({})").format(
                        psycopg.sql.Literal(definition)
                    )
                )
                oid, _indexname = (await cur.fetchone())
                created_defs.append(definition)
                created_oids.append(int(oid))

        # 演示钩子：候选已创建、第二次 EXPLAIN 之前暂停，并监听客户端断连。
        # - asyncio 模式（默认）：暂停不占用数据库工作，断连检测立即生效，
        #   随后走标准取消清理路径（ROLLBACK + hypopg_reset 验证后才归还连接）；
        # - pg_sleep 模式（X-Demo-Pause-Mode: pg_sleep）：在租约连接上制造真实
        #   在途数据库工作，断连时先发取消信号停掉它，再走同一套清理路径。
        if pause_seconds > 0:
            if not settings.demo_pause_enabled:
                raise EvaluationError("演示暂停未启用（DEMO_PAUSE_ENABLED=false）")
            capped = min(pause_seconds, settings.demo_pause_max_seconds)
            log.info("演示暂停 %.2f 秒（模式=%s，候选已建立）", capped, pause_mode)

            async def _wait_disconnect() -> bool:
                if is_disconnected is None:
                    return False
                while not await is_disconnected():
                    await asyncio.sleep(0.1)
                return True

            if pause_mode == "pg_sleep":
                work_task = asyncio.create_task(
                    conn.execute("SELECT pg_sleep(%s)", (capped,))
                )
            else:
                work_task = asyncio.create_task(asyncio.sleep(capped))
            disconnect_task = asyncio.create_task(_wait_disconnect())
            try:
                done, _pending = await asyncio.wait(
                    {work_task, disconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                # 任务被外部取消：直接抛给租约层处理（其内部会取消 DB 工作并清理）
                work_task.cancel()
                disconnect_task.cancel()
                await asyncio.gather(work_task, disconnect_task,
                                     return_exceptions=True)
                raise

            if disconnect_task in done:
                log.info("检测到客户端断连，进入取消安全清理（模式=%s）", pause_mode)
                if pause_mode == "pg_sleep":
                    # 停止在途 pg_sleep，确保服务端工作真正停下。
                    # cancel() 是同步方法，放入线程执行。
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            asyncio.to_thread(conn.cancel), timeout=10.0
                        )
                work_task.cancel()
                await asyncio.gather(work_task, disconnect_task,
                                     return_exceptions=True)
                # 抛 CancelledError 复用租约的标准取消安全清理路径
                raise asyncio.CancelledError()

            # 正常暂停结束：取消监听任务（CancelledError 属 BaseException，
            # 不能用 suppress(Exception)，用 gather 收口）
            disconnect_task.cancel()
            await asyncio.gather(disconnect_task, return_exceptions=True)
            work_exc = work_task.exception()
            if work_exc is not None:
                raise work_exc

        hypo_meta = await _list_hypo_indexes(conn)
        hypothetical_plan = await _explain_json(conn, sql_text)

        # 显式清空本会话假想索引并回滚，随后在同一条连接上验证（观测字段留证）
        async with conn.cursor() as cur:
            await cur.execute("SELECT hypopg_reset()")
        after_reset = await _count_hypo_indexes(conn)

    # 租约退出时 full_cleanup 会再次 ROLLBACK + hypopg_reset 并校验后才归还

    # —— 结果组装（连接已归还，纯 CPU 计算）——
    used_names: set[str] = set()
    _collect_hypo_names(hypothetical_plan["Plan"], used_names)

    # 创建时已按候选顺序拿到 HypoPG 返回的 OID，直接精确映射，不依赖命名或顺序假设
    oid_to_def = {
        oid: created_defs[idx] for idx, oid in enumerate(created_oids)
    }
    adopted: list[CandidateResult] = []
    rejected: list[CandidateResult] = []
    for row in hypo_meta:
        result = CandidateResult(
            indexrelid=row["indexrelid"],
            index_name=row["index_name"],
            definition=oid_to_def.get(row["indexrelid"], ""),
            used_in_plan=row["index_name"] in used_names,
        )
        (adopted if result.used_in_plan else rejected).append(result)

    baseline_cost = _total_cost(baseline_plan)
    hypo_cost = _total_cost(hypothetical_plan)
    delta = round(hypo_cost - baseline_cost, 4)
    ratio = (
        round((baseline_cost - hypo_cost) / baseline_cost, 6)
        if baseline_cost > 0
        else None
    )

    return EvaluateResponse(
        baseline_plan=baseline_plan,
        baseline_total_cost=baseline_cost,
        hypothetical_plan=hypothetical_plan,
        hypothetical_total_cost=hypo_cost,
        cost_delta=delta,
        improvement_ratio=ratio,
        plan_changed=baseline_plan["Plan"].get("Node Type")
        != hypothetical_plan["Plan"].get("Node Type")
        or bool(used_names),
        adopted_candidates=adopted,
        rejected_candidates=rejected,
        session_hypothetical_indexes_before_baseline=before_baseline,
        session_hypothetical_indexes_after_reset=after_reset,
    )
