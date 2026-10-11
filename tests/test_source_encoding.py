"""源码里**静默的编码损坏**护栏。

## 它防的是什么(一条真实发生过的)

用 bash heredoc 写前端 zh-CN 词典时,「今天」被静默毁成两个 U+FFFD。
那次损坏**所有门都绿**:

| 门 | 结果 | 为什么 |
|---|---|---|
| `tsc -b` | 绿 | U+FFFD 是合法的字符串字面量 |
| parity 测试 | 绿 | 键**存在**,只是内容坏了 |
| vitest(除专门那条外) | 绿 | 没人在断言中文文案内容 |
| `pytest` | 绿 | 后端根本没碰这个文件 |

**只有中文用户会看到乱码。** 当时是手工数出来的 —— 手工意味着下次还会漏。

## 为什么不只做前端的版本

前端已有 `apps/web/src/i18nEncoding.test.ts` 查同样的东西。这里查**后端**,
因为后端的中文常量风险更持久:

**`src/services/default_categories.py` 的 181 条分类名**是程序化提取后
逐字写死在源码里的。而种子规则是「**每个用户只播撒一次**」(他一个分类都没有时)
—— 一旦某条分类名在源码里就是坏的,那个用户会**永久**看到一个乱码分类,
而且**没有任何迁移能修**(他自己的账本已经有了,不会再播撒)。

其它中文常量(`src/config.py` 的「税与保险」等)同理:
它们落在**税额归类**的判据上,坏了会让税额落到错误的分类而无人察觉。

## 为什么默认红

CLAUDE.md 记着:注释和文档**不强制任何东西**,而这类损坏 100% 是人写出来的
(heredoc / 复制粘贴 / 编码转换 / 批量替换)。要拦住它得有每次提交都跑的门。

同理「**自己先红**」:如果下面某个 glob 写错、什么文件都没匹配到,
「什么都没检查」不能等于通过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
TESTS = Path(__file__).resolve().parent

#: U+FFFD —— 解码失败 / 字节被截断时留下的痕迹。
REPLACEMENT_CHAR = "�"

#: 允许出现的控制字符(换行/制表/回车);其余一律可疑。
_ALLOWED_CTRL = {"\n", "\t", "\r"}

#: **故意的**例外:本文件自己定义的那个常量就是 U+FFFD。
#: 白名单**要带上限** —— 防止有人往里塞真损坏来「让测试绿」。
#: (CLAUDE.md 记的那条:白名单默认该拒绝,而且必须有上限。)
_ALLOWED_FFFD_FILES = {"test_source_encoding.py"}
_MAX_ALLOWED_FFFD = 2

#: ⚠️ **数量下限**:glob 写错 / 目录搬走时,「一个文件都没匹配到」
#: 绝不能等于「检查通过」。这是 CLAUDE.md 反复记的那类恒真。
_MIN_FILES = 20


def _python_sources() -> list[Path]:
    """`src/` 与 `tests/` **都要扫**。

    ⚠️ 第一版只扫 `src/`,于是 `tests/` 里已有的 5 处损坏完全没被发现
    —— 而那里的损坏同样在**注释和 docstring** 里解释着护栏的契约。
    **范围画窄的护栏,保护不到自己脚下。**
    """
    return sorted(
        p
        for p in list(SRC.rglob("*.py")) + list(TESTS.glob("*.py"))
        if "__pycache__" not in p.parts
    )


def _control_chars(text: str) -> set[str]:
    return {c for c in text if ord(c) < 32 and c not in _ALLOWED_CTRL}


def _bad_lines(text: str, needle: str) -> list[int]:
    """含 `needle` 的**行号**。报行号,不然等于没告诉别人去哪儿改。"""
    return [i for i, line in enumerate(text.splitlines(), 1) if needle in line]


def test_found_enough_source_files() -> None:
    """下限自检:匹配不到东西时**必须自己先红**。"""
    assert len(_python_sources()) >= _MIN_FILES, (
        f"只匹配到 {len(_python_sources())} 个 .py(下限 {_MIN_FILES})"
        " —— glob 写错了,后面的检查会形同虚设"
    )


@pytest.mark.parametrize("path", _python_sources(), ids=lambda p: p.name)
def test_source_has_no_replacement_char(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    bad = _bad_lines(text, REPLACEMENT_CHAR)

    if path.name in _ALLOWED_FFFD_FILES:
        # 白名单**带上限**:「本文件就该有 1 个」不等于「有 5 个也行」。
        # 否则有人往这个文件里塞真损坏,测试照样绿。
        assert len(bad) <= _MAX_ALLOWED_FFFD, (
            f"{path.name} 是白名单文件,只允许 {len(bad)} 处故意的 U+FFFD,"
            f"实际 {len(bad)} 行命中 —— 多出来的是真损坏"
        )
        return

    assert not bad, f"{path.name} 第 {bad} 行含 U+FFFD —— 写入时字节被截断/编码错了"


@pytest.mark.parametrize("path", _python_sources(), ids=lambda p: p.name)
def test_source_has_no_control_chars(path: Path) -> None:
    ctrl = _control_chars(path.read_text(encoding="utf-8"))
    assert not ctrl, f"{path.name} 含控制字符 {sorted(map(hex, ctrl))}"


def test_default_categories_are_clean() -> None:
    """**181 条分类名**逐条查 —— 它们一旦坏,用户那边是永久的。

    布局来自模块自己的类型标注(别凭记忆猜):
    `tuple[str, str, str, int, str | None]` = (key, 名, kind, level, parent)
    """
    from src.services.default_categories import DEFAULT_CATEGORIES

    assert len(DEFAULT_CATEGORIES) == 181, "条数变了,分类种子的口径也变了"
    bad = [c for c in DEFAULT_CATEGORIES if REPLACEMENT_CHAR in str(c)]
    assert not bad, f"含 U+FFFD 的分类: {bad}"
    assert not any(_control_chars(str(c)) for c in DEFAULT_CATEGORIES)


def test_default_categories_names_are_unique_and_nonempty() -> None:
    """名称重复 / 为空,都会让播撒出的分类表出现没法显示的行。"""
    from src.services.default_categories import DEFAULT_CATEGORIES

    names = [c[1] for c in DEFAULT_CATEGORIES]  # noqa: E741  (索引 1 = 名)
    assert len(names) == len(set(names)), "有重复的分类名"
    assert all(n and n.strip() for n in names), "有空的分类名"


def test_config_tax_category_name_is_clean() -> None:
    """「税与保险」是**税额归类的判据** —— 坏了会让税额落到错误分类。"""
    from src.config import get_settings

    name = get_settings().tax_category_name
    assert REPLACEMENT_CHAR not in name
    assert not _control_chars(name)