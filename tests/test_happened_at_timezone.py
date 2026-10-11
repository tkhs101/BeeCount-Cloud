"""`happened_at` 的时区契约。

## 新 UI 团队实测抓到的 bug

> `happened_at` 的时区契约:库内存朴素本地时间、API 却序列化成 `Z`,
> UTC+9 上整体 +9h。

他们**没有**在前端单方面修(那会让新旧 UI 分叉),而是把根因登记在后端
序列化层。这个文件就是那条根因的回归测试。

## 根因链

1. SQLite 没有原生 datetime 类型,SQLAlchemy 存的是**墙钟字符串**
2. 写一个 aware `18:00+09:00`(= UTC 09:00)进去,库里落 `18:00`
   —— **UTC 的那 9 个小时消失了**
3. 读回来是 naive `18:00`,读路径的 `_to_utc` 对 naive 一律
   「假设是 UTC」→ 序列化成 `18:00Z`
4. 前端按本地时区渲染 → **次日 03:00**

不报错、不告警,只是所有交易的时间都偏一个时区。

## 为什么是 `_parse_happened_at` 的错

`snapshot_mutator._to_iso8601` 早就做了 UTC 归一,而
`projection._parse_happened_at`(写 ORM 的那个)**没有** ——
同一个转换两份实现,一份对一份错。凡是看到「两个函数做同一件事」,
先确认它们行为一致。

## 契约

- **任何**带 offset 的输入,进库前必须归一到 UTC
- naive 输入按 UTC 解释(与 `_to_utc` / `_to_iso8601` 同一约定)
- `_parse_happened_at` 与 `_to_iso8601` 对同一输入必须给出**同一时刻**
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.projection import _parse_happened_at
from src.snapshot_mutator import _to_iso8601

TOKYO = timezone(timedelta(hours=9))
NY = timezone(timedelta(hours=-5))


@pytest.mark.parametrize("raw", [
    "2026-10-08T18:00:00+09:00",     # 东京 aware → UTC 09:00
    "2026-10-08T07:00:00-05:00",     # 纽约 aware → UTC 12:00
    "2026-10-08T09:00:00Z",          # UTC Z
    "2026-10-08T09:00:00+00:00",     # UTC +00:00
])
def test_aware_input_is_normalized_to_utc(raw: str) -> None:
    """带 offset 的输入必须归一到 UTC,不能原样进库。"""
    got = _parse_happened_at(raw)
    assert got.tzinfo is not None
    assert got.utcoffset() == timedelta(0), (
        f"{raw} 没有得到 UTC 的 datetime({got.utcoffset()})—— "
        f"非 UTC 的 aware datetime 进 SQLite 会丢掉那一段偏移")
    # 具体值:东京 18:00 = UTC 09:00;纽约 07:00(-05:00) = UTC 12:00;
    # 带 Z / +00:00 的本来就是 UTC,原样。
    expect_hour = {"+09:00": 9, "-05:00": 12, "Z": 9, "+00:00": 9}
    want = next(v for k, v in expect_hour.items() if raw.endswith(k))
    assert got.hour == want, f"{raw} 应归一成 UTC {want}:00,实得 {got.isoformat()}"


@pytest.mark.parametrize("raw", [
    "2026-10-08T18:00:00+09:00",
    "2026-10-08T18:00:00",
    "2026-10-08T09:00:00Z",
    "2026-10-08T07:00:00-05:00",
])
def test_two_converters_agree(raw: str) -> None:
    """`_parse_happened_at` 与 `_to_iso8601` 必须给出同一时刻。

    这两份是同一个转换的两份实现 —— 第一份错、第二份对,就是这次 bug 的
    形状。以后再加第三个转换函数前,先想清楚为什么不能复用这两个。
    """
    from_datetime = _parse_happened_at(raw).isoformat()
    from_string = _to_iso8601(raw)
    assert from_datetime.replace("+00:00", "+00:00") == from_string.replace(
        "Z", "+00:00"), (
        f"同一输入两个转换给出不同结果:{from_datetime} vs {from_string}")


def test_naive_is_interpreted_as_utc() -> None:
    """naive 按 UTC 解释 —— 库里存的是 UTC 墙钟,naive → UTC 才不会
    引入第二次偏移。"""
    got = _parse_happened_at("2026-10-08T18:00:00")
    assert got == datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)


def test_malformed_falls_back_to_now() -> None:
    got = _parse_happened_at("not-a-date")
    assert got.tzinfo is timezone.utc
    assert abs(datetime.now(timezone.utc) - got) < timedelta(minutes=1)


# --------------------------------------------------------------------------- #
# 真 DB round-trip:证明「进库的是 UTC 时刻」                                 #
# --------------------------------------------------------------------------- #


def test_sqlite_roundtrip_preserves_the_instant() -> None:
    """端到端:东京 18:00 写进去,读回来必须还是**同一个瞬间**。

    这个测试直接对着 SQLite 跑 —— 上面的单元测试只能证明
    `_parse_happened_at` 返回了正确的 aware datetime,而**丢掉时区**那一步
    发生在 SQLAlchemy 的 SQLite 驱动里,只能这样才看得见。
    """
    from src.database import Base
    from src.models import ReadTxProjection

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine)

    # 用户在东京记了一笔 18:00 的账 = UTC 09:00
    tokyo_wall = datetime(2026, 10, 8, 18, 0, tzinfo=TOKYO)
    with TS() as db:
        db.add(ReadTxProjection(
            ledger_id="l", sync_id="t", user_id="u", tx_type="expense",
            amount=1.0, happened_at=_parse_happened_at(
                tokyo_wall.isoformat()),
            source_change_id=1,
        ))
        db.commit()

        # SQLite 里存的原文 —— 必须是 UTC 的 09:00,不是墙钟 18:00
        raw = db.execute(text(
            "select happened_at from read_tx_projection where sync_id='t'"
        )).scalar()
        assert "09:00" in str(raw), (
            f"库里存的是 {raw!r} —— 东京 18:00 被存成了 18:00,"
            f"UTC 的 9 个小时消失了。读回后会被当成 18:00Z,"
            f"前端渲染成次日 03:00。")

        # 读回来的 datetime
        got = db.scalar(select(ReadTxProjection.happened_at))
        assert got == datetime(2026, 10, 8, 9, 0), got

    # 读路径的序列化:`_to_utc` 假设 naive 是 UTC → 09:00Z → 前端渲染回
    # 东京 18:00。闭环成立。
    from src.routers.read._shared import _to_utc
    assert _to_utc(got).isoformat() == "2026-10-08T09:00:00+00:00"
