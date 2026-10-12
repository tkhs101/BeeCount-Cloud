"""变异验证脚本 —— 把「顺序」固化成工具,而不是靠记性。

## 为什么需要它

本会话做了 **20+ 次变异验证**,每一次都现写一段临时脚本,而这段脚本
**每一轮都有新的出错方式**:

| 轮 | 出错方式 |
|---|---|
| 多次 | `sed` 模式没匹配上,**变异压根没发生**,却据此判「护栏失效」 |
| 多次 | 跑完忘了还原 |
| 74 | 突变方向搞反(从 import 块删而不是从 `__all__` 删) |
| 126 | 「跑护栏 -> 看颜色」无法区分**护栏失效**和**变异没落地** |

CLAUDE.md「变异审计脚本」那节早就记了这些要点(绝对路径 / finally 还原 /
锚点没匹配要单列),但**记在文档里不等于会照做** —— 20 多次里仍然反复踩。

所以把最关键的一条**做成工具的硬约束**:

> **变异没落地,就不许跑护栏。**

## 两种变异模式

### `--mode replace`(默认)

直接换掉锚点,用于验证「这条护栏抓的是它该抓的那件事」。

### `--mode unreachable` —— **专门抓「源码判据的假护栏」**

把 `if <cond>:` 变成 `if <cond> and False:`。

> ⚠️ **源码文本一字不改,代码却永不可达。**
> 任何「源码里有这句话」的判据**一定不会红**。

**为什么需要它**:本会话连着三轮(151/152/153)在**自己**写的护栏上踩到
同一个坑 —— 它们都断言「源码里有某段文本」,而把那段代码改成不可达之后,
**护栏照样全绿**。实测对照:

| 护栏 | unreachable 变异 |
|---|---|
| `assert "tax <= 0" in inspect.getsource(_normalize_tax_amount)` | **绿**(假) |
| `with pytest.raises(...): _normalize_tax_amount(0.0, ...)` | **红**(真) |

> 📌 **同一个变异,两条判据一真一假** —— 这就是「静态 vs 行为」的可测量差。

⚠️ 用它做变异审计时,**只对行为判据有意义**:静态判据本来就抓不到,
它绿是**预期**,不是「护栏可能失效」。

## 用法

```bash
.venv/Scripts/python.exe scripts/mutation_check.py \
    --file src/projection.py \
    --find "ReadTxProjection, ReadTxSplitProjection," \
    --replace "ReadTxProjection," \
    --test tests/test_truncate_ledger_coverage.py
```

退出码:`0` = 护栏变红(变异被抓到,符合预期);`1` = 护栏仍绿(护栏可能失效);
`2` = **用法/锚点错误**(变异没落地,护栏结果不算)。

📌 **退出码 2 和 1 必须分开** —— 这正是 126 轮踩的坑:
「护栏失效」和「变异没落地」如果都返回同一个结果,我就只能靠猜。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 退出码:锚点没匹配 / 用法错。**与「护栏失效」严格区分**。
EXIT_ANCHOR_MISS = 2
#: 退出码:变异文件与护栏文件不属于同一个测试域(后端 pytest / 前端 vitest)。
EXIT_WRONG_DOMAIN = 3


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _changed_files() -> set[str]:
    out = _git("status", "--porcelain")
    return {line[3:].strip().replace("\\", "/") for line in out.splitlines() if line.strip()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="仓库根相对路径")
    ap.add_argument("--find", required=True, help="要替换的原文本(精确)")
    ap.add_argument(
        "--mode",
        choices=("replace", "unreachable"),
        default="replace",
        help="unreachable:把 `if <cond>:` 变成 `if <cond> and False:` —— "
             "**源码文本一字不改,代码却永不可达**。专门用来抓源码判据的假护栏。",
    )
    ap.add_argument("--replace", required=True, help="替换成的新文本")
    ap.add_argument("--test", required=True, help="要跑的测试路径")
    args = ap.parse_args()

    target = REPO_ROOT / args.file
    if not target.is_file():
        print(f"✗ 文件不存在: {args.file}")
        return EXIT_ANCHOR_MISS

    original = target.read_text(encoding="utf-8")
    n = original.count(args.find)
    if n != 1:
        # ⚠️ **锚点必须恰好命中 1 次** —— 0 次是「变异没发生」,
        #    >1 次说明 find 太短,替换会改到多处。两种都必须拒绝。
        print(f"✗ 锚点在 {args.file} 里出现 **{n} 次**(要求恰好 1 次)")
        print("  → 变异**没有发生**,护栏结果不作数。")
        if n > 1:
            print("  → find 写得太短,会改到多处。请写得更精确。")
        return EXIT_ANCHOR_MISS

    before = _changed_files()
    if args.mode == "unreachable":
        # `if <cond>:` -> `if <cond> and False:`
        # ⚠️ **源码文本一字不改** —— 所以任何「源码里有这句话」的判据都不会红。
        #    这正是 151-153 轮连续三次踩到的失败模式。
        find = args.find
        if not find.rstrip().endswith(":"):
            print(f"✗ --mode unreachable 需要一个以 `:` 结尾的 if 行,收到 {find!r}")
            return EXIT_ANCHOR_MISS
        repl = find.rstrip()[:-1].rstrip() + " and False:"
        args.replace = repl
        find = find
    else:
        find = args.find

    # ⚠️ **锚点可能在死分支里**(178 轮实测踩到的第四种「变异没被检出」)
    # 锚点行若被包在 `if`/`for`/`while`/`try`/`with` 里面,护栏那条测试
    # **不一定进得去** —— 那一段从未执行,变异于是**永远绿**。
    # 它和「护栏失效」在观测上**完全一样**(都绿),但结论相反:
    # 前者要**换锚点**,后者要**补护栏**。
    # 详见 CLAUDE.md「变异没被检出的四种原因」。
    try:
        import ast

        _tree = ast.parse(original)
        _hit_line = original[: original.index(find)].count("\n") + 1
        for _node in ast.walk(_tree):
            if not isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            _fn_end = _node.end_lineno or _node.lineno
            if not (_node.lineno <= _hit_line <= _fn_end):
                continue
            for _sub in ast.walk(_node):
                # ⚠️ `ast.walk` 会走到没有 `end_lineno` 的节点
                # (`arguments` / `alias` / ...),先判再取属性。
                _end = getattr(_sub, "end_lineno", None) or getattr(_sub, "lineno", 0)
                _start = getattr(_sub, "lineno", 0)
                if not (_start < _hit_line <= _end):
                    continue
                # ⚠️ 块头**自己**的那一行(`for x in ...:` / `if cond:`)
                # 是「无条件执行到」的 —— 只有**块体内部**才算死分支。
                if getattr(_sub, "body", None) and _hit_line <= _sub.body[0].lineno:
                    continue
                if isinstance(_sub, (ast.If, ast.For, ast.While, ast.Try, ast.With)):
                    print(
                        f"\n⚠️ **锚点可能落在死分支里** —— 第 {_hit_line} 行被包在"
                        f" `{type(_sub).__name__}`(第 {_sub.lineno} 行)内部。\n"
                        "  → 测试若**没有**进入这个分支,那一段**从未执行**,\n"
                        "    变异会**永远绿** —— 而这与「护栏失效」在观测上**完全一样**。\n"
                        "  → **先确认变异真的执行了**(量一个可观测副作用),\n"
                        "    再谈护栏强不强;或者把锚点挪到**无条件执行**的位置。\n"
                        "  → 这只是**提示**,不是拒绝:测试**确实**可能进这个分支。
    ⚠️ 本检查只到「**某一层**分支」为止,不区分嵌套 —— 所以
    「在分支里」**不等于**「测试进不去」。判据仍然是**变异有没有真的执行**。"
                    )
                    break
            break
    except SyntaxError:
        pass  # 源文件本来就解析不了,交给后面的测试去报

    mutated = original.replace(find, args.replace, 1)
    target.write_text(mutated, encoding="utf-8")

    try:
        # 📌 **顺序在这里被固化**:先证明变异落地,再跑护栏。
        after = _changed_files()
        landed = args.file in (after - before) or mutated != original
        if not landed:
            print("✗ 写入后文件内容没变 —— 变异没落地,护栏结果不作数。")
            return EXIT_ANCHOR_MISS

        diff = _git("diff", "--", args.file)
        if not diff.strip():
            print("✗ git diff 为空 —— 变异没落地(可能被 gitignore 或行尾吞了)。")
            return EXIT_ANCHOR_MISS

        print(f"✓ 变异已落地({args.file},{len(diff.splitlines())} 行 diff)")
        print(f"  {args.find!r}\n  -> {args.replace!r}\n")

        # ⚠️ **门与被变异文件必须属于同一个测试域**,否则「护栏仍绿」是**必然**的,
        # 不是「护栏可能失效」。
        #
        # 实测过一次:变异改的是 `frontend/apps/web/src/i18n/en.ts`(vitest 护栏
        # `i18nKeyCoverage.test.ts`),而我传的 `--test` 是**后端 pytest 文件** ——
        # 那个测试压根读不到那个文件,当然全绿。
        #
        # 📌 **这与「探针的否定结果不能当证据」是同一族**:「护栏没反应」
        # 可能意味着护栏坏了,也可能意味着**你根本没测到它**。
        # 两者必须分开,否则就会写出「护栏失效」这个**假结论**。
        mut_is_fe = args.file.startswith("frontend/")
        test_is_fe = "frontend" in args.test.replace("\\", "/")
        if mut_is_fe != test_is_fe:
            print("⚠️ **门与变异文件不在同一个测试域** ——")
            if mut_is_fe:
                print("   变异在前端(要 `pnpm -C apps/web test`,vitest 护栏),")
                print("   而 `--test` 是 pytest 文件 —— 那个护栏**读不到**这个文件,")
                print("   「全绿」是必然的,不代表护栏有效。")
            else:
                print("   变异在后端(pytest),而 `--test` 指向前端 vitest 文件。")
            return EXIT_WRONG_DOMAIN

        proc = subprocess.run(
            [sys.executable, "-m", "pytest", args.test, "-p", "no:warnings", "-q"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        out = proc.stdout + proc.stderr
        tail = [ln for ln in out.splitlines() if ln.strip()][-3:]
        print("\n".join(tail))

        if proc.returncode == 0:
            print("\n⚠️ 护栏仍绿 —— **护栏可能失效**(或该变异确实不在它职责范围内)。")
            return 1
        print("\n✓ 护栏变红 —— 变异被抓到。")
        return 0
    finally:
        target.write_text(original, encoding="utf-8")
        restored = target.read_text(encoding="utf-8") == original
        print(f"\n{'✓ 已还原' if restored else '✗ **还原失败,需手动 git checkout**'}")


if __name__ == "__main__":
    raise SystemExit(main())