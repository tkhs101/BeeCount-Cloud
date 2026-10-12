"""用标准库 `trace` 量「某个模块**实际**有没有被跑到」。

## 为什么需要这个工具

206 轮实测:「模块名不出现在任何测试文件的文本里」这个判据**完全不成立**。

枚举 `src/**.py` 时 10 个模块在 `tests/` 里**连名字都没有**(静态文本搜索),
看起来是「零覆盖」。用 `trace` 真跑 3 个端到端测试文件后:

    src/ledger_access.py                    67 行  ✅
    src/routers/write/exchange_rate_overrides.py  58 行  ✅
    src/services/backup/rclone_config.py    42 行  ✅
    ... 共 9 个,全部被执行过,零覆盖数 = 0

端到端测试经 `src/main.py` 的 import 图**间接**跑到它们
(中间件、缓存、备份辅助都在请求路径上)。

> 📌 **静态文本搜索 ≠ 动态覆盖。** 这和 CLAUDE.md 里
> 「grep 搜不到 ≠ 不存在」是同一族,但**更深一层**:
> 不只是「搜不到可能是名字被映射了」,而是
> **「调用方根本没提到被调用方的名字」也照样会执行到它**。
>
> ⚠️ 而且这里最危险的方向是**反向**的:我拿「零引用」当**肯定**证据
> (「这个模块没人管」),实际它**有覆盖**。**探针的否定结果不能当证据** ——
> 否定结果只能告诉你「这个判据不够」,不能告诉你「对象有问题」。

## 为什么不装 coverage

没装,而且不该为了查一件事装一个新依赖。标准库 `trace` 够用,
代价是**慢**(约 10-50 倍开销)—— 所以这个脚本只适合**定点查少数模块**,
不适合做全仓覆盖率报告。

## ⚠️⚠️ 207 轮的实测:**这个工具在本项目上不可信,数字不能当覆盖率**

拿它跑 `tests/test_smoke_real_app.py`,`src/routers/auth.py` 记到 **0 行**。
但**同一次运行**的日志里有(205 轮探针实证):

    INFO  src.routers.auth:auth.py:208 auth.register user=...

也就是**这个测试确实走了鉴权模块**,`trace` 却一行没记到。

排除过的解释(都**不成立**):

| 假设 | 实测 |
|---|---|
| async 路由跟不到 | **否** —— `auth.py` 是 9 sync / 0 async;而全 sync 的 `workspace.py` 记到 156 行 |
| 线程池 / `settrace` 不跨线程 | **否** —— 同上,全 sync 的 `read/workspace.py` 记到 156 行 |
| `ignoredirs` 把源码排除了 | **否** —— `sys.prefix` 是 `.venv`,cwd 不在它下面 |
| 路径匹配把行归错了模块 | 已修(`zip` 那处);重跑数字不变 |

所以**漏记的机制我没查清**。在查清之前:

> ⚠️ **这里的数字只是「`trace` 记到的行数」,不是覆盖率。**
> 206 轮我拿它下的结论(「9 个零引用模块其实都被覆盖」)
> **已撤回** —— 那可能整份都是假阳性。

📌 **真正该记住的不是这个工具,而是流程:**

> **用一个新工具下结论之前,先拿一个「已知答案」的样例验证它本身。**
> 我有已知答案(`auth.py` 必然被执行),却直接用它去回答
> 「别的模块有没有被执行」—— **工具没验证过就拿它当证据**。

要真的测覆盖率,请装 `coverage`(有 C 扩展,速度差一个量级,
而且它会正确处理线程池);`trace` 适合「这个文件有没有被 import 过」这类
粗问题,**不适合**「哪些行被执行了」。

## 用法

    python scripts/measure_module_coverage.py \
        src/ledger_access.py src/snapshot_cache.py \
        tests/test_smoke_real_app.py

第一个及之后的位置参数是**要量的模块**;最后一个是**测试文件**。
`--all <目录>` 量该目录下全部 `.py`。
"""

from __future__ import annotations

import sys
import trace
from pathlib import Path

DEFAULT_TESTS = [
    "tests/test_smoke_real_app.py",
    "tests/test_tx_split_invariants.py",
    "tests/test_account_balance_paths.py",
]

# 只数目标模块自己的行 —— 标准库和 site-packages 不在关心范围内,
# 但仍然让 trace 跳过它们,否则计数表会大到没有意义。
SKIP_DIRS = [sys.prefix, sys.exec_prefix]


def measure(modules: list[str], test_files: list[str],
             top: int | None = None) -> int:
    import pytest  # 延迟 import:让 `--help` 不必装 pytest

    repo = Path.cwd().resolve()
    # ⚠️ 这里**不能**写 `zip(modules, sorted(norm))` —— 那是**两套互不相关的
    # 顺序**,一旦 `modules` 不是字母序,行号就会被归到**错误的模块**上。
    #
    # 第一版就是这么写的。这次**没有出事**——因为调用时 `modules` 恰好是
    # 字母序,两套顺序碰巧一致(连跑两次的数字完全相同,21/46 对得上)。
    # **所以这是潜在 bug,不是已发生的 bug** —— 我一度在注释里写成
    # 「两个模块的数字互相对调了」,那是**我没看错就已经下了结论**。
    # 真的对调了的话,两次运行的数字会不一致,而它们是一致的。
    abs_by_module = {m: str((repo / m).resolve()).replace("\\", "/")
                     for m in modules}

    tracer = trace.Trace(count=1, trace=0, ignoredirs=SKIP_DIRS)
    code = tracer.runfunc(pytest.main, ["-p", "no:warnings", "-q", *test_files])

    hit: dict[str, set[int]] = {m: set() for m in modules}
    for (fname, lineno), _count in tracer.results().counts.items():
        key = str(fname).replace("\\", "/")
        for m, abs_path in abs_by_module.items():
            if key == abs_path:
                hit[m].add(lineno)

    total_lines = {m: len(p.read_text(encoding="utf-8",
                                       errors="replace").splitlines())
                   for m, p in ((m, repo / m) for m in modules)}

    rows = []
    for m in modules:
        n, tot = len(hit[m]), total_lines[m]
        ratio = (n / tot) if tot else 0.0
        rows.append((ratio, n, tot, m))

    # 全量模式下按**覆盖率**升序 —— 找的是「覆盖不足」,不是「哪些被执行了」。
    # 定点模式下按输入顺序,回答的是「这个模块到底跑没跑」。
    if top is not None:
        rows.sort(key=lambda r: r[0])
        shown, hidden = rows[:top], rows[top:]
    else:
        shown, hidden = rows, []

    print(f"\npytest exit={code}  跑了 {len(test_files)} 个文件\n")
    print(f"{'模块':<48}{'执行':>7}{'总行':>7}{'覆盖':>8}   判定")
    for ratio, n, tot, m in shown:
        mark = "❌ 完全没跑" if n == 0 else ("⚠️ 覆盖不足" if ratio < 0.5 else "✅")
        print(f"{m:<48}{n:>7}{tot:>7}{ratio * 100:>7.0f}%   {mark}")

    dead = [m for _r, n, _t, m in rows if n == 0]
    print(f"\n完全没被执行: {len(dead)}/{len(modules)}")
    for m in dead:
        print("   -", m)
    if hidden:
        thin = [m for _r, n, _t, m in hidden if n * 2 < 1]
        print(f"(另 {len(hidden)} 行未显示;其中 {len(thin)} 个覆盖低于 50%)")
    return code


def main(argv: list[str]) -> int:
    # ⚠️ **必须自己处理 console 编码。** 本脚本的输出全是中文 + ✅/❌,
    # 而中文 Windows 的默认 stdout 编码是 **GBK** —— 直接 `print(__doc__)`
    # 会 `UnicodeEncodeError` 崩掉。**不能要求调用方先设
    # `PYTHONIOENCODING=utf-8`** 来用你写的脚本(我自己就踩过好几次这个坑)。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):      # 老 Python / 已关闭的流
            pass

    args = argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    # `--all src` = 量那个目录下**全部** .py。手工列 120 个模块不现实,
    # 而「列出候选」正是这个工具存在的意义。
    all_dir = None
    if args and args[0] == "--all":
        all_dir = args[1]
        args = args[2:]
    tests = DEFAULT_TESTS
    if "--" in args:
        cut = args.index("--")
        tests = args[cut + 1:] or DEFAULT_TESTS
        args = args[:cut]
    if all_dir:
        # ⚠️ `root` 必须是**绝对**路径 —— 用相对的 `src` 时 rglob 返回
        # `src\\bootstrap.py`,`relative_to(cwd())` 直接抛 ValueError。
        # 这个 bug 只在 `--all` 这条路径上出现,所以定点调用一直没发现 ——
        # **「只测了一条路径」的又一次**。
        base = Path.cwd().resolve()
        root = Path(all_dir).resolve()
        args = sorted(p.relative_to(base).as_posix()
                      for p in root.rglob("*.py")
                      if "__pycache__" not in p.parts)
    return measure(list(args), tests, top=(25 if all_dir else None))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))