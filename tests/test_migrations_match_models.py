"""212 轮:模型与 alembic 迁移链**必须一致** —— 这是部署前才会炸的那类 bug。

## 为什么需要这条

**模型加了列、迁移没加** 时:

- 本地全绿 —— 测试用 `Base.metadata.create_all()`,**根本不走迁移链**
- 部署后服务直接起不来(`no such column`),或者第一次查询才炸

CLAUDE.md 里写的「迁移链本身的正确性**有专门的迁移测试**」——
**212 轮实测:并不存在这样的测试**(`grep -rln alembic tests/` 命中的
只是**提到**它的文件)。所以这条现在才补上。

## 为什么必须用独立子进程

`alembic/env.py:12` 是:

```python
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)
```

两个后果,都踩过:

1. **`cfg.set_main_option(...)` 对这个仓库无效** —— env.py 拿到 url 后
   **硬覆盖**掉它。所以「设一个 url 就跑迁移到临时库」是假的,
   212 轮第一版探针就是这样**把迁移跑到了真实的 `beecount.db` 上**
   (幸而它本就在 head,`upgrade head` 是 no-op,没造成破坏)。
2. `get_settings()` 是**带缓存的**,同进程里改环境变量**不生效**。

所以用 `subprocess` 起一个**全新进程**:环境变量干净、缓存干净。

## 为什么用相对路径的 sqlite url

CLAUDE.md 记过:Windows 临时路径经 SQLAlchemy 编码成 `C%3A/...`,
而 `alembic/env.py` 走 `configparser`,它把 `%` 当**插值** -> ValueError。
用仓根下的**相对路径**(不含 `%`)绕开。

📌 副作用要自己收拾:这个测试会在仓根建一个临时 `.db`,所以必须清理。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect

from src.models import Base

REPO = pathlib.Path(__file__).resolve().parent.parent

# ⚠️ 第一版在**仓根**建临时库,结果**清不掉**:Windows 上 engine 的连接
# 一直持有 -> `PermissionError (WinError 32)`,而我的 `_clean()` 又把那个
# 异常静默吞了 -> 「失败不许被吞」当场演示。
#
# 现在把 cwd 设成 pytest 的 `tmp_path`,库文件落在那里,由 pytest 自己清理。
# ⚠️ URL 用**相对路径**:Windows 绝对路径含 `%`(configparser 会当插值炸),
# 见下面 `script_location` 的处理。
RUNNER = """
import sys
sys.path.insert(0, {repo!r})
from alembic import command
from alembic.config import Config
cfg = Config({ini!r})
cfg.set_main_option("script_location", {scripts!r})
command.upgrade(cfg, "head")
print("UPGRADE_OK")
"""


@pytest.fixture(scope="module")
def migrated_schema(tmp_path_factory) -> dict[str, set[str]]:
    """从**空库**跑完整条迁移链,返回实际建出来的 {表: 列}。"""
    work = tmp_path_factory.mktemp("mig")
    runner = RUNNER.format(repo=str(REPO), ini=str(REPO / "alembic.ini"),
                            scripts=str(REPO / "alembic").replace("\\", "/"))
    env = {**os.environ, "DATABASE_URL": "sqlite:///check.db",
           "PYTHONPATH": str(REPO)}
    proc = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=work, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    assert "UPGRADE_OK" in proc.stdout, (
        "alembic upgrade head 失败:\n"
        f"stdout:\n{proc.stdout[-1500:]}\n"
        f"stderr:\n{proc.stderr[-1500:]}"
    )

    engine = create_engine(f"sqlite:///{work / 'check.db'}")
    try:
        insp = inspect(engine)
        return {
            t: {c["name"] for c in insp.get_columns(t)}
            for t in insp.get_table_names()
            if not t.startswith("alembic")
        }
    finally:
        # ⚠️ 必须 dispose —— 不关连接的话 Windows 删不掉 tmp 文件,
        # 而「删不掉」又会被静默吞掉,于是每次跑测试留一个垃圾库。
        engine.dispose()


def test_every_model_table_is_created_by_migrations(migrated_schema) -> None:
    want = {t.name for t in Base.metadata.tables.values()}
    missing = sorted(want - set(migrated_schema))
    assert not missing, (
        "tables in models but NOT created by the migration chain:\n  "
        + "\n  ".join(missing)
    )


def test_every_model_column_is_created_by_migrations(migrated_schema) -> None:
    """🔴 核心:模型加了列而迁移没加 —— **本地测试永远发现不了**。

    本地测试走 `Base.metadata.create_all()`,它**照抄模型**;
    只有部署时才走迁移链。所以这个不一致能一路活到生产。
    """
    want = {t.name: {c.name for c in t.columns}
            for t in Base.metadata.tables.values()}
    problems: list[str] = []
    for table, cols in sorted(want.items()):
        actual = migrated_schema.get(table)
        if actual is None:
            continue                      # 上面那条已经报了
        miss = sorted(cols - actual)
        if miss:
            problems.append(f"{table}: 缺列 {miss}")
    assert not problems, (
        "columns in models but NOT created by the migration chain.\n"
        "Local tests stay green because they use Base.metadata.create_all()\n"
        "(never the migration chain); only a deployed DB hits "
        "'no such column'.\n  " + "\n  ".join(problems)
    )


def test_migrations_do_not_create_unknown_tables(migrated_schema) -> None:
    """反向:迁移建了模型里没有的表 —— 多半是忘了更新模型或删了迁移。"""
    want = {t.name for t in Base.metadata.tables.values()}
    extra = sorted(set(migrated_schema) - want)
    assert not extra, f"迁移建了模型里没有的表: {extra}"