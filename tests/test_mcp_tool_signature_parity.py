"""MCP 工具的**四层签名必须一致**(第 136 轮)

## 它防什么(一次真实发生过的)

`create_transaction_with_receipt` 的 docstring 写着「参数与 `create_transaction`
**完全一致**」—— 但它**独缺 `splits`**。后果是 agent 想「带小票地建一笔组合支付」
做不到,**没有任何报错**:工具正常返回,只是悄悄少一个能力。

根因是这条链有**四层**,而加参数时很容易只改其中一层:

| # | 层 | 加 `splits` 时要动吗 |
|---|---|---|
| 1 | `write_tools` 的函数签名与 `patch` | 要 |
| 2 | `server.py` 里对应的 `TypedDict` | 要(mypy 会报,层数 2 唯一有护栏的) |
| 3 | `@mcp.tool()` wrapper 的签名 | 要(**无护栏**) |
| 4 | wrapper 里组装 `kw` 的 dict 字面量 | 要(**无护栏**) |

我第一版**只改了第 1 层**,第 3、4 层都没动 —— 而第 2 层的 TypedDict
会让 `mypy` 报 `Name "splits" is not defined`,所以**它被挡住了**。
但如果当时顺手把 TypedDict 也写了(比如它是 `total=False`),
**四层里三层漏掉也照样全绿**。

📌 **本护栏比较的是「wrapper 签名」与「底层实现签名」** ——
第 3 层与第 1 层的直接对照。

⚠️ **第 4 层**(kw 字面量漏传)前两条**结构上就盖不到**:实测把
`"splits": splits,` 删掉,它们**照样全绿**(不改签名、不改 schema)。
我第一版在 docstring 里写「第 4 层由 `test_mcp_attachments` 覆盖」——
**那句话是凭印象写的,实测下来是错的**,那条护栏也盖不到。
现在由文件末尾那条查源码的护栏兜住。

## 为什么用 `inspect.signature` 而不是 MCP schema

MCP schema 是**从第 3 层生成的**,拿它跟第 1 层比就是「结果 vs 源」;
而直接比函数签名能在**不需要起 MCP server** 的前提下定位到差异。
两条判据不重复:`test_mcp_attachments` 比的是**两个工具之间**,
本条比的是**同一工具的上下两层之间**。
"""

from __future__ import annotations

import inspect

from src.mcp import server as S
from src.mcp.tools import read_tools as R
from src.mcp.tools import write_tools as W

_IMPLS = (W, R)

#: 注入参数,不参与比对。
_NOISE_IMPL = {"user"}
_NOISE_WRAPPER = {"ctx"}


def _collect() -> list[tuple[str, inspect.Signature, inspect.Signature]]:
    """[(工具名, 实现签名, wrapper 签名)] —— 只收两边都存在的。"""
    out: list[tuple[str, inspect.Signature, inspect.Signature]] = []
    for mod in _IMPLS:
        for name, fn in vars(mod).items():
            if name.startswith("_") or not inspect.iscoroutinefunction(fn):
                continue
            wrapper = getattr(S, name, None)
            if wrapper is None or not inspect.iscoroutinefunction(wrapper):
                continue
            out.append((name, inspect.signature(fn), inspect.signature(wrapper)))
    return out


PAIRS = _collect()


def test_enough_pairs_compared_so_the_rest_is_not_vacuous() -> None:
    """⚠️ **下限自检**。

    「签名全一致」在**比较了 0 对**时也是真的 —— 那是最危险的一种绿:
    看起来有护栏,实际上什么都没比。

    同 CLAUDE.md 记的「正则失效要自检」「tokenize 恒真」那几条。
    """
    assert len(PAIRS) >= 8, f"只比较到 {len(PAIRS)} 对,判据可能已失效:{[p[0] for p in PAIRS]}"


def test_wrapper_signature_matches_implementation() -> None:
    """每个工具的 `@mcp.tool()` wrapper 必须接受底层实现接受的全部参数。

    少一个 = 那个能力在 MCP 层**静默不可用**(工具照常返回,不报错)。
    """
    problems: list[str] = []
    for name, impl_sig, wrap_sig in PAIRS:
        impl_params = set(impl_sig.parameters) - _NOISE_IMPL
        wrap_params = set(wrap_sig.parameters) - _NOISE_WRAPPER

        missing = sorted(impl_params - wrap_params)
        if missing:
            problems.append(f"{name}: wrapper 缺少 {missing}")

        # 反方向只报不失败 —— 现有代码可能有意多带 ctx 之外的参数,
        # 那需要人判断,不在这条护栏的职责内。
        extra = sorted(wrap_params - impl_params)
        if extra:
            problems.append(f"{name}: wrapper 多出 {extra}(请人工确认是否有意)")

    assert not problems, "\n".join(problems)


def test_every_mcp_schema_exposes_wrapper_params() -> None:
    """wrapper 参数必须真的出现在 MCP schema 里。

    ⚠️ 这一条抓的是**另一层**:签名对、但参数没被 Pydantic 收进去
    (比如漏了类型标注 → schema 里没这个字段,调用方传了也被忽略)。
    """
    import asyncio

    tools = {t.name: t for t in asyncio.run(S.mcp.list_tools())}
    assert len(tools) >= 8, f"MCP 只注册了 {len(tools)} 个工具,判据可能失效"

    missing: list[str] = []
    for name, _impl_sig, wrap_sig in PAIRS:
        schema = tools.get(name)
        if schema is None:
            missing.append(f"{name}: 未注册为 MCP 工具")
            continue
        props = set(schema.inputSchema.get("properties", {}))
        expected = set(wrap_sig.parameters) - _NOISE_WRAPPER
        gone = sorted(expected - props)
        if gone:
            missing.append(f"{name}: schema 里没有 {gone}")

    assert not missing, "\n".join(missing)

def test_receipt_variant_forwards_every_param_to_the_implementation() -> None:
    """**第 4 层**(kw 字面量)只有查源码能抓 —— 上面两条结构上都盖不到。

    ⚠️ 本轮实测:把 `"splits": splits,` 从 wrapper 的 kw dict 删掉,
    上面两条护栏**照样全绿** —— 少传一项不改签名、不改 schema。

    判据 = wrapper 源码里组装 `kw` 的那段,**每个参数都必须出现**。
    """
    name = "create_transaction_with_receipt"
    impl_params = set(inspect.signature(getattr(W, name)).parameters) - {"user"}
    wrap_params = set(inspect.signature(getattr(S, name)).parameters) - {"ctx"}

    assert impl_params == wrap_params, "签名层已经不一致,先修那个"

    body = inspect.getsource(getattr(S, name))
    missing = sorted(p for p in impl_params if f'"{p}": {p},' not in body)
    assert not missing, (
        f"{name} 的 kw 字面量里没有透传 {missing}"
        "  -> 签名和 schema 都对,但底层实际拿不到这些参数 —— "
        "**结构判据全绿,功能却是坏的**"
    )
