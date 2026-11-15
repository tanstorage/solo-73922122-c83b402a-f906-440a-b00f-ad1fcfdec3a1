# PostgreSQL 15 + HypoPG（Debian bookworm 上的 PGDG 官方扩展包）
FROM postgres:15-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-15-hypopg \
    && rm -rf /var/lib/apt/lists/*

# 初始化脚本（建表、预置 50 万行扫描明细）由官方 entrypoint 在首次启动时按文件名顺序执行
COPY initdb/ /docker-entrypoint-initdb.d/
