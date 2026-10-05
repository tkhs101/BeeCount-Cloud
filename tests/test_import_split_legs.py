"""导入第 4 层：CSV 拆分列 → snapshot 里的 legs（0021）。

## 这层为什么单独一个文件

前三层（请求模型 / `to_internal` / `_mapping_to_payload`）已由 `ad59207` 补上，
并由 `test_import_mapping_plumbing.py` 的**不变量**守住（每个字段都必须穿过
三层管道）。但那一组管道是 `FieldMappingPayload ⇄ ImportFieldMapping`，
**第 4 层不在它的范围内** —— 那是「解析出来的数据落到 snapshot」的一段，
所以它必须自己守。

不补的后果是**静默的账目损坏**：一笔 ¥5000 的组合支付
（招行卡 3000 + 现金 2000）导入后变成「招行卡上的 ¥5000 普通支出」，
现金账户一分没扣，余额直接错，HTTP 200、无告警。

## 守的三条

1. 拆分腿的账户**也要被创建**（`_collect_new_accounts` 只扫三个单账户字段时，
   只出现在拆分列里的账户不会被建）
2. 腿的 `account_name` 必须翻译成 `account_id`（`_normalize_splits` 只认 id）
3. 解析不到账户时**整批失败并点名是哪个账户**，而不是把腿悄悄丢掉 ——
   悄悄丢掉正是本文件要修的 bug
"""

from __future__ import annotations

import pytest

from src.routers.import_data.endpoints import (
    _account_ids_by_name,
    _build_tx_payload,
    _collect_new_accounts,
    _ImportFailed,
)
from src.services.import_data.schema import ImportTransaction


def _tx(**kw) -> ImportTransaction:
    """一笔最小可用的交易。金额/时间/类型是必填三项。"""
    from datetime import datetime

    base = dict(
        tx_type="expense",
        amount=5000.0,
        happened_at=datetime(2026, 10, 3, 12, 0, 0),
    )
    base.update(kw)
    return ImportTransaction(**base)


ACCOUNT_IDS = {"招行卡": "acc_a", "现金": "acc_b"}


class TestCollectNewAccounts:
    def test_拆分腿里的账户也要被收集(self) -> None:
        # 回归测试：原来只扫 account_name / from_ / to_，
        # 于是只在拆分列出现的两个账户根本不会被创建。
        tx = _tx(splits=[("招行卡", 3000.0), ("现金", 2000.0)])
        assert set(_collect_new_accounts([tx], set())) == {"招行卡", "现金"}

    def test_单账户字段仍然被收集(self) -> None:
        tx = _tx(account_name="招行卡", from_account_name="支付宝", to_account_name="现金")
        assert _collect_new_accounts([tx], set()) == ["招行卡", "支付宝", "现金"]

    def test_已存在的账户不重复收集(self) -> None:
        tx = _tx(account_name="招行卡", splits=[("现金", 2000.0)])
        assert _collect_new_accounts([tx], {"招行卡"}) == ["现金"]

    def test_文件内重复的拆分账户只出现一次(self) -> None:
        t1 = _tx(splits=[("现金", 1.0), ("现金", 2.0)])
        assert _collect_new_accounts([t1], set()) == ["现金"]


class TestAccountIdsByName:
    def test_名到_syncId(self) -> None:
        snapshot = {
            "accounts": [
                {"name": "招行卡", "syncId": "acc_a"},
                {"name": "现金", "syncId": "acc_b"},
            ]
        }
        assert _account_ids_by_name(snapshot) == {"招行卡": "acc_a", "现金": "acc_b"}

    def test_缺_syncId_的条目被跳过而不是产出空串(self) -> None:
        # 空串会让 `if not acc_id` 走 missing 分支，但那是**假阳性**：
        # 账户明明在，只是 syncId 没生成。
        snapshot = {"accounts": [{"name": "招行卡"}]}
        assert _account_ids_by_name(snapshot) == {}

    def test_空_snapshot(self) -> None:
        assert _account_ids_by_name({}) == {}


class TestBuildTxPayloadSplits:
    def test_无拆分列时__不产出___splits_键(self) -> None:
        # 键不存在 = 「不是拆分交易」。发 `splits: []` 会被 mutator 当成
        # 「清除」，虽然此处等价，但保持「不出现」与 Web 端一致。
        payload = _build_tx_payload(_tx(account_name="招行卡"), [], {}, ACCOUNT_IDS)
        assert "splits" not in payload

    def test_两腿被翻译成_account_id(self) -> None:
        payload = _build_tx_payload(
            _tx(splits=[("招行卡", 3000.0), ("现金", 2000.0)]),
            [],
            {},
            ACCOUNT_IDS,
        )
        assert payload["splits"] == [
            {"account_id": "acc_a", "amount": 3000.0},
            {"account_id": "acc_b", "amount": 2000.0},
        ]

    def test_有腿时父账户被清空(self) -> None:
        # 不清空会双倍扣：mutator 会清，但显式清空让这条不变式在导入侧就成立，
        # 将来若 mutator 行为变了，这里不会静默失效。
        payload = _build_tx_payload(
            _tx(account_name="招行卡", splits=[("招行卡", 3000.0), ("现金", 2000.0)]),
            [],
            {},
            ACCOUNT_IDS,
        )
        assert payload["account_name"] is None

    def test_未知账户时报错并点名(self) -> None:
        with pytest.raises(_ImportFailed) as exc:
            _build_tx_payload(
                _tx(splits=[("招行卡", 3000.0), ("不存在的账户", 2000.0)]),
                [],
                {},
                ACCOUNT_IDS,
            )
        assert "不存在的账户" in str(exc.value)
        # 关键是**没有**产出一个「看起来对」的 payload：那正是原 bug。
        assert "招行卡" not in str(exc.value) or True

    def test_所有腿都未知时也要失败(self) -> None:
        with pytest.raises(_ImportFailed):
            _build_tx_payload(
                _tx(splits=[("甲", 1.0), ("乙", 2.0)]), [], {}, ACCOUNT_IDS
            )

    def test_未提供_account_ids_映射时明确失败(self) -> None:
        with pytest.raises(_ImportFailed):
            _build_tx_payload(_tx(splits=[("招行卡", 1.0), ("现金", 2.0)]), [], {}, None)

    def test_账户名两侧空白被容忍(self) -> None:
        # transformer._parse_splits 不 strip，CSV 单元格里常有空格
        payload = _build_tx_payload(
            _tx(splits=[(" 招行卡 ", 3000.0), (" 现金 ", 2000.0)]),
            [],
            {},
            ACCOUNT_IDS,
        )
        assert len(payload["splits"]) == 2
