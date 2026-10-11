"""配置字段名 -> 环境变量名的**隐式契约**。

## 为什么值得测

`src/main.py:223` 读的是 `settings.web_static_dir`,但没有一行代码提到
`WEB_STATIC_DIR`。这个映射是 **Pydantic Settings 做的**:没有设
`env_prefix`,所以字段名 `web_static_dir` 按约定映射成大写环境变量
`WEB_STATIC_DIR`。

`SELFHOST-RUNBOOK.md` 让部署者设 `WEB_STATIC_DIR` 指向 `dist`,
`CLAUDE.md` 也把它列成「源码安装最容易漏的一行」—— 但

    grep -rn "WEB_STATIC_DIR" src/     # **什么都搜不到**

于是我这一轮差点把这条**正确**的文档当成过时说法删掉,理由是
「代码从不读它」。**结论看起来很有说服力,而它是错的。**

这条测试把映射钉住:有人改字段名、或给 Settings 加了 `env_prefix`,
这里立刻红。
"""
from __future__ import annotations

import src.config as config_mod
from src.config import Settings


def test_web_static_dir_maps_from_uppercase_env(monkeypatch) -> None:
    """`WEB_STATIC_DIR` 必须真的能让 `settings.web_static_dir` 变。

    ⚠️ 别用 `/tmp/...` 之类的 POSIX 路径 —— Git Bash 会把它翻译成
    Windows 临时路径,断言得跟着写死平台,换个 shell 就红。
    用一个和平台无关的相对路径。
    """
    monkeypatch.setenv("WEB_STATIC_DIR", "some-relative-dist-dir")
    cfg = Settings()
    assert cfg.web_static_dir == "some-relative-dist-dir", (
        f"环境变量没生效:{cfg.web_static_dir!r} —— "
        f"部署照 SELFHOST-RUNBOOK.md 设了它,Web 面板会 404")


def test_web_static_dir_default_is_the_docker_path() -> None:
    """默认值是 Docker 的 `/app/static` —— 所以源码安装**必须**改它。

    ⚠️ **不能直接 `Settings()` 然后断言默认值** —— 别的测试文件用
    `os.environ[...]` 设过 env 且**没在异常时还原**(比如
    `test_write_no_strict_base.py:174` 的 `STRICT_BASE_CHANGE_ID`),
    全量跑时本进程环境已经不干净了。

    我第一版就是这么写的,单跑绿、全量 4 条红 —— 又一次
    「单跑绿、全量红」,而根因**不在我这条测试**,在别人的泄漏。

    改成**读字段的默认值声明**(`Settings.model_fields[...]`),
    它与环境状态无关。
    """
    field = Settings.model_fields["web_static_dir"]
    assert field.default == "/app/static", (
        f"默认值变了:{field.default!r} —— "
        f"CLAUDE.md / SELFHOST-RUNBOOK.md 里「源码安装必须改这一行」"
        f"的说明要一起复核")


def test_config_module_exposes_a_usable_entrypoint() -> None:
    """`src.config` 能被 import —— main.py 依赖它拿 `web_static_dir`。"""
    assert hasattr(config_mod, "Settings")
    assert hasattr(config_mod, "get_settings")
