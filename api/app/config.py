"""进程级配置（全部来自环境变量，带安全默认值）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是整数，实际为 {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"环境变量 {name} 必须为正整数，实际为 {value}")
    return value


@dataclass(frozen=True)
class Settings:
    db_dsn: str
    pool_max_connections: int
    statement_timeout_ms: int
    # 演示用暂停开关与上限（秒）。生产应保持关闭。
    demo_pause_enabled: bool
    demo_pause_max_seconds: float


def get_settings() -> Settings:
    return Settings(
        db_dsn=os.environ.get(
            "DB_DSN", "postgresql://app:app@127.0.0.1:5433/logistics"
        ),
        # 需求硬性约束：单 API 进程最多复用两条数据库连接
        pool_max_connections=min(2, _int("POOL_MAX_CONNECTIONS", 2)),
        statement_timeout_ms=_int("STATEMENT_TIMEOUT_MS", 30_000),
        demo_pause_enabled=os.environ.get("DEMO_PAUSE_ENABLED", "false").lower()
        in ("1", "true", "yes", "on"),
        demo_pause_max_seconds=float(os.environ.get("DEMO_PAUSE_MAX_SECONDS", "30")),
    )
