"""增量 SQL 的端到端自测：在真实 PostgreSQL 中执行生成的 SQL，
并与"全量重跑 readings ∪ new_readings 再求差"的结果比对。

需要 DATABASE_URL（同 tests/test_e2e.py）。
"""
import os
import random
import uuid

import pytest
import psycopg
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL 未设置，跳过端到端测试"
)

client = TestClient(app)
DSN = os.environ.get("DATABASE_URL", "")

_CREATE = """
CREATE TABLE stations (id INTEGER PRIMARY KEY, threshold INTEGER);
CREATE TABLE readings (station_id INTEGER REFERENCES stations(id), value INTEGER);
CREATE TABLE new_readings (station_id INTEGER REFERENCES stations(id), value INTEGER);
"""


def _canon_row(row):
    return tuple((0, 0) if v is None else (1, v) for v in row)


def _canon(rows):
    """多重集规范化：NULL 可比较、忽略行序、保留重复。"""
    return sorted(_canon_row(row) for row in rows)


def _multiset_minus(a, b):
    """a - b（逐次计数，NULL 视为相同）。"""
    rest = [_canon_row(row) for row in b]
    out = []
    for row in a:
        key = _canon_row(row)
        for i, other in enumerate(rest):
            if other == key:
                rest.pop(i)
                break
        else:
            out.append(key)
    return sorted(out)


def _run_delta_case(conn, query, stations, readings, new_readings):
    """执行生成的 SQL，并返回 (added, removed, counts) 与全量重跑差额。"""
    cur = conn.cursor()
    cur.executemany("INSERT INTO stations (id, threshold) VALUES (%s, %s)", stations)
    cur.executemany(
        "INSERT INTO readings (station_id, value) VALUES (%s, %s)", readings
    )
    cur.executemany(
        "INSERT INTO new_readings (station_id, value) VALUES (%s, %s)", new_readings
    )

    body = client.post("/incremental", json={"query": query}).json()
    cur.execute(body["delta_sql"])
    delta_rows = [tuple(r) for r in cur.fetchall()]
    added = sorted(_canon([r[1:] for r in delta_rows if r[0] == "added"]))
    removed = sorted(_canon([r[1:] for r in delta_rows if r[0] == "removed"]))
    assert all(r[0] in ("added", "removed") for r in delta_rows)

    cur.execute(body["count_sql"])
    counts = {r[0]: r[1] for r in cur.fetchall()}

    # 全量重跑：旧结果 vs readings ∪ new_readings 的新结果
    cur.execute(query)
    old_full = [tuple(r) for r in cur.fetchall()]
    cur.execute("INSERT INTO readings SELECT * FROM new_readings")
    cur.execute(query)
    new_full = [tuple(r) for r in cur.fetchall()]

    exp_added = _multiset_minus(new_full, old_full)
    exp_removed = _multiset_minus(old_full, new_full)
    return (added, removed, counts), (exp_added, exp_removed)


@pytest.fixture()
def schema():
    name = "inc_" + uuid.uuid4().hex
    with psycopg.connect(DSN) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
        conn.execute(f'SET search_path TO "{name}"')
        for stmt in _CREATE.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        yield conn
        conn.rollback()  # 不提交，schema 随事务回滚消失


def test_first_reading_retracts_null_placeholder_even_when_filtered(schema):
    """首条读数被 WHERE 排除，但原本的补空行必须净撤回。"""
    query = (
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE r.value >= 10 OR r.value IS NULL"
    )
    got, exp = _run_delta_case(
        schema, query,
        stations=[(1, 10), (2, 20)],
        readings=[(2, 50)],
        new_readings=[(1, 5)],  # 5 < 10 被 WHERE 排除，但补空行 (1, NULL) 消失
    )
    assert got[0] == exp[0] == []            # 无净新增
    assert got[1] == exp[1] == [((1, 1), (0, 0))]  # 净撤回 (1, NULL)
    assert got[2] == {"added": 0, "removed": 1}


def test_delta_matches_full_rerun_mixed(schema):
    """存量+批次混合：新增、撤回、不受影响站点、NULL 值、NULL 外键。"""
    query = (
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE r.value >= 10 OR r.value IS NULL"
    )
    got, exp = _run_delta_case(
        schema, query,
        stations=[(1, 10), (2, 20), (3, None), (4, 40)],
        readings=[(1, 3), (2, 15), (None, 99)],
        new_readings=[(1, 12), (1, 7), (3, 4), (None, 5)],
    )
    assert got[0] == exp[0]
    assert got[1] == exp[1]
    assert got[2] == {"added": len(exp[0]), "removed": len(exp[1])}
    assert got[0]  # 站点 1 的 (1, 12) 等应净新增
    assert got[1]  # 站点 3 首条读数 (3,4) 被 WHERE 排除，补空行 (3,NULL) 净撤回


def test_duplicate_rows_counted_individually(schema):
    """重复行逐次计数：同一值出现次数的净变化必须精确。"""
    query = (
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id WHERE r.value >= 10"
    )
    got, exp = _run_delta_case(
        schema, query,
        stations=[(1, 0)],
        readings=[(1, 10), (1, 10)],
        new_readings=[(1, 10), (1, 20), (1, 20), (1, 20)],
    )
    assert got[0] == exp[0]
    # (1,10): 2 -> 3 净增 1 行；(1,20): 0 -> 3 净增 3 行
    assert got[0] == [((1, 1), (1, 10)), ((1, 1), (1, 20)),
                      ((1, 1), (1, 20)), ((1, 1), (1, 20))]
    assert got[2] == {"added": 4, "removed": 0}


def test_empty_batch_yields_zero_counts(schema):
    query = (
        "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id"
    )
    got, exp = _run_delta_case(
        schema, query,
        stations=[(1, 5)],
        readings=[(1, 1)],
        new_readings=[],
    )
    assert got[0] == exp[0] == []
    assert got[1] == exp[1] == []
    assert got[2] == {"added": 0, "removed": 0}  # 两类恒有行


def test_station_only_projection_and_no_where(schema):
    """只投影 s.id 时补空行与读数行同值：差额退化为纯次数变化。"""
    query = (
        "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id"
    )
    got, exp = _run_delta_case(
        schema, query,
        stations=[(1, 1), (2, 2)],
        readings=[],
        new_readings=[(1, 9), (1, 9)],
    )
    # old={(1),(2)}，new={(1),(1),(2)}：净增 1 行 (1)，无净撤回
    assert got[0] == exp[0] == [((1, 1),)]
    assert got[1] == exp[1] == []
    assert got[2] == {"added": 1, "removed": 0}


_QUERIES = [
    "SELECT s.id, r.value FROM stations s LEFT JOIN readings r "
    "ON s.id = r.station_id",
    "SELECT s.id, r.value FROM stations s LEFT JOIN readings r "
    "ON s.id = r.station_id WHERE r.value >= 10",
    "SELECT s.id, r.value FROM stations s LEFT JOIN readings r "
    "ON s.id = r.station_id WHERE r.value IS NULL",
    "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id "
    "WHERE s.threshold >= 5 AND (r.value < 10 OR r.value IS NULL)",
    "SELECT s.id, s.threshold, r.value FROM stations s LEFT JOIN readings r "
    "ON s.id = r.station_id WHERE NOT (r.value >= 10)",
    "SELECT s.id, r.value FROM stations s LEFT JOIN readings r "
    "ON s.id = r.station_id WHERE r.value IS NOT NULL AND s.threshold != 5",
]


@pytest.mark.parametrize("seed", range(12))
def test_randomized_delta_equals_full_rerun(schema, seed):
    """随机数据 + 多形态查询：生成 SQL 的差额必须等于全量重跑差额。"""
    rng = random.Random(seed)
    query = _QUERIES[seed % len(_QUERIES)]
    stations = [
        (i, rng.choice([None, 0, 5, 10])) for i in range(1, rng.randint(2, 5))
    ]
    ids = [s[0] for s in stations]

    def rand_rows(n):
        return [
            (rng.choice(ids + [None]), rng.choice([None, 3, 7, 10, 15]))
            for _ in range(n)
        ]

    readings = rand_rows(rng.randint(0, 6))
    new_readings = rand_rows(rng.randint(0, 4))
    got, exp = _run_delta_case(schema, query, stations, readings, new_readings)
    assert got[0] == exp[0], f"added 不一致: {got[0]} != {exp[0]}"
    assert got[1] == exp[1], f"removed 不一致: {got[1]} != {exp[1]}"
    assert got[2] == {"added": len(exp[0]), "removed": len(exp[1])}
