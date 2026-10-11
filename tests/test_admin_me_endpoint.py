"""`/admin/me` —— 显式的「我是不是管理员」契约。

## 这个端点替代了什么

前端要显示「后台管理」入口,此前只能**探测**:调一个只有管理员能过的端点。

- 探测 `/admin/health` → **错**:它只要 `ops:write` scope,
  普通用户(有写权限)也返回 200 → 任何能记账的人都被当成管理员
- 探测 `/admin/overview` → 能用,但那是**借来的**判据:
  哪天 overview 给 viewer 开放只读,探测就悄悄失效,而且不报错

## 契约

- 管理员 → 200 + `{is_admin: true, user_id}`
- 非管理员 → **403**,不是 200 + `is_admin: false`

为什么是 403 而不是 200:「你是不是管理员」这个答案,对非管理员来说
不该由服务端算一次再传回来。直接不给过,和后台其它端点同一个语义 ——
前端把 403 理解成「不是管理员」就够了,不需要解析 body。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.models import User


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override():
        db = TS()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _engine_of(client: TestClient):
    """从 fixture 注入的 `get_db` override 里拿回 engine。

    fixture 用的是 `sqlite://` 内存库 + StaticPool(连接全共享),
    所以拿到 override 闭包里的 sessionmaker 就能看到同一份数据。
    """
    gen = app.dependency_overrides[get_db]
    # 生成器还没被消费过时,frame 里拿不到;先消费一次再断
    db = next(gen())
    eng = db.get_bind()
    db.close()
    return eng


def _register(c: TestClient, email: str) -> dict[str, str]:
    r = c.post("/api/v1/auth/register", json={
        "email": email, "password": "AdminMeTest1!x", "client_type": "web",
        "device_name": "d", "platform": "test",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}",
            "X-Device-ID": "d", "Content-Type": "application/json"}


def _make_admin(TS, email: str) -> None:
    with TS() as db:
        u = db.scalar(select(User).where(User.email == email))
        u.is_admin = True
        db.commit()


def test_non_admin_gets_403_not_200_false(client) -> None:
    """非管理员 → **403**,不是 200 + `is_admin: false`。

    200 + false 会让前端多一条要解析的分支,而且「服务端告诉用户他不是
    管理员」这件事本身没有意义 —— 直接不给过就行。
    """
    H = _register(client, "admin-me-no@t.com")
    r = client.get("/api/v1/admin/me", headers=H)
    assert r.status_code == 403, (
        f"非管理员应得 403,实得 {r.status_code}:{r.text[:200]}")
    # 而且 body 不该包含 is_admin: false —— 那会诱导前端去解析它
    assert "is_admin" not in (r.text or ""), r.text[:200]


def test_unauthenticated_gets_401(client) -> None:
    r = client.get("/api/v1/admin/me")
    assert r.status_code == 401, r.text


def test_endpoint_requires_admin_not_just_scope(client) -> None:
    """只有 scope 不够 —— 这正是它替代 `/admin/health` 的理由。

    `/admin/health` 只要 `ops:write`,普通用户也 200。如果把本端点也
    加上 scope 依赖,「有 scope 但不是管理员」的人又会拿到 200,
    这个端点就白加了。
    """
    import inspect

    from src.deps import require_admin_user
    from src.routers.admin import admin_me

    src = inspect.getsource(admin_me)
    assert "require_admin_user" in src, (
        "本端点必须依赖 require_admin_user —— 少了它,有写权限的人"
        "都会被当成管理员")
    assert "require_scopes" not in src, (
        "本端点**不该**再加 scope 依赖 —— 那会重新引入它要消灭的歧义")
    # 依赖的确实是你以为的那个函数
    sig = inspect.signature(admin_me)
    assert any(p.default.dependency is require_admin_user
               for p in sig.parameters.values()
               if hasattr(p.default, "dependency"))

def test_real_admin_passes_with_user_id(client, monkeypatch) -> None:
    """真管理员 → 200 + is_admin: true + user_id。

    上一条只测了「非管理员被拒」—— 那不够:一个**永远 403** 的端点
    也能全绿。必须有一条证明管理员真能过,且 body 有前端要的字段。
    """
    TS = sessionmaker(bind=_engine_of(client))
    H = _register(client, "admin-me-real@t.com")

    # 直接把刚注册的用户提成管理员
    with TS() as db:
        u = db.scalar(select(User).where(User.email == "admin-me-real@t.com"))
        assert u is not None
        u.is_admin = True
        db.commit()

    r = client.get("/api/v1/admin/me", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_admin"] is True, body
    # user_id 是前端要拿去当 key / 展示的 —— 必须是真 id,不能是占位
    assert body["user_id"], body
    with TS() as db:
        u = db.scalar(select(User).where(User.email == "admin-me-real@t.com"))
        assert body["user_id"] == str(u.id), body
