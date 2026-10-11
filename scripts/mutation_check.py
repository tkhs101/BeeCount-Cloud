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
    mutated = original.replace(args.find, args.replace, 1)
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