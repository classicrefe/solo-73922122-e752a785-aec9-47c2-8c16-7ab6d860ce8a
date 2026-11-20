"""API 请求 / 响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .ir import MAX_ROWS


class CheckRequest(BaseModel):
    query_a: str = Field(..., description="原始 SELECT（当前在用的质控 SQL）")
    query_b: str = Field(..., description="改写后的 SELECT")
    solver_timeout_ms: int | None = Field(
        None,
        ge=100,
        le=120000,
        description="z3 求解超时（毫秒）；缺省取环境变量 SOLVER_TIMEOUT_MS 或 10000",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query_a": (
                        "SELECT s.id, r.value FROM stations s "
                        "INNER JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value > 0"
                    ),
                    "query_b": (
                        "SELECT s.id, r.value FROM stations s "
                        "INNER JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value >= 1"
                    ),
                }
            ]
        }
    }


class Counterexample(BaseModel):
    setup_sql: str = Field(..., description="可装载的建表与数据 SQL 脚本")
    query_a_result: list[list[Any]] = Field(..., description="反例数据下 query_a 的实际结果")
    query_b_result: list[list[Any]] = Field(..., description="反例数据下 query_b 的实际结果")


class CheckResponse(BaseModel):
    verdict: Literal["EQUIVALENT", "NOT_EQUIVALENT", "UNKNOWN"] = Field(
        ...,
        description=(
            "EQUIVALENT=界内无反例；NOT_EQUIVALENT=差异已在隔离 PostgreSQL 复现；"
            "UNKNOWN=求解或复现失败"
        ),
    )
    bound_rows: int = Field(
        MAX_ROWS, description="等价结论成立的每表行数上界（含 0 行）"
    )
    message: str = Field(..., description="判定说明")
    counterexample: Counterexample | None = Field(
        None, description="verdict=NOT_EQUIVALENT 时给出可复现的反例"
    )


class IncrementalRequest(BaseModel):
    query: str = Field(
        ...,
        description=(
            "质控 SELECT：必须是 stations 按 id = station_id 左连接 readings，"
            "列投影与 WHERE 沿用 /check 的受支持子集"
        ),
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": (
                        "SELECT s.id, r.value FROM stations s "
                        "LEFT JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value >= 10 OR r.value IS NULL"
                    )
                }
            ]
        }
    }


class IncrementalResponse(BaseModel):
    delta_sql: str = Field(
        ...,
        description=(
            "单条 SELECT：返回净增量明细行 (delta, c1..cN)，"
            "delta='added' 为净新增（需补发）、'removed' 为净撤回（需撤回）；"
            "多重集语义：按列位置、NULL 视为相同、重复逐次计数"
        ),
    )
    count_sql: str = Field(
        ...,
        description="单条 SELECT：返回 (delta, n) 两行，分别给出净新增/净撤回次数",
    )
    projection: list[str] = Field(
        ..., description="明细列 c1..cN 对应的原始投影列（table.column，按位置）"
    )
    required_tables: list[str] = Field(
        ...,
        description=(
            "执行生成的 SQL 时目标库必须存在的表：stations、readings（入仓前存量）、"
            "new_readings（同结构且已通过外键校验的待追加批次）"
        ),
    )
    message: str = Field(..., description="生成说明")
