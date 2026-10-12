"""217 轮:analytics 响应必须把 `tax_category_name` **原样**下发。

## 背景

`TAX_CATEGORY_NAME` env 决定税额归到哪个分类名。**后端三处**都用同一个
settings 值(读端归集、MCP、默认分类播种),是自洽的。

但**前端是硬编码**的 —— `apps/web/src/lib/taxCategory.ts` 写死
`TAX_CATEGORY_NAME = '税与保险'`。改了 env 之后:

- 后端把税额归到新名字 ✅
- 前端**找不到那个分类** -> 税额从扇区**掉进「其他」** ❌
- (统计数字本身仍然正确,因为后端用的是同一个值)

所以响应里加一个 `tax_category_name`,前端就不必再硬编码第二份。

## 为什么这条测试要**改 env** 再断言

⚠️ 如果只用默认值断言「返回 '税与保险'」,那条断言**恒真** ——
后端哪怕写死一个字面量它也过。而**恒真的断言比没有护栏更糟**。

## 🔴 `get_settings` 是 **`@lru_cache`** —— 这条测试第一版就栽在这

`src/config.py:165` 是 `@lru_cache`,所以 `get_settings()` **全局只算一次**。
而且全仓有 **8 处模块级 `settings = get_settings()`**
(`src/main.py`、`src/database.py`、`src/security.py`、三个 `_shared.py`…),
它们在 **import 时**就把当时的环境**冻结**了。

于是第一版测试:`monkeypatch.setenv` 之后直接断言 -> **恒定拿到默认值**,
因为缓存早被 `import src.main` 填好了。

| | 第一版 | 正确做法 |
|---|---|---|
| 改 env | ✅ | ✅ |
| 清缓存 | ❌ **忘了** | `get_settings.cache_clear()` |
| 还原后 | —— | **再 clear 一次**,否则脏值留给别的测试 |

> 📌 我 217 轮先断言「`get_settings()` **没有缓存**,`return Settings()` 每次
> 新建」—— **错的**。我只 grep 了函数体,**没看它上面那行装饰器**,
> 而 `src/main.py:63` 的 `get_settings.cache_clear()` 早就在告诉我答案。
> **这是本会话第三次「把观察当结论」。**

⚠️ 「grep 到 `return Settings()`」与「每次调用都新建」**不是一回事** ——
缓存不在函数体里。

## 同一个坑的另一种形态(诊断时踩到)

写诊断脚本时我特意**没 import `src.main`**,于是 `get_settings()` 读到 env 值,
我据此断言「没有缓存」—— **那个观察是对的,结论是错的**:
它成立仅仅因为**缓存还没被谁填过**。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import get_settings
from src.database import Base, get_db
from src.main import app


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override():
        db = TS()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    return TestClient(app)


def test_analytics_does_not_hardcode_the_tax_category(monkeypatch) -> None:
    monkeypatch.setenv("TAX_CATEGORY_NAME", "自定义税金科目")
    get_settings.cache_clear()          # 🔴 关键:见文件头
    try:
        client = _client()
        r = client.post("/api/v1/auth/register", json={
            "email": "tax217@t.com", "password": "TaxCat1!x",
            "client_type": "web", "device_name": "d", "platform": "test"})
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]

        body = client.get("/api/v1/read/workspace/analytics",
                          headers={"Authorization": f"Bearer {token}"},
                          params={"scope": "all", "metric": "expense"}).json()

        got = body.get("tax_category_name")
        assert got == "自定义税金科目", (
            f"响应没有跟着 env 走(得到 {got!r})—— 说明它是硬编码的,"
            f"用户改了配置前端仍然对不上。")
    finally:
        # 🔴 env 恢复了,但**缓存里还留着那个自定义值**。不清的话,
        #    后面每一个测试都会拿到「自定义税金科目」。
        get_settings.cache_clear()
        app.dependency_overrides.pop(get_db, None)