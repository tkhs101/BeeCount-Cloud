"""自动还款的进程内 self-call(阶段 3b)。

## 为什么不直接操作 DB

自动还款要写的不是「一张新行」,而是完整走一遍 `write` 路径:
tx_id 生成 → `snapshot_mutator` 字段规范化与校验 → `projection.upsert_tx`
→ `AuditLog` → `broadcast_to_ledger`(WS 推送)。

直接操作 DB 就要把这一整套**重实现一遍**。漏任何一步都会命中
CLAUDE.md 记录的那几个**静默**丢失坑(税额被抹、splits 被删、
nativeAmount 变 NULL),而且不报错。

`mcp/tools/write_tools.py:71-92` + `_mcp_internal_client.py` 是仓库里
唯一的「非用户 HTTP 触发,但完整走全套」的先例。复用它,等于白拿一整套
已经验证过的逻辑。

## 同步/异步的桥接

MCP 的 self-call 是 **async** 的(`httpx.AsyncClient`),而 APScheduler 的
job 跑在**线程池**里(同步上下文)。这里用 `asyncio.run()` 在线程内起一个
独立事件循环 —— 每个线程一个 loop,不与 MCP server 的主 loop 共享。

⚠️ 不要试图复用 MCP server 的事件循环:调度器线程拿不到它,而且共用
一个 loop 会让「等待一个 await」阻塞整个 server。

## 身份

用 `_internal_token()` 自签**短期** JWT(`write_tools.py:59-69` 的做法),
`scopes=[SCOPE_APP_WRITE]`、`client_type="app"`。不签长期 token 落盘 ——
那会变成一个躺在磁盘上的万能凭据。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger("beecount.credit_autorepay")


def make_self_call() -> Any:
    """返回一个同步可调用的 `self_call(db, *, method, path, body, headers)`。"""

    def self_call(db: Session, *, method: str, path: str,
                  body: dict[str, Any], headers: dict[str, str]) -> Any:
        return asyncio.run(_do(method, path, body, headers))

    return self_call


async def _do(method: str, path: str, body: dict[str, Any],
              headers: dict[str, str]) -> Any:
    import httpx

    from ..._mcp_internal_client import close_internal_client, get_internal_client
    from ..mcp.tools.write_tools import _internal_token
    from ...security import SCOPE_APP_WRITE

    client = get_internal_client()
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers)
    hdrs["Authorization"] = f"Bearer {_internal_token(scopes=[SCOPE_APP_WRITE])}"

    resp = await client.request(method, path, json=body, headers=hdrs)
    if resp.status_code >= 400:
        # 4xx/5xx 一律抛 —— 让执行器走「失败撤回 last_period」的路径。
        # 静默吞掉会让这张卡这一期永远不再还,而用户以为还了。
        raise RuntimeError(
            f"auto-repay write failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"raw": resp.text}