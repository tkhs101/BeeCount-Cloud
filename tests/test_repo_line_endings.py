"""仓库里**不允许**出现 CRLF 行尾 —— 在 **pytest session 结束时**统一检查。

## 为什么必须是 session 结束,而不是一个普通测试

210 轮的第一版护栏写在某个测试文件里,判据是「跑之前 == 跑之后」。
**变异后它没红** —— 因为污染发生在**更早的测试**里,轮到它时文件**已经脏了**,
before/after 都脏 -> 相等。

> 📌 **判据不能依赖「跑之前的状态是干净的」** —— 那正是被测对象
> 可能已经破坏掉的东西。要在**全部测试跑完之后**再看。

`pytest_sessionfinish` 是唯一能保证「看得到整场 session 之后状态」的位置。

## 为什么这值得一条护栏

210 轮实测:`scripts/mutation_check.py` 的
`read_text` + `write_text` 在 Windows 上**读一次写一次就把 LF 变回 CRLF**。

**三重隐蔽**,所以任何单点检查都看不见:

| # | 它骗过了什么 |
|---|---|
| 1 | 工具 **exit 0**,输出还打印「已还原」 |
| 2 | **`git diff` 为空** —— `.gitattributes` 的 `eol=lf` 让 git 规范化比较 |
| 3 | **内容逐字节相同**,只有行尾变了,没有任何 diff 可看 |

## 判据:任何**受跟踪文本**文件含 CRLF = 违规

209 轮加了 `.gitattributes`(`* text=auto eol=lf`)并统一了全仓,
所以**仓库规范就是全 LF** —— 任何 CRLF 都是异常,不需要白名单。

⚠️ **不扫工作区目录**,只扫 `git ls-files` 的结果:
`node_modules` / `.venv` / `dist` 里有第三方文件,扫它们只会造假警报。

⚠️ **git 不可用时跳过而不是失败** —— 那台机器可能不是 git checkout,
行尾规范无从谈起,不该让测试红掉。

---

## ⚠️ 为什么报错信息是英文而注释是中文(211 轮实测)

中文 Windows 的 console 编码是 **GBK**,pytest 输出中文断言信息 ->
**乱码**,连「哪个文件」都读不出来。

⚠️ 我先试了在 `tests/conftest.py` 里 `reconfigure(encoding="utf-8")`
—— **无效**:pytest 在导入 conftest **之前**就捕获并包装了 stdout。
仓库里没有能修好这一层的办法(只能靠 `PYTHONIOENCODING` 环境变量,
而**不该要求调用方去设**)。

所以这里取务实做法:

| 位置 | 用什么 | 原因 |
|---|---|---|
| 注释 / docstring | 中文 | 存在**文件里**,不经过 console |
| **报错与诊断输出** | **ASCII** | 会**打印到 console**,必须扛得住 GBK |

⚠️ 这就是 210 轮那条教训的延伸:**工具要能在别人的机器上直接跑** ——
只是这次「别人的机器」是 **console 编码**,不是行尾。
"""
from __future__ import annotations

import subprocess

CRLF = bytes([13, 10])


def _crlf_files() -> tuple[list[str], str]:
    """返回 (含 CRLF 的受跟踪文本文件, 诊断信息)。git 不可用时返回 ([], 原因)。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--eol"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"git unavailable ({type(exc).__name__}); skipping EOL check"

    bad: list[str] = []
    for line in out.splitlines():
        # 形如: i/lf    w/crlf  attr/text=auto eol=lf \t<path>
        # 带 `-text` / `binary` 的是二进制,git 不对它做行尾转换,跳过。
        if not line.strip() or "-text" in line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        eol_field, path = parts[0], parts[-1].strip()
        if " w/crlf" in eol_field or " w/mixed" in eol_field:
            bad.append(path)
    return bad, ""


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001, ARG001
    bad, reason = _crlf_files()
    if reason:
        print(f"\n[eol guard] {reason}")
        return
    if not bad:
        return

    tr = session.config.pluginmanager.getplugin("terminalreporter")
    if tr is not None:
        tr.write_line("")
        tr.write_line("[EOL GUARD] tracked files were rewritten with CRLF:",
                      red=True)
        for p in bad[:20]:
            tr.write_line(f"     {p}", red=True)
        if len(bad) > 20:
            tr.write_line(f"     ... and {len(bad) - 20} more", red=True)
        tr.write_line(
            "   Common cause: Python text-mode I/O without newline= "
            "(read_text does universal newlines; write_text converts "
            "back to os.linesep).", red=True)
        tr.write_line(
            "   NOTE: git diff does NOT see this - .gitattributes "
            "normalizes EOL when comparing.", red=True)
    session.exitstatus = 1


def test_repo_has_no_crlf_files() -> None:
    """单跑时也能查(而不是只有 session 结束才查)。"""
    bad, reason = _crlf_files()
    if reason:
        print(f"\n[eol guard] {reason}")
        return
    assert not bad, (
        "tracked files with CRLF/mixed line endings:\n  "
        + "\n  ".join(bad[:20])
        + (f"\n  ... and {len(bad) - 20} more" if len(bad) > 20 else "")
    )