# 夜间包裹扫描查询 · 候选索引成本评估 API

物流数仓工程师在优化夜间包裹扫描查询前，用这套服务判断 **B-tree 候选索引能否降低查询成本**。
基于 **FastAPI + psycopg(v3) + PostgreSQL 15 + HypoPG**，只做 **EXPLAIN 估算**：
不执行查询（`EXPLAIN` 不带 `ANALYZE`），也不创建真实索引（HypoPG 假想索引仅存于后端内存）。

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
│       ├── main.py             # 路由 / 生命周期 / 异常映射
│       ├── config.py           # 环境变量配置（连接上限、超时、演示开关）
│       ├── db.py               # 两连接租约池 + 取消安全清理
│       ├── service.py          # HypoPG 评估主流程（EXPLAIN、成本、采纳判定）
│       ├── validation.py       # 只读 SELECT 解析守卫
│       ├── schemas.py          # 请求/响应模型
│       └── samples.py          # 预置样例 SQL 与候选
├── tests/test_api.py           # 47 项端到端断言
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
python -I tests/test_api.py http://127.0.0.1:8000
```
