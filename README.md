# 夜间包裹扫描查询 · 索引评估与列更名预览 API

物流数仓工程师在维护夜间报表时有两类只读诉求，本服务一并提供：

1. **候选索引成本评估**（`/evaluate`）：基于 **FastAPI + psycopg(v3) + PostgreSQL 15 + HypoPG**，
   只做 **EXPLAIN 估算**——不执行查询（`EXPLAIN` 不带 `ANALYZE`），也不创建真实索引
   （HypoPG 假想索引仅存于后端内存）。
2. **列更名 SQL 预览**（`/rename-preview`）：准备给 `public.package_scan` 的一列改名前，
   静态检查并改写夜间报表 SQL。**只读取 information_schema 列目录，不执行提交的查询、
   不修改表结构**；星号、歧义或不支持的结构逐条标注需人工处理，绝不给出半成品改写。

## 能力与约束（与需求逐条对应）

| 需求 | 实现 |
| --- | --- |
| 提交只读 SELECT + 最多 3 个候选（表名 + 有序列名） | `POST /evaluate`，Pydantic 校验候选数 1~3、标识符合法性、候选不重复 |
| 返回无候选 / 全候选启用的 EXPLAIN JSON、总成本、采用的候选 | 响应含 `baseline_plan`、`hypothetical_plan`、两套 `total_cost`、`adopted_candidates` / `rejected_candidates` |
| 只估算，不执行查询、不建真实索引 | `EXPLAIN (FORMAT JSON)`；`hypopg_create_index()` 只在会话内存造假想索引 |
| 单 API 进程最多复用两条数据库连接 | `TwoConnectionPool`：信号量硬上限 2，单 uvicorn worker；`/health` 可观测 |
| 并发评估只能看见各自候选 | HypoPG 假想索引是**会话级**状态，一条租约连接一个会话，天然隔离 |
| 每次基线不继承旧候选 | 借出连接先 `ROLLBACK + hypopg_reset` 并验证为 0 才放行；响应字段留证 |
| 报错 / 断连时等真实 DB 工作停止、事务与假想索引清干净后才归还 | 租约上下文统一 `full_cleanup`：取消在途查询 → ROLLBACK → hypopg_reset → 协议状态与计数双重验证；清理不可确认的连接直接关闭丢弃，绝不回队 |
| 迟到清理不取消下一请求、不删其候选 | 清理全程 `asyncio.shield` 屏蔽取消；下一请求只从队列取已验证干净的连接 |
| 失败不返回部分比较 | 任何异常都不产出半成品响应；非法输入 400，内部错误 500 |
| 可实际启动的 Docker Compose，/docs 可提交评估 | `docker compose up` 后访问 <http://localhost:8000/docs> |
| 能改善 / 不能改善计划的索引样例 | `GET /samples`，内置两组样例及配套候选 |
| 可演示创建候选后暂停、断连再复用连接 | 请求头 `X-Demo-Pause-Seconds`（可选 `X-Demo-Pause-Mode`） |

## 列更名预览（`POST /rename-preview`）

### 能力与约束（与需求逐条对应）

| 需求 | 实现 |
| --- | --- |
| 提交旧列名、新列名及最多 5 条只读 SELECT | Pydantic 校验 `queries` 1~5 条、标识符合法、单条长度上限 |
| 查询只涉及 `public.package_scan` 且不含 JOIN | 静态结构分析：FROM 仅允许该表（可带 schema/别名）；JOIN、逗号多表、非目标表、FROM 函数/子查询逐条转人工 |
| 支持表别名、一层 CTE、表达式及筛选/分组/排序 | 纯标准库 tokenizer + 块结构分析，覆盖 WHERE/GROUP BY/HAVING/WINDOW/ORDER BY/LIMIT、DISTINCT ON、CASE、窗口函数等 |
| 只改写确实引用原列的位置 | 字符串/注释/dollar 引用/关键字/函数名一律不动；限定名校验限定符必须解析到表或别名 |
| 未显式起别名的输出列仍用旧名称 | 裸列输出改写为 `new AS old`（带限定则 `alias.new AS old`），显式/隐式别名原样保留 |
| CTE 输出别名及外层引用不误改 | CTE 块与外层块分别分析：仅 CTE 内部表引用可改写，外层对 CTE 输出名的引用保持原样 |
| 星号、歧义、不支持结构逐条人工处理，不给改写 SQL | `status=manual` + 中文 `reason`，`rewritten_sql=null`；支持星号、JOIN、子查询、集合运算、多/递归/嵌套 CTE、行锁定、多语句、未闭合括号、无法解析的限定名 |
| 旧列不存在 / 新列名已存在时拒绝 | 进入逐条处理前查 `information_schema.columns` 整体拒绝（HTTP 400）；新旧同名同样拒绝 |
| 只读列目录，不执行提交查询、不改表 | 服务端只发目录查询；用户 SQL 仅做词法/结构分析，连接本身 `default_transaction_read_only=on` |
| 保留 `/evaluate` 与两条物理连接上限 | 新接口复用同一 `TwoConnectionPool`（信号量硬上限 2，单 worker），`/evaluate` 行为不变 |
| 可启动的 Docker Compose，/docs 可预览 | `docker compose up` 后在 <http://localhost:8000/docs> 找到 `rename-preview` |

### 改写示例

请求：

```json
{
  "old_column": "scan_time",
  "new_column": "scanned_at",
  "queries": [
    "SELECT scan_id, scan_time FROM public.package_scan WHERE scan_time >= now() ORDER BY scan_time",
    "WITH night AS (SELECT scan_id, scan_time FROM package_scan WHERE scan_time >= now()) SELECT scan_id, scan_time FROM night ORDER BY scan_time",
    "SELECT * FROM package_scan"
  ]
}
```

响应（节选）：

```json
{
  "target_table": "public.package_scan",
  "old_column": "scan_time",
  "new_column": "scanned_at",
  "results": [
    {
      "index": 0,
      "status": "rewritten",
      "rewritten_sql": "SELECT scan_id, scanned_at AS scan_time FROM public.package_scan WHERE scanned_at >= now() ORDER BY scan_time",
      "output_columns_preserved": true,
      "renamed_reference_count": 2
    },
    {
      "index": 1,
      "status": "rewritten",
      "rewritten_sql": "WITH night AS (SELECT scan_id, scanned_at AS scan_time FROM package_scan WHERE scanned_at >= now()) SELECT scan_id, scan_time FROM night ORDER BY scan_time",
      "output_columns_preserved": true,
      "renamed_reference_count": 2
    },
    {
      "index": 2,
      "status": "manual",
      "rewritten_sql": null,
      "reason": "投影列表包含星号（* 或 表.*），输出列无法静态确认，需人工处理"
    }
  ],
  "summary": {"total": 3, "rewritten": 2, "manual": 1, "changed": 2}
}
```

要点：

- 第 1 条裸列输出变成 `scanned_at AS scan_time`，**输出列名仍是 `scan_time`**；
  ORDER BY 中的 `scan_time` 命中输出名，按 PostgreSQL 语义属于输出列引用，保持不变；
- 第 2 条只改 CTE 内部；外层 `night.scan_time` 是 CTE 输出别名引用，原样保留；
- 第 3 条星号无法静态确认输出列，标记人工且不给任何改写 SQL。

### 名称解析与“歧义”判定（PostgreSQL 语义）

- 单表作用域内裸标识符只可能指向该表列；带限定名（`a.col` / `schema.tbl.col`）时
  校验限定符是否为已声明别名或表名，无法解析即判歧义转人工；
- `ORDER BY` / `GROUP BY` 中**独立裸标识符**若命中投影输出名（含隐式列名），
  解析为输出列引用而不是表列，保持原样；处于表达式内部时看不到输出别名，按表列改写；
- 裸词按 PG 规则折叠小写匹配，引号标识符按实际名字大小写不敏感匹配，
  输出别名保留用户书写形式（包括引号）。

## 目录结构

```
.
├── docker-compose.yml          # db(Postgres+HypoPG) + api(FastAPI)，api 单进程
├── db/
│   ├── Dockerfile              # postgres:15-bookworm + postgresql-15-hypopg
│   └── initdb/
│       ├── 01_schema.sql       # package_scan 事实表（仅主键真实索引）
│       └── 02_seed.sql         # 确定性生成 50 万行扫描明细 + ANALYZE
├── api/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py             # 路由 / 生命周期 / 异常映射（/evaluate 与 /rename-preview）
│       ├── config.py           # 环境变量配置（连接上限、超时、演示开关）
│       ├── db.py               # 两连接租约池 + 取消安全清理
│       ├── service.py          # HypoPG 评估主流程（EXPLAIN、成本、采纳判定）
│       ├── rename_service.py   # 列更名预览服务（列目录校验 + 静态改写编排）
│       ├── rewrite.py          # 纯标准库 SQL 词法/结构分析与文本级改写（零第三方依赖）
│       ├── validation.py       # 只读 SELECT 解析守卫
│       ├── schemas.py          # 请求/响应模型
│       └── samples.py          # 预置样例 SQL 与候选
├── tests/test_api.py           # 索引评估端到端断言
├── tests/test_rewrite.py       # 改写核心离线单测（纯标准库，无需数据库）
├── tests/test_rename_api.py    # 更名预览 API 端到端断言
└── scripts/demo.sh             # 命令行演示（正常评估 / 断连复用）
```

## 启动

```bash
docker compose up --build
# 首次启动会初始化数据库并灌入 50 万行数据，约需十几秒
# - Swagger 文档： http://localhost:8000/docs
# - 健康检查：     http://localhost:8000/health
# - 预置样例：     http://localhost:8000/samples
```

## 快速体验

### 1) 能改善计划的候选（warehouse_id 等值 + scan_time 排序）

```bash
curl -s -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "sample_id": "nightly_warehouse_scan",
    "candidates": [
      {"table": "public.package_scan", "columns": ["warehouse_id", "scan_time"]}
    ]
  }'
```

基线 `Seq Scan + Sort`（成本约 15920）→ 假想索引下 `Index Scan Backward`（成本约 772），
候选进入 `adopted_candidates`。

### 2) 不能改善计划的候选（谓词无关列 / 全表聚合）

```bash
curl -s -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "sample_id": "nightly_warehouse_scan",
    "candidates": [
      {"table": "public.package_scan", "columns": ["destination"]},
      {"table": "public.package_scan", "columns": ["weight_kg"]}
    ]
  }'
```

成本与计划均不变，两个候选进入 `rejected_candidates`。
`nightly_aggregate` 样例（全表 GROUP BY）下任何 B-tree 都不会被采用。

### 3) 暂停 + 断连 + 连接复用演示

```bash
# 终端 A：发起一个创建候选后暂停 30 秒的请求
curl -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' -H 'X-Demo-Pause-Seconds: 30' \
  -d '{"sample_id":"nightly_warehouse_scan",
       "candidates":[{"table":"public.package_scan","columns":["warehouse_id","scan_time"]}]}'
# 在另一个终端 Ctrl+C / 杀掉 curl 模拟断连；随后再发普通请求，
# /health 中 cancels_performed 增加，total_discarded 保持 0，新请求看不到任何残留候选。
```

- 默认 `X-Demo-Pause-Mode: asyncio`：暂停不占用数据库工作，断连检测即时，
  服务端走完整取消清理后把连接回队复用。
- 设为 `pg_sleep` 时，暂停是一条真实在途 `SELECT pg_sleep(...)`，断连会先
  `conn.cancel()` 停掉服务端查询再清理（需要操作系统允许进程信号，标准容器内核支持）。

也可直接运行 `bash scripts/demo.sh`。

### 4) 列更名预览（不改表、不执行查询）

```bash
curl -s -X POST http://localhost:8000/rename-preview \
  -H 'Content-Type: application/json' \
  -d '{
    "old_column": "scan_time",
    "new_column": "scanned_at",
    "queries": [
      "SELECT scan_id, scan_time FROM public.package_scan WHERE scan_time >= now() ORDER BY scan_time",
      "SELECT * FROM public.package_scan"
    ]
  }'
```

第一条返回改写 SQL（裸列输出以 `scanned_at AS scan_time` 保留旧输出名），
第二条星号查询返回 `status=manual` 与中文原因、不带任何改写 SQL。
离线可先跑 `python3 -I tests/test_rewrite.py` 验证全部改写规则。

## 安全设计

1. **SQL 只读双保险**：`sqlparse` 词法层拒绝多语句与一切写/DDL 关键字；
   每条连接 `SET default_transaction_read_only = on`，数据库自身再挡一层。
2. **标识符注入防护**：表名/列名经 Pydantic 正则与 psycopg `Identifier` 双重处理。
3. **慢查询兜底**：会话级 `statement_timeout`、`lock_timeout`、
   `idle_in_transaction_session_timeout`。
4. **脏连接绝不复用**：清理后校验协议事务状态为 IDLE 且假想索引计数为 0，
   不达标即关闭连接并新建，信号量保证任意时刻物理连接数 ≤ 2。

## 本地（无 Docker）运行测试

仓库的测试可指向任意已装 HypoPG 的 Postgres：

```bash
pip install -r api/requirements.txt
DB_DSN=postgresql://USER@HOST:PORT/logistics \
DEMO_PAUSE_ENABLED=true \
uvicorn app.main:app --app-dir api --port 8000
python -I tests/test_api.py http://127.0.0.1:8000          # 索引评估端到端
python -I tests/test_rename_api.py http://127.0.0.1:8000   # 更名预览端到端
```

更名改写核心是纯标准库实现，不依赖数据库或第三方包，可直接离线运行：

```bash
python3 -I tests/test_rewrite.py
```
