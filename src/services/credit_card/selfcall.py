"""自动还款的进程内 self-call(阶段 3b)。

## 为什么不直接操作 DB

自动还款要写的不是「一张新行」,而是完整走一遍 `write` 路径:
tx_id 生成 → `snapshot_mutator` 字段规范化与校验 → `projection.upsert_tx`
→ `AuditLog` → `broadcast_to_ledger`(WS 推送)。

直接操作 DB 就要把这一整套**重实现一遍**。漏任何一步都会命中
CLAUDE.md 记录的那几个**静默**丢失坑(税额被抹、splits 被删、
nativeAmount 变 NULL),而且不报错。

## 直接复用 MCP 的 `_self_call`,不自造

第一版在这里自己实现了一遍「签 token + 发请求 + 判错误」,结果连着三个
笔误:`from ..mcp...` 导入路径错(多了一层 `services`)、`_internal_token()`
签名猜错(它要 `user: User` 不是 `scopes=`)、以及漏了 `X-Device-ID`。

而 `mcp/tools/write_tools.py:71` 的 `_self_call` **已经把这些全做好了** ——
它是仓库里唯一的「非用户 HTTP 触发,但完整走 `_commit_write` 全套」先例。

教训:要复用某个模式时,**先去找它是否已经存在**,而不是照着描述重写。
重写的每一行都是新的出错机会,而复用的那份已经在生产路径上跑着。

## 同步/异步的桥接 —— **两种调用上下文,两种做法**

`_self_call` 是 async 的,调用方有两种:

1. **APScheduler 的 job** —— 线程池里,**没有**运行中的事件循环,
   可以用 `asyncio.run()` 起独立 loop
2. **FastAPI 端点**(`POST .../autorepay/run`)—— `async def`,**已经在
   运行的事件循环里**。这里 `asyncio.run()` 直接抛
   `RuntimeError: asyncio.run() cannot be called from a running event loop`

第一版只考虑了场景 1,冒烟测试点一次手动触发就炸出场景 2。而当时端点把
异常吞成 `status="failed"` 再返回 HTTP 200,所以**看起来只是「点了没反应」**,
排查绕了一大圈 —— 那次吞异常的问题一并修了(失败现在返回 502/400)。
"""
from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.orm import Session


def make_self_call() -> Any:
    """**同步**上下文用(APScheduler 线程池)。

    返回的 `self_call(...)` 是普通函数,内部 `asyncio.run()` 起独立 loop。
    """

    def self_call(db: Session, *, method: str, path: str,
                  body: dict[str, Any], headers: dict[str, str],
                  user: Any) -> Any:
        # 参数名统一用 (与执行器一致); 需要 httpx 的
        #  关键字,在这里转换一次。名字不统一过一次,端点报
        # TypeError 才发现。
        return asyncio.run(call_async(
            method=method, path=path, body=body, headers=headers, user=user))

    return self_call


async def call_async(*, method: str, path: str, body: dict[str, Any],
                     headers: dict[str, str], user: Any) -> Any:
    """**async** 上下文用(FastAPI 端点)—— 复用当前 loop,不起新的。"""
    from ...mcp.tools.write_tools import _self_call

    return await _self_call(method, path, user, json=body, headers=headers)
