"""两条连接上限的租约式连接池 + 取消安全的清理逻辑。

关键设计：
- 物理连接数硬上限为 2（信号量 + 空闲队列），单 uvicorn worker 单进程内全局复用；
- HypoPG 的假想索引是 **会话级** 状态，天然按连接隔离，并发评估互不可见；
- 每次借出连接先做 reset 校验（ROLLBACK + hypopg_reset），基线绝不继承上一请求遗留候选；
- 请求报错或客户端断连（CancelledError）时，先让服务端查询真正停止
  （conn.cancel() 并等待协程退出），再回滚事务、清空假想索引，全部完成后才归还；
- 清理无法确认干净的连接直接关闭丢弃，绝不放回队列；迟到的清理作用于当前请求自己的
  连接，下一请求只会从队列取到已验证干净的连接，因此不会被取消或误删候选。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

import psycopg
from psycopg import AsyncConnection

from .config import Settings

log = logging.getLogger("hypopg_api.pool")

_CANCEL_WAIT_SECONDS = 10.0
_RESET_RETRIES = 2


class PoolClosedError(RuntimeError):
    pass


class TwoConnectionPool:
    """信号量限流 + 空闲队列复用，物理连接永不超过 max_connections（=2）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._max = settings.pool_max_connections
        self._sem = asyncio.Semaphore(self._max)
        self._idle: asyncio.Queue[AsyncConnection] = asyncio.Queue()
        self._closed = False
        self._total_created = 0
        self._total_discarded = 0
        self._verifications = 0
        self._resets = 0
        self._cancels = 0
        self._in_use = 0
        self._stats_lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    async def open(self) -> None:
        # 预建一条连接，提前暴露数据库不可达问题
        conn = await self._new_connection()
        await self._idle.put(conn)

    async def close(self) -> None:
        self._closed = True
        while True:
            try:
                conn = self._idle.get_nowait()
            except asyncio.QueueEmpty:
                break
            await _silent_close(conn)

    # ---------- 内部工具 ----------

    async def _new_connection(self) -> AsyncConnection:
        conn = await AsyncConnection.connect(
            self._settings.db_dsn,
            autocommit=False,
            application_name="hypopg-eval-api",
        )
        self._total_created += 1
        # 会话级只读 + 超时：即使校验被绕过，数据库也拒绝写入；慢查询有兜底
        async with conn.cursor() as cur:
            await cur.execute(
                psycopg.sql.SQL("SET statement_timeout = {}").format(
                    psycopg.sql.Literal(self._settings.statement_timeout_ms)
                )
            )
            await cur.execute("SET default_transaction_read_only = on")
            await cur.execute("SET lock_timeout = 2000")
            await cur.execute("SET idle_in_transaction_session_timeout = 60000")
            await cur.execute("ROLLBACK")
            await cur.execute("SELECT hypopg_reset()")
            await cur.execute("COMMIT")
        return conn

    async def _checkin(self, conn: AsyncConnection, *, discard: bool) -> None:
        async with self._stats_lock:
            self._in_use -= 1
            if discard:
                self._total_discarded += 1
        if discard:
            log.warning("连接无法确认已清理干净，丢弃该物理连接（不归还队列）")
            await _silent_close(conn)
        else:
            await self._idle.put(conn)
        self._sem.release()

    async def _acquire_verified(self) -> AsyncConnection:
        """取得一条经过干净校验的连接（队列复用或新建）。"""
        try:
            conn = self._idle.get_nowait()
        except asyncio.QueueEmpty:
            return await self._new_connection()

        # 复用前验证：事务已结束、无遗留假想索引；不达标则重置，重置失败则丢弃重建。
        self._verifications += 1
        ok = await full_cleanup(conn, cancelled=False)
        if not ok:
            await _silent_close(conn)
            async with self._stats_lock:
                self._total_discarded += 1
            return await self._new_connection()
        self._resets += 1
        return conn

    async def _finish(
        self, conn: AsyncConnection | None, *, cancelled: bool, acquired: bool
    ) -> None:
        """统一收尾：完整清理后才归还/释放信号量。整个过程屏蔽取消。"""
        if conn is None:
            if acquired:
                self._sem.release()
                async with self._stats_lock:
                    self._in_use -= 1
            return
        ok = await _uncancellable(full_cleanup(conn, cancelled=cancelled))
        await self._checkin(conn, discard=not ok)

    # ---------- 对外租约 API ----------

    @contextlib.asynccontextmanager
    async def lease(self):
        if self._closed:
            raise PoolClosedError("连接池已关闭")

        await self._sem.acquire()
        acquired = True
        conn: AsyncConnection | None = None
        try:
            async with self._stats_lock:
                self._in_use += 1
            conn = await self._acquire_verified()
            yield conn
        except asyncio.CancelledError:
            # 客户端断连：当前请求任务被取消。先让服务端工作真正停下来、
            # 清掉事务与假想索引，再释放信号量，否则下一个请求可能拿到脏连接。
            log.info("请求被取消（客户端断连），开始取消安全清理")
            self._cancels += 1
            await self._finish(conn, cancelled=True, acquired=acquired)
            conn = None
            raise
        except BaseException:
            # 其他异常同样完整清理后再归还，失败不返回部分比较
            await self._finish(conn, cancelled=False, acquired=acquired)
            conn = None
            raise
        else:
            await self._finish(conn, cancelled=False, acquired=acquired)
            conn = None

    def stats(self) -> dict:
        return {
            "max_connections": self._max,
            "in_use": self._in_use,
            "idle": self._idle.qsize(),
            "total_created": self._total_created,
            "total_discarded": self._total_discarded,
            "checkout_verifications": self._verifications,
            "resets_performed": self._resets,
            "cancels_performed": self._cancels,
        }


# ---------------- 模块级清理原语 ----------------


async def _uncancellable(coro):
    """让清理逻辑屏蔽再次送达的 CancelledError，跑完为止。"""
    while True:
        try:
            return await asyncio.shield(coro)
        except asyncio.CancelledError:
            # 清理期间再次收到取消：继续等，绝不半途而废
            continue


async def _silent_close(conn: AsyncConnection) -> None:
    with contextlib.suppress(Exception):
        await conn.close()


async def _reset_session_state(conn: AsyncConnection) -> bool:
    """ROLLBACK + hypopg_reset，带少量重试并验证。成功返回 True。

    注意：非自动提交模式下每条 SQL 都会隐式开启事务，因此 hypopg_reset()
    之后必须再发一个 ROLLBACK 收尾，协议层事务状态才能回到 IDLE。
    HypoPG 假想索引是后端内存中的非事务状态，ROLLBACK 不会让 reset 失效。
    """
    for attempt in range(_RESET_RETRIES + 1):
        try:
            async with conn.cursor() as cur:
                await cur.execute("ROLLBACK")
                await cur.execute("SELECT hypopg_reset()")
                # 验证查询要放在收尾 ROLLBACK 之前（查询本身会开启隐式事务）
                await cur.execute("SELECT count(*) FROM hypopg_list_indexes")
                hypo_count = (await cur.fetchone())[0]
                await cur.execute("ROLLBACK")
            # 验证 1：协议层事务状态必须回到 IDLE（无打开事务/在途查询）
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                log.warning(
                    "重置后事务状态非 IDLE: %s", conn.info.transaction_status
                )
                continue
            # 验证 2：HypoPG 假想索引必须为零
            if hypo_count == 0:
                return True
            log.warning("重置后仍残留 %s 个假想索引", hypo_count)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise
        except Exception as exc:
            log.warning("会话重置第 %s 次尝试失败: %r", attempt + 1, exc)
            with contextlib.suppress(Exception):
                await conn.rollback()
    return False


async def full_cleanup(conn: AsyncConnection, *, cancelled: bool) -> bool:
    """确保连接上的真实数据库工作已停止，且事务/假想索引全部清空。

    返回 True 表示连接干净可复用；False 表示无法保证，调用方必须丢弃。
    """
    # 1) 连接不在 idle（查询在跑/事务打开）：先发取消并等待，确保服务端工作停止。
    #    注意 psycopg3 的 cancel() 是同步阻塞方法（内部等待查询确认终止），
    #    必须放到线程中调用，绝不能直接 await（其返回值为 None）。
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(conn.cancel), timeout=_CANCEL_WAIT_SECONDS
            )
        except Exception as exc:
            # 取消信号无法送达或查询未在时限内终止：连接无法保证可复用，
            # 交由下方 reset 验证失败后关闭丢弃（绝不脏连接回队）
            log.warning("conn.cancel() 未成功终止查询: %r", exc)
        # 让出事件循环，让被取消的查询协程收到 QueryCanceled 并退出
        await asyncio.sleep(0)

    # 2) 回滚事务并清空 HypoPG 假想索引（重试 + 验证）
    try:
        return await _reset_session_state(conn)
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        log.warning("连接已不可用，清理判定为丢弃: %r", exc)
        await _silent_close(conn)
        return False
    except Exception as exc:  # 防御性兜底
        log.error("清理发生未预期异常，连接丢弃: %r", exc)
        await _silent_close(conn)
        return False
