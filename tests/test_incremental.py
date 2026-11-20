"""增量 SQL 生成的单元测试：形态校验、WHERE 渲染、SQL 结构（不依赖数据库）。"""
import pytest
from fastapi.testclient import TestClient

from app.incremental import build_incremental_sql
from app.main import app
from app.sql_parser import SqlRejected, parse_query

client = TestClient(app)

GOOD_QUERY = (
    "SELECT s.id, r.value FROM stations s "
    "LEFT JOIN readings r ON s.id = r.station_id "
    "WHERE r.value >= 10 OR r.value IS NULL"
)


def gen(sql: str):
    return build_incremental_sql(parse_query(sql))


# ---------- 形态校验 ----------

@pytest.mark.parametrize(
    "sql",
    [
        # 基本形态（别名任意、LEFT OUTER、ON 两侧顺序任意、列可不限定）
        "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id",
        "SELECT id, value FROM stations LEFT JOIN readings ON id = station_id",
        "SELECT st.id FROM stations st LEFT OUTER JOIN readings rd "
        "ON rd.station_id = st.id",
        # 无 WHERE
        "SELECT s.id, s.threshold, r.station_id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id",
        # 复杂 WHERE
        "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE NOT (r.value >= 10) AND (s.threshold != 0 OR s.threshold IS NULL)",
    ],
)
def test_accepted_shapes(sql):
    out = gen(sql)
    assert "new_readings" in out.delta_sql
    assert "EXCEPT ALL" in out.delta_sql
    assert out.count_sql


@pytest.mark.parametrize(
    "sql",
    [
        # 单表
        "SELECT id FROM stations WHERE threshold > 0",
        "SELECT value FROM readings",
        # INNER JOIN
        "SELECT s.id FROM stations s INNER JOIN readings r ON s.id = r.station_id",
        "SELECT s.id FROM stations s JOIN readings r ON s.id = r.station_id",
        # 驱动表不是 stations
        "SELECT r.station_id FROM readings r LEFT JOIN stations s "
        "ON r.station_id = s.id",
        # 连接列不对
        "SELECT s.id FROM stations s LEFT JOIN readings r "
        "ON s.threshold = r.station_id",
        "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.value",
    ],
)
def test_rejected_shapes(sql):
    with pytest.raises(SqlRejected):
        gen(sql)


def test_api_rejects_inner_join_with_400():
    resp = client.post(
        "/incremental",
        json={
            "query": "SELECT s.id FROM stations s "
                     "INNER JOIN readings r ON s.id = r.station_id"
        },
    )
    assert resp.status_code == 400
    assert "增量转换" in resp.json()["detail"]


def test_api_rejects_out_of_subset_sql_with_400():
    resp = client.post(
        "/incremental",
        json={"query": "SELECT s.id, COUNT(*) FROM stations s "
                       "LEFT JOIN readings r ON s.id = r.station_id GROUP BY s.id"},
    )
    assert resp.status_code == 400


# ---------- SQL 结构 ----------

def test_delta_sql_structure():
    out = gen(GOOD_QUERY)
    sql = out.delta_sql
    # 只重算批次涉及的站点
    assert "FROM new_readings" in sql
    assert "s.id IN (SELECT station_id FROM affected)" in sql
    # 存量与批次合并后重算
    assert "UNION ALL" in sql
    # 多重集差集
    assert sql.count("EXCEPT ALL") == 2
    # 净新增 / 净撤回两类标记
    assert "'added' AS delta" in sql
    assert "'removed' AS delta" in sql
    # 投影按位置编号，列名不依赖用户别名
    assert "c1" in sql and "c2" in sql
    assert out.projection == ("stations.id", "readings.value")
    # 单条语句（无分号、可嵌入）
    assert ";" not in sql


def test_count_sql_wraps_delta_and_always_returns_two_kinds():
    out = gen(GOOD_QUERY)
    assert "VALUES ('added'), ('removed')" in out.count_sql
    assert "COALESCE" in out.count_sql
    assert "GROUP BY q.delta" in out.count_sql


def test_where_rendered_with_canonical_aliases_and_parens():
    out = gen(
        "SELECT st.id FROM stations st LEFT JOIN readings rd "
        "ON rd.station_id = st.id "
        "WHERE 10 > rd.value AND NOT (st.threshold IS NULL)"
    )
    # 字面量在左被翻转、别名归一化、全括号
    assert "r.value < 10" in out.delta_sql
    assert "(NOT s.threshold IS NULL)" in out.delta_sql


def test_no_where_query_has_no_dangling_and():
    out = gen(
        "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id"
    )
    assert " AND (" not in out.delta_sql


def test_duplicate_projection_columns_get_positional_aliases():
    out = gen(
        "SELECT s.id, s.id FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id"
    )
    assert "s.id AS c1, s.id AS c2" in out.delta_sql


# ---------- API 行为 ----------

def test_api_incremental_ok():
    resp = client.post("/incremental", json={"query": GOOD_QUERY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["required_tables"] == ["stations", "readings", "new_readings"]
    assert body["projection"] == ["stations.id", "readings.value"]
    assert "WITH affected AS" in body["delta_sql"]
    assert body["count_sql"].startswith("SELECT k.delta")


def test_api_incremental_empty_query_400():
    resp = client.post("/incremental", json={"query": ""})
    assert resp.status_code == 400


def test_openapi_lists_incremental_with_examples():
    spec = client.get("/openapi.json").json()
    assert "/incremental" in spec["paths"]
    post = spec["paths"]["/incremental"]["post"]
    examples = post["requestBody"]["content"]["application/json"]["examples"]
    assert "backfill-null-placeholder-retract" in examples
