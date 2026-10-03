"""默认分类种子(本 fork 专有)。

## 为什么需要它

默认分类原本**只存在于 Flutter App**(`lib/services/data/seed_service.dart`),
靠首次启动时本地建好再 sync push 上行。服务端与 Web 端**一个都没有** ——
实测新建账本后 `/read/workspace/categories` 返回 `[]`。

于是:一旦放弃 App(本 fork 的部署形态就是 Web/PWA),新账本**一个分类都没有**,
连「餐饮」都得自己手建。这是部署形态改变带来的真实缺口,不只是「少一个税与保险」。

## 与上游 issue #512 的关系

上游 #512 讨论的是「默认分类表里补一组税与保险」。本 fork 直接实现了它,并把范围
扩到一整套基础分类 —— 因为缺的不只是税。

## 与 App 的兼容性

- **幂等**:按 `(name, kind)` 跳过已存在的分类,重复调用安全。
- App 首次启动仍会推它自己那套,同名分类会变成两条 —— 对**只用 Web** 的部署无影响;
  若将来要同时用 App,应关掉这个种子(`SEED_DEFAULT_CATEGORIES=false`)让 App  seeding。
- 想彻底关掉:`SEED_DEFAULT_CATEGORIES=false`(见 `config.py`)。

## 分类表本身

这是一张**可直接编辑的常量表**,不是「上游标准答案」—— 它按日常记账的通用分类
搭骨架,带二级分类的地方才带(不需要的地方就不加,保持精简)。用户随时可以在
Web 的分类页增删改,不影响这里。
"""
from __future__ import annotations

from typing import Any

# (一级分类名, kind, (二级分类名, ...))
DEFAULT_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # ── 支出 ──
    ("餐饮", "expense", ("外食", "食材", "咖啡")),
    ("交通", "expense", ("公共交通", "打车", "汽油", "停车")),
    ("住房", "expense", ("房租", "水电燃气", "通信费", "宽带")),
    ("购物", "expense", ("日用品", "服饰", "电器", "家具")),
    ("医疗", "expense", ("门诊", "药品", "住院")),
    ("娱乐", "expense", ("运动", "旅行", "订阅")),
    ("教育", "expense", ("书籍", "课程", "培训")),
    ("人情", "expense", ()),
    ("其他支出", "expense", ()),
    # ── 税与保险 ──
    # 这一组是本 fork 的核心:统计时消费税会从各分类剥出汇进「税与保险」
    # (`routers/read/_shared.tax_in_base_currency`),而住民税 / 国民健康保险
    # 是直接记在这个一级分类下的实际付款 —— 两者在饼图上合成一块。
    # 名字必须与 `config.tax_category_name` 的默认值一致,否则统计切片对不上。
    ("税与保险", "expense", ("消费税", "所得税", "社会保险")),
    # ── 收入 ──
    ("工资", "income", ("基本工资", "奖金", "补贴")),
    ("副业", "income", ()),
    ("投资收益", "income", ()),
    ("其他收入", "income", ()),
)


def _existing_keys(rows: list[Any]) -> set[tuple[str, str]]:
    """已存在的 `(name, kind)` 集合(大小写不敏感,与 mutator 的判定口径一致)。"""
    out: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip().lower()
        kind = str(row.get("kind", "expense")).strip()
        if name:
            out.add((name, kind))
    return out


def build_default_snapshot(
    existing_categories: list[Any],
    *,
    actor_user_id: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """把缺失的默认分类叠加到一个 snapshot 上。

    返回 `(snapshot, created_names)`。`created_names` 只含真正新建的,
    已存在的分类原样保留(不覆盖名字 / 图标 / 排序 —— 用户改过的东西不该被种子
    改回去)。
    """
    from ..snapshot_mutator import create_category, ensure_snapshot_v2

    # 用 dict 持有 snapshot —— 直接在闭包里 `snapshot, sync_id = create_category(...)`
    # 会让 Python 把 snapshot 当成 _add 的局部变量(赋值即声明),报
    # UnboundLocalError。
    box: dict[str, Any] = {"snap": ensure_snapshot_v2({"items": [], "count": 0})}
    have = _existing_keys(existing_categories)
    created: list[str] = []

    def _add(name: str, kind: str, parent_name: str | None) -> str | None:
        key = (name.strip().lower(), kind)
        if key in have:
            return None
        payload: dict[str, Any] = {"name": name, "kind": kind}
        # 父级按**名字**传,并把 level 标成 1/2 —— 与 MCP `create_category`
        # (`write_tools.create_category`)完全一致的口径。
        #
        # 为什么不是 parent_sync_id:`snapshot_mutator.create_category` 只写
        # `parentName`,不写 `parentSyncId`;父子关系的解析发生在
        # `projection.upsert_category` —— 它优先读 parentSyncId,没有就按
        # (user_id, parent_name, kind, level=1) 去库里反查。所以父级必须
        # 先落库,且 level=1,子级的反查才命中。
        payload["level"] = 2 if parent_name else 1
        if parent_name:
            payload["parent_name"] = parent_name
        if actor_user_id:
            payload["updatedByUserId"] = actor_user_id
        box["snap"], sync_id = create_category(box["snap"], payload)
        have.add(key)
        created.append(name)
        return sync_id

    for top_name, kind, children in DEFAULT_CATEGORIES:
        _add(top_name, kind, None)
        for child in children:
            # 父级已存在时也要挂对 —— 用名字而不是新建返回的 sync_id
            _add(child, kind, top_name)

    snapshot = box["snap"]
    snapshot["count"] = len(snapshot.get("categories") or [])
    return snapshot, created
