"""护栏:账户类型在**四处注册表**里必须一致。

## 为什么需要

新增一个账户类型要在**四个地方**登记,而漏掉任何一处的表现都是**静默的**:

| 注册表 | 漏了会怎样 |
|---|---|
| `AccountsPanel.TRADABLE_TYPES` / `VALUATION_TYPES` | **`computeTypeGroups` 用 `ACCOUNT_ORDER.filter(...)`,不在表里的类型整个分组被丢弃** → 账户能建、能记账、余额算得对,但**账户列表页完全看不到**;而 hero 净值和首页饼图照常把它算进总额 → 用户看到「总资产里有这笔钱但列表里找不到」,且不报任何错 |
| `AccountsPanel.TYPE_ICON_URL` | `TypeIcon` 回退到 `other_account.svg`,图标错但不报错 |
| `AccountsPanel.TYPE_COLORS` | `computeTypeGroups` 回退 `'#94a3b8'`,变成灰色 |
| `AssetCompositionDonut.TYPE_META` / `HomeTopAccounts` 的两份副本 | 同上(灰 / 别的图标),且 HomeTopAccounts 那两份是**独立副本**,注释明说是「无导出引用降低耦合」故意复制的 —— 没有任何机制会提醒 |

再加两处:

- **i18n 三语**:`t('accountType.' + type)`,缺 key 时 `t()` 返回**裸 key 字符串**
  `accountType.bank_account`,直接显示在饼图图例上。
  保护它的只有 `i18n.test.ts` 跑测试时才发现;`TranslationKey` 类型**没有
  编译期约束**(`t` 签名是 `(k: string) => string`)。
- **MCP `VALID_ACCOUNT_TYPES`**:服务端写入路径其实不校验 account_type(自由文本),
  只有 MCP 工具查这份白名单。前端下拉是另一份,两边会漂移。

所以这条护栏同时校验**全部六个注册表 + 三语 i18n + 图标文件存在**。
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
WEB_SRC = REPO / "frontend" / "apps" / "web" / "src"
FEATURES_SRC = REPO / "frontend" / "packages" / "web-features" / "src"
ICONS_DIR = REPO / "frontend" / "apps" / "web" / "public" / "icons" / "account"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _array_values(src: str, const_name: str) -> set[str]:
    """从 `const X: T[] = [ { value: 'a' }, ... ]` 里抽出 value 集合。"""
    # 类型注解里有 `{`(如 `{ value: string }[]`),所以不能用 `[^{]*`;
    # 闭合 `]` 在行首(零缩进)。这两点都踩过。
    m = re.search(rf"const\s+{const_name}\b[^=]*=\s*\[(.*?)\n\]", src, re.S)
    assert m, f"未找到 {const_name}"
    return set(re.findall(r"value:\s*'([a-z_]+)'", m.group(1)))


def _record_keys(src: str, const_name: str) -> set[str]:
    """从 `const X: Record<string, ...> = { k: ..., ... }` 里抽出顶层键。"""
    m = re.search(rf"const\s+{const_name}\b[^{{]*\{{(.*?)\n\}}", src, re.S)
    assert m, f"未找到 {const_name}"
    body = m.group(1)
    keys: set[str] = set()
    for line in body.split("\n"):
        km = re.match(r"\s*([a-z_]+)\s*:", line)
        if km:
            keys.add(km.group(1))
    return keys


def _i18n_keys(name: str) -> set[str]:
    src = _read(WEB_SRC / "i18n" / name)
    return set(re.findall(r"'accountType\.([a-z_]+)'", src))


# --------------------------------------------------------------------------- #
# 各注册表                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def accounts_panel() -> str:
    return _read(FEATURES_SRC / "features" / "AccountsPanel.tsx")


@pytest.fixture(scope="module")
def donut() -> str:
    return _read(WEB_SRC / "components" / "dashboard" / "AssetCompositionDonut.tsx")


@pytest.fixture(scope="module")
def top_accounts() -> str:
    return _read(WEB_SRC / "components" / "dashboard" / "HomeTopAccounts.tsx")


def _backend_types() -> set[str]:
    sys_path = str(REPO)
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    from src.mcp.tools.entity_tools import VALID_ACCOUNT_TYPES

    return set(VALID_ACCOUNT_TYPES)


# --------------------------------------------------------------------------- #
# 核心不变式                                                                  #
# --------------------------------------------------------------------------- #


def test_account_type_lists_partition_everything(accounts_panel: str) -> None:
    """可交易 + 估值 == 全集，且两者不相交。

    不相交很关键:`ACCOUNT_ORDER = [...TRADABLE, ...VALUATION]` 拼出来的,
    重复会导致同一类型在账户页出现两次。
    """
    tradable = _array_values(accounts_panel, "TRADABLE_TYPES")
    valuation = _array_values(accounts_panel, "VALUATION_TYPES")
    assert tradable & valuation == set(), f"同时出现在两张表里: {tradable & valuation}"
    all_types = tradable | valuation
    for required in ("cash", "bank_account", "bank_card", "credit_card",
                     "point_card", "receivable"):
        assert required in all_types, f"{required} 没有登记到任何一张表"
    # 与后端 MCP 白名单一致
    assert all_types == _backend_types(), {
        "only_frontend": sorted(all_types - _backend_types()),
        "only_backend": sorted(_backend_types() - all_types),
    }


def test_icon_and_color_tables_cover_every_type(accounts_panel: str) -> None:
    """图标与颜色表必须覆盖全部类型 —— 漏了会静默回退(灰图标 / 灰颜色)。"""
    all_types = _array_values(accounts_panel, "TRADABLE_TYPES") | _array_values(
        accounts_panel, "VALUATION_TYPES")
    for table in ("TYPE_ICON_URL", "TYPE_COLORS"):
        keys = _record_keys(accounts_panel, table)
        missing = all_types - keys
        assert not missing, f"{table} 缺: {sorted(missing)}"


def test_icon_files_exist() -> None:
    """每条 TYPE_ICON_URL 指向的文件必须真的存在，否则是 404 空白图标。"""
    src = _read(FEATURES_SRC / "features" / "AccountsPanel.tsx")
    for rel in re.findall(r"'(/icons/account/[^']+)'", src):
        assert (ICONS_DIR / pathlib.Path(rel).name).exists(), f"图标文件缺失: {rel}"


def test_dashboard_copies_cover_every_type(
    accounts_panel: str, donut: str, top_accounts: str
) -> None:
    """dashboard 的三张表(含 HomeTopAccounts 的**两份独立副本**)也要齐全。

    HomeTopAccounts 的 TYPE_ICON_URL / TYPE_COLORS 是 AccountsPanel 的复制品
    (注释:「无导出引用降低耦合」),所以天生会漂移 —— 这条断言就是它的兜底。

    **例外**:它只覆盖 `EXCLUDE_TYPES` 之外的类型 —— 那是「活跃 Top5 账户」
    控件,估值类(real_estate / vehicle / receivable …)本来就不该出现在
    「日常活跃」名单里。所以这里断言的是 `全类型 - EXCLUDE_TYPES` ⊆ 表。
    """
    all_types = _array_values(accounts_panel, "TRADABLE_TYPES") | _array_values(
        accounts_panel, "VALUATION_TYPES")
    donut_keys = _record_keys(donut, "TYPE_META")
    assert all_types <= donut_keys, (
        f"AssetCompositionDonut.TYPE_META 缺: {sorted(all_types - donut_keys)}"
    )

    m = re.search(r"EXCLUDE_TYPES = new Set\(\[([^\]]*)\]\)", top_accounts)
    assert m, "找不到 HomeTopAccounts.EXCLUDE_TYPES(结构变了请同步更新本测试)"
    excluded = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert excluded <= all_types, f"EXCLUDE_TYPES 里有未注册的类型: {excluded - all_types}"

    shown = all_types - excluded
    top_icons = _record_keys(top_accounts, "TYPE_ICON_URL")
    top_colors = _record_keys(top_accounts, "TYPE_COLORS")
    assert shown <= top_icons, (
        f"HomeTopAccounts.TYPE_ICON_URL 缺: {sorted(shown - top_icons)}"
    )
    assert shown <= top_colors, (
        f"HomeTopAccounts.TYPE_COLORS 缺: {sorted(shown - top_colors)}"
    )
    # 反向:被排除的类型也不该出现在那两份表里(否则 EXCLUDE 形同虚设)
    assert not (excluded & top_icons), f"已排除却仍有图标: {sorted(excluded & top_icons)}"


def test_i18n_has_every_type_in_all_three_languages() -> None:
    """三语必须都有每个类型 —— 缺了 `t()` 会把裸 key `accountType.xxx`
    直接显示在饼图图例上。"""
    backend = _backend_types()
    for name in ("en.ts", "zh-CN.ts", "zh-TW.ts"):
        keys = _i18n_keys(name)
        missing = backend - keys
        assert not missing, f"{name} 缺 accountType: {sorted(missing)}"
        extra = keys - backend
        assert not extra, f"{name} 有多余的 accountType: {sorted(extra)}"


def test_colors_agree_across_tables(accounts_panel: str, donut: str,
                                    top_accounts: str) -> None:
    """同一个类型在三处必须是同一个颜色，否则账户页和饼图对不上。"""
    def flat_colors(src: str, const: str) -> dict[str, str]:
        m = re.search(rf"const\s+{const}\b[^=]*=\s*\{{(.*?)\n\}}", src, re.S)
        assert m, const
        return dict(re.findall(r"([a-z_]+)\s*:\s*'([^']+)'", m.group(1)))

    panel = flat_colors(accounts_panel, "TYPE_COLORS")
    top = flat_colors(top_accounts, "TYPE_COLORS")
    m = re.search(r"const\s+TYPE_META\b[^=]*=\s*\{(.*?)\n\}", donut, re.S)
    assert m, "找不到 AssetCompositionDonut.TYPE_META"
    donut_colors = dict(re.findall(
        r"([a-z_]+):\s*\{\s*color:\s*'([^']+)'", m.group(1)))
    for name, table in (("HomeTopAccounts", top), ("AssetCompositionDonut", donut_colors)):
        shared = set(panel) & set(table)
        for key in shared:
            assert panel[key] == table[key], (
                f"{name} 里 {key} 的颜色 {table[key]} != AccountsPanel 的 {panel[key]}"
            )


# --------------------------------------------------------------------------- #
# 语义不变式                                                                  #
# --------------------------------------------------------------------------- #


def test_receivable_is_asset_not_liability() -> None:
    """应收款是**资产**(别人欠你钱,余额为正),故意不在 is_liab 集合里。

    `read/workspace.py` 里:
        is_liab = {a.sync_id: (a.account_type in ("credit_card", "loan")) ...}
    那是给**负余额**负债用的。应收款余额为正,加进去会把净资产方向算反。
    """
    src = _read(REPO / "src" / "routers" / "read" / "workspace.py")
    m = re.search(r"is_liab\s*=\s*\{[^}]*?account_type in \(([^)]*)\)", src, re.S)
    assert m, "找不到 is_liab 定义(结构变了请同步更新本测试)"
    liability_types = set(re.findall(r"\"([a-z_]+)\"", m.group(1)))
    assert "receivable" not in liability_types, (
        "应收款是资产,加进 is_liab 会把净资产方向算反"
    )
    assert {"credit_card", "loan"} <= liability_types, liability_types


def test_bank_account_has_no_card_last_four() -> None:
    """`bank_account` 有开户行、但**没有卡号后四位**。

    这是它与 `bank_card` 的唯一区别，也是拆 `isBankOrCredit` 成两个能力
    谓词的原因。
    """
    src = _read(FEATURES_SRC / "lib" / "accountTypeCaps.ts")

    def _set(name: str) -> set[str]:
        m = re.search(rf"{name} = new Set\(\[([^\]]*)\]\)", src)
        assert m, f"accountTypeCaps 里找不到 {name}"
        return set(re.findall(r"'([a-z_]+)'", m.group(1)))

    bank_names = _set("BANK_NAME_TYPES")
    card_four = _set("CARD_LAST_FOUR_TYPES")
    assert "bank_account" in bank_names, bank_names
    assert "bank_account" not in card_four, (
        "银行普通存款户口没有卡,不该显示「卡号后四位」"
    )
    assert card_four <= bank_names, "有卡号的类型必然也有开户行"
    assert {"bank_card", "credit_card"} == card_four, card_four


def test_scanner_is_not_vacuous() -> None:
    """防「空集通过」—— 扫描器本身要能真的抓到东西。"""
    panel = _read(FEATURES_SRC / "features" / "AccountsPanel.tsx")
    assert len(_array_values(panel, "TRADABLE_TYPES")) >= 6
    assert len(_array_values(panel, "VALUATION_TYPES")) >= 6
    assert json.loads("[]") == []  # 保持 import 可见
