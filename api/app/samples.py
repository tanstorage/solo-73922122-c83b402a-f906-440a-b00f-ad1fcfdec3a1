"""预置样例 SQL 与配套候选索引（/docs 页面可直接试用）。

每组样例同时给出：
- 一个能改善计划的候选（列顺序与过滤/排序条件匹配）；
- 一个不能改善计划的候选（列不在查询条件里，规划器不会采用）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateExample:
    table: str
    columns: list[str]
    helps: bool
    note: str


@dataclass(frozen=True)
class SampleQuery:
    sample_id: str
    title: str
    sql_text: str
    helpful_candidates: list[CandidateExample]
    useless_candidates: list[CandidateExample]


SAMPLES: list[SampleQuery] = [
    SampleQuery(
        sample_id="nightly_warehouse_scan",
        title="夜间某仓指定时段扫描流水（范围过滤 + 排序，索引可改善）",
        sql_text=(
            "SELECT scan_id, package_no, conveyor_id, scan_time, scan_event, destination\n"
            "FROM public.package_scan\n"
            "WHERE warehouse_id = 3\n"
            "  AND scan_time >= TIMESTAMPTZ '2026-11-12 18:00:00+00'\n"
            "  AND scan_time <  TIMESTAMPTZ '2026-11-13 06:00:00+00'\n"
            "ORDER BY scan_time DESC\n"
            "LIMIT 200"
        ),
        helpful_candidates=[
            CandidateExample(
                table="public.package_scan",
                columns=["warehouse_id", "scan_time"],
                helps=True,
                note="等值列在前、排序列在后，可走 Index Scan 并免排序，成本显著下降",
            ),
        ],
        useless_candidates=[
            CandidateExample(
                table="public.package_scan",
                columns=["destination"],
                helps=False,
                note="destination 不在过滤/排序列中，规划器不会采用该假想索引",
            ),
            CandidateExample(
                table="public.package_scan",
                columns=["weight_kg"],
                helps=False,
                note="纯测量列，与查询谓词无关，计划保持 Seq Scan",
            ),
        ],
    ),
    SampleQuery(
        sample_id="nightly_aggregate",
        title="全网夜间扫描量聚合（需扫描全表，B-tree 无法改善）",
        sql_text=(
            "SELECT warehouse_id, scan_event, count(*) AS scans, avg(weight_kg) AS avg_weight\n"
            "FROM public.package_scan\n"
            "GROUP BY warehouse_id, scan_event\n"
            "ORDER BY warehouse_id, scan_event"
        ),
        helpful_candidates=[],
        useless_candidates=[
            CandidateExample(
                table="public.package_scan",
                columns=["warehouse_id"],
                helps=False,
                note="聚合要读取全部行，优化器偏好 Seq Scan + HashAggregate，单列 B-tree 不被采用",
            ),
            CandidateExample(
                table="public.package_scan",
                columns=["scan_time"],
                helps=False,
                note="没有任何时间谓词，索引无法缩小扫描范围",
            ),
        ],
    ),
]


def get_sample(sample_id: str) -> SampleQuery | None:
    return next((s for s in SAMPLES if s.sample_id == sample_id), None)
