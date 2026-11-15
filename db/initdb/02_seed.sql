-- 预置 50 万行包裹扫描明细。
-- 全部用纯 SQL、确定性表达式生成（不依赖外部数据文件，docker init 可复现）。
-- 时间窗口覆盖最近 60 天，夜间扫描数据集中在 18:00-次日 05:59。
-- 注意：先把序列转成 bigint，避免乘法在 int4 下溢出。
-- 评估场景不需要 JIT；提前关闭也保证精简环境（无 LLVM 运行库）下脚本可运行。
SET jit = off;
SET max_parallel_workers_per_gather = 0;

WITH base AS (
    SELECT g::bigint AS g FROM generate_series(1, 500000) AS g
)
INSERT INTO public.package_scan
SELECT
    g                                                          AS scan_id,
    'PKG' || lpad((g % 400000)::text, 10, '0')                 AS package_no,
    (1 + (g % 12))::int                                        AS warehouse_id,
    (1 + (g % 36))::int                                        AS conveyor_id,
    -- 锚定 2026-11-13 00:00 UTC，向前推 0~59 天，小时集中在 18..29（即 18:00-次日 05:59）
    (timestamptz '2026-11-13 00:00:00+00'
        - (((g % 60) || ' days')::interval)
        + ((((g * 7919) % 12) + 18) || ' hours')::interval
        + (((g * 104729) % 3600) || ' seconds')::interval)    AS scan_time,
    (ARRAY['INBOUND','SORT','OUTBOUND','LOAD','EXCEPTION'])[1 + (g % 5)] AS scan_event,
    (ARRAY['EAST','SOUTH','WEST','NORTH','CENTRAL'])[1 + (g % 5)]        AS destination,
    (ARRAY['STANDARD','EXPRESS','ECONOMY'])[1 + (g % 3)]                 AS service_type,
    round((20 + (g * 13) % 180)::numeric, 2)                   AS length_cm,
    round((15 + (g * 17) % 120)::numeric, 2)                  AS width_cm,
    round((5  + (g * 23) % 90)::numeric, 2)                    AS height_cm,
    round((0.2 + ((g * 29) % 2500) / 100.0)::numeric, 3)       AS weight_kg
FROM base;

-- 让规划器拥有准确统计信息，EXPLAIN 成本才稳定可比较。
-- 评估场景不需要 JIT；关闭也保证精简环境（无 LLVM 运行库）下 ANALYZE 可运行。
SET jit = off;
ANALYZE public.package_scan;
