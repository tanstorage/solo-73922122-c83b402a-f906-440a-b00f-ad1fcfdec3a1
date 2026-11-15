-- 物流数仓：包裹扫描明细事实表（夜间扫描查询的目标表）
-- 该脚本由容器首次初始化时自动执行（docker-entrypoint-initdb.d）。

CREATE EXTENSION IF NOT EXISTS hypopg;

CREATE TABLE IF NOT EXISTS public.package_scan (
    scan_id        bigint PRIMARY KEY,
    package_no     text        NOT NULL,
    warehouse_id   integer     NOT NULL,
    conveyor_id    integer     NOT NULL,
    scan_time      timestamptz NOT NULL,
    scan_event     text        NOT NULL,
    destination    text        NOT NULL,
    service_type   text        NOT NULL,
    length_cm      numeric(6,2) NOT NULL,
    width_cm       numeric(6,2) NOT NULL,
    height_cm      numeric(6,2) NOT NULL,
    weight_kg      numeric(8,3) NOT NULL
);

COMMENT ON TABLE public.package_scan IS '包裹扫描明细（夜间自动化分拣线流水）';

-- 仅保留主键这一真实索引；评估请求里的候选全部走 HypoPG 假想索引，不创建真实索引。
