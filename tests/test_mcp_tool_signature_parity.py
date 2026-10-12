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

def test_receipt_wrapper_actually_forwards_splits(monkeypatch) -> None:
    """wrapper 必须把参数**真的**透传下去 —— 判据是**真调一次**。

    ⚠️ 这条护栏原来断言「wrapper 源码里有 `"splits": splits,`」。那是
    **源码文本判据**,而 151-153 轮已实测这一族对「代码不可达」**无效**
    (文本还在、代码不跑,护栏照样绿)——
    `mutation_check.py --mode unreachable` 就是为抓这一族加的。

    ### 为什么替换 `_logged_call` 而不是造一个假 ctx

    我先试了传 `ctx=None`、再试 `SimpleNamespace(request_context=...)` ——
    **连错两次**:wrapper 走 `_logged_call`,而它内部要 `get_mcp_user_from_context`
    + `require_mcp_scope`(真鉴权),假 ctx 补不齐,第三次又冒出 `scope` 缺失。

    📌 **别逐个属性试**,去读被调的那个函数。而这里正确解法更简单:
    **把 `_logged_call` 本身换掉** —— 我要验的是「wrapper 拼出来的 kw 对不对」,
    鉴权不属于这件事。**隔离掉与判据无关的依赖。**
    """
    import asyncio
    import inspect as _inspect
    from types import SimpleNamespace

    import src.mcp.server as S

    captured: dict = {}

    async def _fake_logged_call(ctx, *, name, scope, kwargs, body):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(S, "_logged_call", _fake_logged_call)

    params = _inspect.signature(S.create_transaction_with_receipt).parameters
    kwargs = {n: ("SENTINEL-" + n) for n, p in params.items()
              if n != "ctx" and p.default is not p.empty}
    kwargs["amount"] = 1.0
    kwargs["image_base64"] = "Zm9v"

    asyncio.run(S.create_transaction_with_receipt(
        SimpleNamespace(), **kwargs))

    missing = sorted(k for k in kwargs if k not in captured)
    assert not missing, (
        f"wrapper 没有把这些参数透传下去:{missing}"
        "  -> 签名层与 schema 层都绿,但底层实际拿不到 —— **结构全对、功能是坏的**"
    )
    assert captured.get("splits") == "SENTINEL-splits", (
        f"splits 没有原样传下去,实际拿到 {captured.get('splits')!r}"
    )


def test_create_transactions_schema_exposes_splits() -> None:
    """批量建交易的 MCP schema **必须**暴露 `splits`。

    ⚠️ `BatchTxItem` 是 **TypedDict**,而它的另一个身份是
    **给 LLM 看的 JSON Schema 声明**。不写这个字段,LLM **根本不知道**
    这个工具能建组合支付 —— 工具没坏,只是没人会调用那个参数。

    这与 `test_no_write_request_field_is_declared_and_forgotten` 是同一族,
    但那一条只看「代码有没有读」;这一条看「**schema 有没有暴露**」。
    少任何一半,功能都不完整。
    """
    import asyncio

    tools = {t.name: t for t in asyncio.run(S.mcp.list_tools())}
    schema = tools["create_transactions"].inputSchema
    item = schema["properties"]["transactions"]["items"]

    # ⚠️ **`items` 是 `$ref`,不是内联对象** —— 第一版直接读
    # `items["properties"]`,于是 `props` 恒为空,**判据恒假**。
    # 这与「扫到 0 个字段就说没问题」是同一类:安静地什么都不检查。
    # 所以这里两条路径都处理,并断言**确实解析出了 properties**。
    if "$ref" in item:
        ref_name = item["$ref"].rsplit("/", 1)[-1]
        item = schema["$defs"][ref_name]
    props = item.get("properties") or {}
    assert props, "既没解析出 $defs,也没有内联 properties —— 判据失效"

    assert "splits" in props, (
        "批量建交易的 schema 里没有 splits —— LLM 看不到这个能力。"
        f" 实际字段:{sorted(props)}"
    )
