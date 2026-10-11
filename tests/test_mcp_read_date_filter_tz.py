"""MCP 读工具的日期筛选 —— 必须归一到 UTC。

## 用户实际遇到的事

用户在东京(UTC+9),让 MCP 查「5 月 13 号之后的交易」,传
`date_from=2026-05-13T18:00:00+09:00`。

那笔交易确实在库里(`happened_at = 2026-05-13T09:00:00Z`,东京 18:00 记的)。
但查**不到** —— 返回空列表,不报错,不提示。

## 为什么单元测试查不出来

纯 Python 里两个 aware datetime 比较会**自动归一**:

    datetime(2026,5,13,9,tzinfo=utc) >= datetime(2026,5,13,18,tzinfo=+9)   # True

所以 REPL 里和「assert 返回值带 +09:00 偏移」的测试都**看不出问题**。
只有落到 SQL 驱动才炸。

因此这里的核心那条测试**必须穿过真 SQLite**。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.mcp.tools.read_tools import _parse_dt
from src.models import ReadTxProjection

TOKYO = timezone(timedelta(hours=9))


@pytest.mark.parametrize("raw,expect_utc", [
    ("2026-05-13T18:00:00+09:00", "2026-05-13T09:00:00+00:00"),
    ("2026-05-13T07:00:00-05:00", "2026-05-13T12:00:00+00:00"),
    ("2026-05-13T09:00:00Z", "2026-05-13T09:00:00+00:00"),
    ("2026-05-13T09:00:00", "2026-05-13T09:00:00+00:00"),   # naive 按 UTC
    ("2026-05-13", "2026-05-13T00:00:00+00:00"),
])
def test_parse_dt_normalizes_to_utc(raw: str, expect_utc: str) -> None:
    got = _parse_dt(raw)
    assert got.utcoffset() == timedelta(0), (
        f"{raw} → {got.isoformat()}(偏移 {got.utcoffset()})。"
        f"这个值直接进 SQL 去和库里的 UTC 比较,带偏移就查不到")
    assert got.isoformat() == expect_utc, got.isoformat()


def test_end_of_day_still_works_with_offset() -> None:
    """`date_to` 的日末逻辑在带偏移时也要对。

    `date_to=2026-05-13` → 当天 23:59:59 结束。这一条最容易被改坏:
    加归一时如果把 `end_of_day` 的替换忘了,日末就变成日初。
    """
    got = _parse_dt("2026-05-13", end_of_day=True)
    assert got.isoformat() == "2026-05-13T23:59:59+00:00", got.isoformat()


def test_sql_filter_finds_the_transaction() -> None:
    """**穿过真 SQLite** 的那条 —— 纯 Python 比较会自动归一,掩盖问题。

    库里那笔是东京 18:00 记的(= UTC 09:00)。用户传
    `date_from=2026-05-13T18:00:00+09:00` 问「13 号之后」,**应该查得到**。
    """
    from src.database import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine)

    # 东京 5/13 18:00 记的账 = UTC 5/13 09:00
    with TS() as db:
        db.add(ReadTxProjection(
            ledger_id="l", sync_id="t", user_id="u", tx_type="expense",
            amount=100.0, happened_at=datetime(2026, 5, 13, 9, 0,
                                              tzinfo=timezone.utc),
            source_change_id=1,
        ))
        db.commit()

    # 复现 MCP 读工具的查询:`.where(happened_at >= _parse_dt(date_from))`
    with TS() as db:
        rows = db.scalars(
            select(ReadTxProjection).where(
                ReadTxProjection.happened_at >= _parse_dt(
                    "2026-05-13T18:00:00+09:00")
            )
        ).all()

    assert len(rows) == 1, (
        f"查不到 —— 库里那笔正是东京 {18}:00 记的(= UTC 09:00)。"
        f"查询边界比库里时刻晚了 9 小时。这条不报错、不提示,用户只看到空列表。")


def test_same_input_agrees_with_projection_contract() -> None:
    """与 `projection._parse_happened_at` 对同一输入必须给出同一时刻。

    这是本仓第五次栽在「同一个转换两份实现」上:
    `_to_iso8601` / `projection._parse_happened_at` 已经统一,
    MCP 这个读工具的 `_parse_dt` 却还在自己解析。
    """
    from src.projection import _parse_happened_at

    for raw in ("2026-05-13T18:00:00+09:00", "2026-05-13T07:00:00-05:00",
                "2026-05-13T09:00:00Z", "2026-05-13T09:00:00"):
        assert _parse_dt(raw) == _parse_happened_at(raw), (
            f"{raw}:MCP 读工具 { _parse_dt(raw).isoformat() } "
            f"vs projection { _parse_happened_at(raw).isoformat() }")

# --------------------------------------------------------------------------- #
# CSV 导入路径:同一个「两层都得输出 UTC」的契约                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw,tz_off,expect", [
    # CSV 自带时区标记 → **以它为准**,不能被 tz_offset_minutes 再挪一次
    ("2026-05-13T18:00:00+09:00", 540, "2026-05-13T09:00:00+00:00"),
    ("2026-05-13T09:00:00Z", 540, "2026-05-13T09:00:00+00:00"),
    ("2026-05-13T00:00:00-05:00", 540, "2026-05-13T05:00:00+00:00"),
    # 裸墙钟 → 才用 tz_offset_minutes 换算(issue #314 的正解)
    ("2026-05-13T18:00:00", 540, "2026-05-13T09:00:00+00:00"),
    # 老客户端没传偏移 → 当作 UTC(兼容旧行为)
    ("2026-05-13T18:00:00", None, "2026-05-13T18:00:00+00:00"),
])
def test_csv_import_always_outputs_utc(raw: str, tz_off, expect: str) -> None:
    """CSV 导入的输出**必须**是 UTC,否则同一列里会混两种口径。

    原实现对已带偏移的输入「原样保留」→ `18:00+09:00` 一路穿到
    `projection._parse_happened_at` 才归一,**归一发生得太晚**:
    SQLite 在 `upsert_tx` 落库时存的是墙钟,于是库里同时存在
    「UTC 墙钟」和「本地墙钟」两种值,统计/排序/筛选全乱。
    """
    from src.services.import_data.transformer import _parse_datetime

    got = _parse_datetime(raw, None, tz_off)
    assert got.utcoffset() == timedelta(0), (
        f"{raw}(tz_offset={tz_off}) → {got.isoformat()},带偏移穿过了写入路径")
    assert got.isoformat() == expect, (
        f"{raw}(tz_offset={tz_off}):"
        f"CSV 自带时区标记时 tz_offset_minutes 会被二次应用")


def test_csv_output_matches_the_rest_of_the_write_path() -> None:
    """CSV 的输出必须与 `projection._parse_happened_at` 口径一致。

    两者对同一个 ISO 串给出不同时刻 = 库里两套口径。
    """
    from src.projection import _parse_happened_at
    from src.services.import_data.transformer import _parse_datetime

    for raw in ("2026-05-13T18:00:00+09:00", "2026-05-13T09:00:00Z",
                "2026-05-13T18:00:00"):
        assert _parse_datetime(raw, None, None) == _parse_happened_at(raw), raw
