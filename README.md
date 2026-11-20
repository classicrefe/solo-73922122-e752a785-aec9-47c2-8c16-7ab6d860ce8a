# SQL 改写有界等价核对服务

面向数仓质控场景：维护员替换站点读数入仓质控 SQL 前，提交**原始 SELECT** 与
**改写 SELECT**，服务在固定模式下枚举每表 **0~3 行**的所有合法取值，核对两侧
结果**多重集**是否一致（按列位置比较、忽略行序、保留重复），用于排查改写对
缺测（NULL）与重复采样的影响。

- 差异必须在**隔离 PostgreSQL** 中实际复现才报 `NOT_EQUIVALENT`，并返回
  可装载 SQL 与两侧实际结果；
- 界内无反例报 `EQUIVALENT`（仅三行界内结论，不构成更大表上的证明）；
- 求解或复现失败报 `UNKNOWN`。

## 固定模式

```sql
CREATE TABLE stations (id INTEGER PRIMARY KEY, threshold INTEGER);
CREATE TABLE readings (station_id INTEGER REFERENCES stations(id), value INTEGER);
```

全列 `integer`；非主键列可 `NULL`；`readings.station_id` 为外键（NULL 合法）。

## 支持的 SQL 子集（其余一律 400 拒绝）

| 子句 | 支持内容 |
| --- | --- |
| SELECT | 仅列投影（可带表限定、别名；不支持 `*`、表达式、函数、字面量） |
| FROM | 单表，或两表间至多一次 `INNER` / `LEFT [OUTER]` 等值连接（`ON a.col = b.col`） |
| WHERE | 列与整数字面量比较（`=` `!=` `<` `<=` `>` `>=`）、`IS [NOT] NULL`、`AND` / `OR` / `NOT`、括号 |

不支持：聚合 / `GROUP BY` / `HAVING` / `ORDER BY` / `LIMIT` / `DISTINCT` /
子查询 / `IN` / `BETWEEN` / 字符串与浮点字面量 / 列间比较（WHERE 内）/
自连接 / `RIGHT` / `FULL` / `CROSS` 连接 / 写操作等。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# API: http://localhost:8000  交互文档: http://localhost:8000/docs
```

`docker-compose.yml` 启动两个服务：`db`（postgres:16，仅内部网络，专用于
反例复现）与 `api`（FastAPI）。无需初始化数据——复现用的表结构由服务在
临时 schema 中自建，事务回滚即清理。

## API 用法

### `POST /check`

```json
{
  "query_a": "SELECT id FROM stations WHERE threshold = 5",
  "query_b": "SELECT id FROM stations WHERE threshold = 5 OR threshold IS NULL",
  "solver_timeout_ms": 10000
}
```

响应（`NOT_EQUIVALENT` 示例）：

```json
{
  "verdict": "NOT_EQUIVALENT",
  "bound_rows": 3,
  "message": "两侧结果存在差异，已在隔离 PostgreSQL 中实际复现。",
  "counterexample": {
    "setup_sql": "DROP TABLE IF EXISTS readings;\nDROP TABLE IF EXISTS stations;\nCREATE TABLE stations ...\nINSERT INTO stations ...",
    "query_a_result": [],
    "query_b_result": [[-1]]
  }
}
```

- `verdict=EQUIVALENT`：每表 0~3 行所有合法取值下结果多重集相同；
- `verdict=NOT_EQUIVALENT`：`counterexample.setup_sql` 可直接装载到任意
   scratch 库复现差异，`query_a_result` / `query_b_result` 为实际执行结果；
- `verdict=UNKNOWN`：z3 超时/无法判定，或 PostgreSQL 复现失败（`message`
  说明原因）。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/check \
  -H 'Content-Type: application/json' \
  -d '{"query_a":"SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id",
       "query_b":"SELECT id FROM stations"}'
```

### `POST /incremental`

读数补传入仓场景：把一条质控 SELECT 转成可执行的 PostgreSQL **增量 SQL**，
下游据此补发 / 撤回质控结果，无需全量重跑。

```json
{
  "query": "SELECT s.id, r.value FROM stations s LEFT JOIN readings r ON s.id = r.station_id WHERE r.value >= 10 OR r.value IS NULL"
}
```

响应：

```json
{
  "delta_sql": "WITH affected AS (...), old_rows AS (...), new_rows AS (...), added AS (...), removed AS (...) SELECT 'added' AS delta, c1, c2 FROM added UNION ALL SELECT 'removed', c1, c2 FROM removed",
  "count_sql": "SELECT k.delta, COALESCE(d.n, 0) AS n FROM (VALUES ('added'), ('removed')) ...",
  "projection": ["stations.id", "readings.value"],
  "required_tables": ["stations", "readings", "new_readings"],
  "message": "增量 SQL 已生成：..."
}
```

- **接受的查询形态**：仅 `stations LEFT [OUTER] JOIN readings ON
  stations.id = readings.station_id`（ON 两侧顺序、表别名任意），列投影与
  WHERE 沿用上述受支持子集；其余形态（INNER JOIN、单表、readings 驱动、
  其他连接列）返回 400。
- **执行前提**：目标库存在 `stations`、`readings`（入仓前存量）与
  `new_readings`（与 readings 同结构、已通过外键校验的待追加批次）。
- **语义**：`delta_sql` 返回明细行 `(delta, c1..cN)`——`added` 为净新增
  （需补发）、`removed` 为净撤回（需撤回），`c1..cN` 按 `projection`
  顺序对应原投影列；`count_sql` 返回 `(delta, n)` 两行给出两类次数
  （无行时次数为 0）。只重算 `new_readings` 涉及的站点（`station_id`
  为 NULL 的批次行不参与连接，自动忽略），结果与"全量重跑
  readings ∪ new_readings 再求差"一致。
- **多重集语义**：按列位置比较、两个 NULL 视为相同、重复行逐次计数
  （由 PostgreSQL `EXCEPT ALL` 保证）。
- **补空行陷阱**：站点原本无读数时左连接输出一行 readings 全 NULL 的
  补空行；该站首条读数到批后，即便这条读数本身被 WHERE 排除，补空行
  也会消失——它会正确出现在净撤回中。

执行示例（psql）：

```sql
-- 假设业务库已有 stations / readings，先装载待追加批次
CREATE TABLE new_readings (station_id INTEGER REFERENCES stations(id), value INTEGER);
\copy new_readings FROM 'batch.csv' CSV
-- 粘贴执行 delta_sql / count_sql，把结果推给下游补发或撤回
-- 核对无误后合入：
INSERT INTO readings SELECT * FROM new_readings;
```

curl 示例：

```bash
curl -s -X POST http://localhost:8000/incremental \
  -H 'Content-Type: application/json' \
  -d '{"query":"SELECT s.id, r.value FROM stations s LEFT JOIN readings r ON s.id = r.station_id WHERE r.value >= 10 OR r.value IS NULL"}'
```

### 其他端点

- `GET /`：服务信息；
- `GET /health`：健康检查（含数据库连通性）；
- `GET /docs`：Swagger 交互文档，内置等价 / 不等价（NULL 语义、LEFT JOIN
  重复行）及增量 SQL（阈值过滤、补空行撤回、混合条件）等命名示例，
  可直接 "Try it out"。

## 判定原理

1. **解析与白名单校验**（`app/sql_parser.py`，基于 sqlglot）：越界语法拒绝。
2. **z3 有界判定**（`app/z3check.py`）：两表各 0~3 行符号化，施加主键唯一、
   外键引用、int4 值域约束；WHERE 按 SQL 三值逻辑编码（`UNKNOWN` 被过滤）；
   JOIN/LEFT JOIN 展开为符号行；结果多重集差异编码为"存在某元组值两侧计数
   不同"。`UNSAT` 即界内等价，`SAT` 提取候选反例。
3. **PostgreSQL 复现**（`app/verifier.py`）：候选反例在独立临时 schema 中
   建表、装载、实际执行两条查询并比较多重集；确认差异才报
   `NOT_EQUIVALENT`，否则报 `UNKNOWN`。

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/postgres` | 复现用 PostgreSQL DSN |
| `SOLVER_TIMEOUT_MS` | `10000` | z3 求解超时（可被请求字段 `solver_timeout_ms` 覆盖） |

## 本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/                       # 单元测试（解析器 / z3 / 增量 SQL 生成 / API 打桩）
DATABASE_URL=postgresql://... pytest tests/test_e2e.py tests/test_incremental_e2e.py   # 端到端（需真实 PG）
```

`tests/test_incremental_e2e.py` 会在真实 PostgreSQL 中执行生成的增量 SQL，
并与"全量重跑 readings ∪ new_readings 再求差"的结果逐例比对（含补空行
撤回、重复行计数、NULL 值 / NULL 外键、空批次等场景）。

## 已知边界

- 等价结论仅限每表 ≤3 行（有界模型检验，非全量证明）；
- 仅支持上述 SQL 子集；两查询投影列数不一致返回 400；
- 复现使用单事务 + 临时 schema，不在库中留存对象；数据库不可达时
  不等价判定降级为 `UNKNOWN`（等价判定不依赖数据库）；
- `/incremental` 仅接受 stations 左连接 readings 的固定连接形态；
  生成 SQL 的正确性依赖执行前提：`new_readings` 与 `readings` 同结构、
  批次已通过外键校验、执行期间三表无并发写（建议在单事务内执行
  delta_sql 后再合入批次）。
