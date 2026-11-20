"""质控 SELECT -> 可执行的 PostgreSQL 增量 SQL。

场景：站点读数补传入仓时，下游需要补发或撤回部分质控结果。执行生成的 SQL 时：

- ``stations``：站点主表（不变）；
- ``readings``：入仓前的存量读数；
- ``new_readings``：与 readings 同结构、已通过外键校验的待追加批次。

生成的 SQL 只重算该批涉及的站点（new_readings 中出现的 station_id），
输出净新增（需要补发）与净撤回（需要撤回）的结果行及次数，结果与
"全量重跑 readings ∪ new_readings 再与全量旧结果求差" 一致。

正确性要点：

- 左连接的补空行：站点原本无读数时输出一行 readings 全 NULL 的补空行；
  该站首条读数到批后，即便这条读数本身被 WHERE 排除，补空行也会消失，
  因此它出现在净撤回中。EXCEPT ALL 的差集语义天然覆盖这一点。
- 多重集语义：按列位置比较、两个 NULL 视为相同、重复行逐次计数——
  PostgreSQL 的 EXCEPT ALL 正是如此（NULL 视为相等、按位置、保留次数差）。
- 只重算受影响站点：不在批次的站点新旧结果完全相同，差集中相互抵消，
  因此用 ``s.id IN (SELECT station_id FROM affected)`` 收敛两侧即可，
  不改变差额；station_id 为 NULL 的批次行不参与连接，直接忽略。
"""
from __future__ import annotations

from dataclasses import dataclass

from .ir import And, BoolNode, ColRef, Compare, IsNull, Not, Or, QueryIR
from .sql_parser import SqlRejected

# 生成 SQL 使用的固定别名（与用户原文的别名无关）
_ALIAS = {"stations": "s", "readings": "r"}

# 唯一接受的连接形态：stations LEFT JOIN readings ON stations.id = readings.station_id
_REQUIRED_LEFT_COL = ColRef("stations", "id")
_REQUIRED_RIGHT_COL = ColRef("readings", "station_id")


@dataclass(frozen=True)
class IncrementalSQL:
    """生成结果。两段 SQL 均为单条 SELECT，可直接在目标库执行。"""

    delta_sql: str  # 明细：(delta='added'|'removed', c1..cN)，净新增/净撤回行
    count_sql: str  # 汇总：(delta, n)，两类各一行，无行时次数为 0
    projection: tuple[str, ...]  # 明细列 c1..cN 对应的原始投影（table.column）


def build_incremental_sql(query: QueryIR) -> IncrementalSQL:
    """把已解析的质控查询 IR 转成增量 SQL；形态不符抛 SqlRejected。"""
    _require_supported_shape(query)
    proj_sql = [f"{_ALIAS[c.table]}.{c.column}" for c in query.projections]
    out_cols = [f"c{i}" for i in range(1, len(proj_sql) + 1)]
    select_list = ", ".join(f"{p} AS {a}" for p, a in zip(proj_sql, out_cols))
    cols = ", ".join(out_cols)

    scope_filter = "s.id IN (SELECT station_id FROM affected)"
    if query.where is not None:
        scope_filter += f" AND ({_render_where(query.where)})"

    delta_sql = f"""WITH affected AS (
    SELECT DISTINCT nr.station_id
    FROM new_readings AS nr
    WHERE nr.station_id IS NOT NULL
),
old_rows AS (
    SELECT {select_list}
    FROM stations AS s
    LEFT JOIN readings AS r ON s.id = r.station_id
    WHERE {scope_filter}
),
new_rows AS (
    SELECT {select_list}
    FROM stations AS s
    LEFT JOIN (
        SELECT station_id, value FROM readings
        UNION ALL
        SELECT station_id, value FROM new_readings
    ) AS r ON s.id = r.station_id
    WHERE {scope_filter}
),
added AS (
    SELECT {cols} FROM new_rows
    EXCEPT ALL
    SELECT {cols} FROM old_rows
),
removed AS (
    SELECT {cols} FROM old_rows
    EXCEPT ALL
    SELECT {cols} FROM new_rows
)
SELECT 'added' AS delta, {cols} FROM added
UNION ALL
SELECT 'removed' AS delta, {cols} FROM removed"""

    count_sql = (
        "SELECT k.delta, COALESCE(d.n, 0) AS n\n"
        "FROM (VALUES ('added'), ('removed')) AS k(delta)\n"
        "LEFT JOIN (\n"
        "    SELECT q.delta, COUNT(*) AS n\n"
        f"    FROM (\n{_indent(delta_sql)}\n    ) AS q\n"
        "    GROUP BY q.delta\n"
        ") AS d ON d.delta = k.delta\n"
        "ORDER BY k.delta"
    )
    return IncrementalSQL(
        delta_sql=delta_sql,
        count_sql=count_sql,
        projection=tuple(f"{c.table}.{c.column}" for c in query.projections),
    )


# ---------- 内部实现 ----------

def _require_supported_shape(query: QueryIR) -> None:
    if len(query.tables) == 1:
        raise SqlRejected(
            "增量 SQL 要求 stations LEFT JOIN readings，缺少连接"
        )
    if query.tables[0] != "stations" or query.tables[1] != "readings":
        raise SqlRejected(
            "增量 SQL 要求以 stations 为驱动表左连接 readings"
        )
    join = query.join
    assert join is not None  # 两表必有连接（解析器保证）
    if join.kind != "left":
        raise SqlRejected("增量 SQL 仅支持 LEFT [OUTER] JOIN，不支持 INNER JOIN")
    if join.left_col != _REQUIRED_LEFT_COL or join.right_col != _REQUIRED_RIGHT_COL:
        raise SqlRejected(
            "连接条件必须是 stations.id = readings.station_id"
        )


def _render_where(node: BoolNode) -> str:
    """把 WHERE IR 渲染回 SQL（全括号化，固定别名 s/r）。"""
    if isinstance(node, Compare):
        return f"{_col(node.col)} {node.op} {node.value}"
    if isinstance(node, IsNull):
        return f"{_col(node.col)} IS {'NOT ' if node.negated else ''}NULL"
    if isinstance(node, And):
        return f"({_render_where(node.left)} AND {_render_where(node.right)})"
    if isinstance(node, Or):
        return f"({_render_where(node.left)} OR {_render_where(node.right)})"
    if isinstance(node, Not):
        return f"(NOT {_render_where(node.child)})"
    raise TypeError(f"未知布尔节点: {node!r}")  # pragma: no cover - 防御


def _col(c: ColRef) -> str:
    return f"{_ALIAS[c.table]}.{c.column}"


def _indent(sql: str) -> str:
    return "\n".join("    " + line for line in sql.splitlines())
