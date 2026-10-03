"""默认分类种子(本 fork 专有)。

## 为什么需要它

默认分类原本**只存在于 Flutter App**(`lib/services/data/seed_service.dart`),
靠首次启动时本地建好再 sync push 上行。服务端与 Web 端**一个都没有** ——
实测新建账本后 `/read/workspace/categories` 返回 `[]`。

放弃 App、只用 Web/PWA(本 fork 的部署形态)时,新账本会**一个分类都没有**,
连「餐饮」都得自己手建。这不是「少一个税与保险」,是整套默认分类都没了。

## 分类表从哪来

**官方 App 的 `seed_service.dart` + `app_zh.arb` 原样提取**,不是本 fork 自创:

| | 来源 |
|---|---|
| 层级与 key | `hierarchicalExpenseCategories` / `hierarchicalIncomeCategories` |
| 中文名 | `app_zh.arb` 里 `categoryExpense*` / `categoryIncome*`,按 `-` 拆分 |
| 数量 | 支出 127 + 收入 54 = **181 条**(官方 177 + 本 fork 新增 4) |

**唯一的新增**是 `tax_insurance`「税与保险」及其下三个子类(上游 issue #512):
统计时从各分类剥出的消费税汇进「税与保险」,而住民税 / 国民健康保险是直接记在
这个分类下的实际付款 —— 两者在饼图上合成一块。**名字必须与
`config.tax_category_name` 默认值一致**,否则统计切片对不上。

## syncId:与 App 用同一套确定性算法

App 用 `uuid v5`,命名空间 `b3e7c0de-0000-4000-8000-beec00000001`,名字格式
`cat:{kind}:{level}:{key}` —— 任何设备、任何语言 seed 出来的同一个默认分类都
得到同一个 syncId,云端天然只留一份。

本 fork **复刻同一套算法**(见 `deterministic_category_sync_id`)。所以将来即使
再用一次 App,两边 seed 出来的分类 syncId 相同,不会变成两套。

## 播撒规则

**每个用户只播撒一次** —— 他一个分类都没有时才播(`routers/write/ledgers.py`)。
按名字判幂等做不到尊重用户改名:把「餐饮」改叫「吃饭」,下次建账本又会加回来。

想彻底关掉:`SEED_DEFAULT_CATEGORIES=false`。
"""
from __future__ import annotations

import uuid as _uuid
from typing import Any

# 与 Flutter App 的 seed_service.dart 保持一致
_SEED_SYNC_NAMESPACE = _uuid.UUID("b3e7c0de-0000-4000-8000-beec00000001")


def deterministic_category_sync_id(*, kind: str, level: int, key: str) -> str:
    """默认分类的确定性 syncId —— 与 App 的 `deterministicCategorySyncId` 同算法。

    App 侧(uuid 包的 v5 = SHA-1 based):
        uuid.v5('b3e7c0de-0000-4000-8000-beec00000001', 'cat:$kind:$level:$key')

    保持一致的意义:同一个默认分类在 App 与本 fork 里算出的 syncId 相同 →
    两边都 seed 过也只会留一条,而不是「餐饮」出现两次。
    """
    return str(_uuid.uuid5(_SEED_SYNC_NAMESPACE, f"cat:{kind}:{level}:{key}"))


# (key, 中文名, kind, level, parent_key) —— key 与 App 的 seed key 同名
DEFAULT_CATEGORIES: tuple[tuple[str, str, str, int, str | None], ...] = (
    # ---- 支出 ----
    ("dining", "餐饮", "expense", 1, None),
    ("dining_breakfast", "早餐", "expense", 2, 'dining'),
    ("dining_lunch", "午餐", "expense", 2, 'dining'),
    ("dining_dinner", "晚餐", "expense", 2, 'dining'),
    ("dining_meituan", "美团外卖", "expense", 2, 'dining'),
    ("dining_eleme", "饿了么外卖", "expense", 2, 'dining'),
    ("dining_jd", "京东外卖", "expense", 2, 'dining'),
    ("dining_restaurant", "餐厅", "expense", 2, 'dining'),
    ("dining_food", "美食", "expense", 2, 'dining'),
    ("snacks", "零食", "expense", 1, None),
    ("snacks_biscuit", "饼干", "expense", 2, 'snacks'),
    ("snacks_chips", "薯片", "expense", 2, 'snacks'),
    ("snacks_candy", "糖果", "expense", 2, 'snacks'),
    ("snacks_chocolate", "巧克力", "expense", 2, 'snacks'),
    ("snacks_nuts", "坚果", "expense", 2, 'snacks'),
    ("fruit", "水果", "expense", 1, None),
    ("fruit_apple", "苹果", "expense", 2, 'fruit'),
    ("fruit_banana", "香蕉", "expense", 2, 'fruit'),
    ("fruit_orange", "橙子", "expense", 2, 'fruit'),
    ("fruit_grape", "葡萄", "expense", 2, 'fruit'),
    ("fruit_watermelon", "西瓜", "expense", 2, 'fruit'),
    ("fruit_other", "其他水果", "expense", 2, 'fruit'),
    ("beverage", "饮品", "expense", 1, None),
    ("beverage_milk_tea", "奶茶", "expense", 2, 'beverage'),
    ("beverage_coffee", "咖啡", "expense", 2, 'beverage'),
    ("beverage_juice", "果汁", "expense", 2, 'beverage'),
    ("beverage_soda", "汽水", "expense", 2, 'beverage'),
    ("beverage_water", "矿泉水", "expense", 2, 'beverage'),
    ("pastry", "糕点", "expense", 1, None),
    ("pastry_cake", "蛋糕", "expense", 2, 'pastry'),
    ("pastry_bread", "面包", "expense", 2, 'pastry'),
    ("pastry_dessert", "甜点", "expense", 2, 'pastry'),
    ("pastry_biscuit", "曲奇", "expense", 2, 'pastry'),
    ("cooking", "做饭食材", "expense", 1, None),
    ("cooking_vegetable", "蔬菜", "expense", 2, 'cooking'),
    ("cooking_meat", "肉类", "expense", 2, 'cooking'),
    ("cooking_seafood", "水产", "expense", 2, 'cooking'),
    ("cooking_seasoning", "调料", "expense", 2, 'cooking'),
    ("cooking_grain", "粮油", "expense", 2, 'cooking'),
    ("shopping", "购物", "expense", 1, None),
    ("shopping_clothing", "服装", "expense", 2, 'shopping'),
    ("shopping_shoes", "鞋帽", "expense", 2, 'shopping'),
    ("shopping_bag", "包包", "expense", 2, 'shopping'),
    ("shopping_accessory", "配饰", "expense", 2, 'shopping'),
    ("shopping_daily", "日用百货", "expense", 2, 'shopping'),
    ("pets", "宠物", "expense", 1, None),
    ("pets_food", "宠物食品", "expense", 2, 'pets'),
    ("pets_supplies", "宠物用品", "expense", 2, 'pets'),
    ("pets_medical", "宠物医疗", "expense", 2, 'pets'),
    ("pets_grooming", "宠物美容", "expense", 2, 'pets'),
    ("transport", "交通", "expense", 1, None),
    ("transport_subway", "地铁", "expense", 2, 'transport'),
    ("transport_bus", "公交", "expense", 2, 'transport'),
    ("transport_taxi", "出租车", "expense", 2, 'transport'),
    ("transport_ride", "网约车", "expense", 2, 'transport'),
    ("transport_parking", "停车费", "expense", 2, 'transport'),
    ("transport_fuel", "加油", "expense", 2, 'transport'),
    ("car", "汽车", "expense", 1, None),
    ("car_maintenance", "汽车保养", "expense", 2, 'car'),
    ("car_repair", "汽车维修", "expense", 2, 'car'),
    ("car_insurance", "汽车保险", "expense", 2, 'car'),
    ("car_wash", "洗车", "expense", 2, 'car'),
    ("car_fine", "违章罚款", "expense", 2, 'car'),
    ("clothing", "服饰", "expense", 1, None),
    ("clothing_top", "上衣", "expense", 2, 'clothing'),
    ("clothing_pants", "裤子", "expense", 2, 'clothing'),
    ("clothing_skirt", "裙子", "expense", 2, 'clothing'),
    ("clothing_shoes", "鞋子", "expense", 2, 'clothing'),
    ("clothing_accessory", "服饰配件", "expense", 2, 'clothing'),
    ("daily_goods", "日用品", "expense", 1, None),
    ("daily_toiletries", "洗护用品", "expense", 2, 'daily_goods'),
    ("daily_paper", "纸品", "expense", 2, 'daily_goods'),
    ("daily_cleaning", "清洁用品", "expense", 2, 'daily_goods'),
    ("daily_kitchen", "厨房用品", "expense", 2, 'daily_goods'),
    ("education", "教育", "expense", 1, None),
    ("education_tuition", "学费", "expense", 2, 'education'),
    ("education_training", "培训费", "expense", 2, 'education'),
    ("education_books", "书籍", "expense", 2, 'education'),
    ("education_stationery", "文具", "expense", 2, 'education'),
    ("education_office", "办公用品", "expense", 2, 'education'),
    ("invest_loss", "投资亏损", "expense", 1, None),
    ("invest_loss_stock", "股票亏损", "expense", 2, 'invest_loss'),
    ("invest_loss_fund", "基金亏损", "expense", 2, 'invest_loss'),
    ("invest_loss_other", "其他投资亏损", "expense", 2, 'invest_loss'),
    ("entertainment", "娱乐", "expense", 1, None),
    ("entertainment_movie", "电影", "expense", 2, 'entertainment'),
    ("entertainment_ktv", "KTV", "expense", 2, 'entertainment'),
    ("entertainment_amusement", "游乐场", "expense", 2, 'entertainment'),
    ("entertainment_bar", "酒吧", "expense", 2, 'entertainment'),
    ("entertainment_other", "其他娱乐", "expense", 2, 'entertainment'),
    ("game", "游戏", "expense", 1, None),
    ("game_recharge", "游戏充值", "expense", 2, 'game'),
    ("game_equipment", "游戏装备", "expense", 2, 'game'),
    ("game_membership", "游戏会员", "expense", 2, 'game'),
    ("health_products", "保健品", "expense", 1, None),
    ("health_vitamin", "维生素", "expense", 2, 'health_products'),
    ("health_food", "保健食品", "expense", 2, 'health_products'),
    ("health_nutrition", "营养品", "expense", 2, 'health_products'),
    ("subscription", "订阅服务", "expense", 1, None),
    ("subscription_video", "视频会员", "expense", 2, 'subscription'),
    ("subscription_music", "音乐会员", "expense", 2, 'subscription'),
    ("subscription_cloud", "云存储", "expense", 2, 'subscription'),
    ("subscription_other", "其他订阅", "expense", 2, 'subscription'),
    ("sports", "运动", "expense", 1, None),
    ("sports_gym", "健身房", "expense", 2, 'sports'),
    ("sports_equipment", "运动装备", "expense", 2, 'sports'),
    ("sports_course", "运动课程", "expense", 2, 'sports'),
    ("sports_outdoor", "户外活动", "expense", 2, 'sports'),
    ("housing", "住房", "expense", 1, None),
    ("housing_rent", "房租", "expense", 2, 'housing'),
    ("housing_property", "物业费", "expense", 2, 'housing'),
    ("housing_mortgage", "房贷", "expense", 2, 'housing'),
    ("housing_decoration", "装修", "expense", 2, 'housing'),
    ("home", "居家", "expense", 1, None),
    ("home_furniture", "家具", "expense", 2, 'home'),
    ("home_appliance", "家电", "expense", 2, 'home'),
    ("home_decor", "装饰品", "expense", 2, 'home'),
    ("home_bedding", "床上用品", "expense", 2, 'home'),
    ("beauty", "美容", "expense", 1, None),
    ("beauty_skincare", "护肤品", "expense", 2, 'beauty'),
    ("beauty_cosmetics", "化妆品", "expense", 2, 'beauty'),
    ("beauty_salon", "美容美发", "expense", 2, 'beauty'),
    ("beauty_nail", "美甲", "expense", 2, 'beauty'),
    # ---- 收入 ----
    ("salary", "工资", "income", 1, None),
    ("salary_basic", "基本工资", "income", 2, 'salary'),
    ("salary_performance", "绩效奖金", "income", 2, 'salary'),
    ("salary_year_end", "年终奖", "income", 2, 'salary'),
    ("salary_overtime", "加班费", "income", 2, 'salary'),
    ("investment", "理财", "income", 1, None),
    ("investment_fund", "基金收益", "income", 2, 'investment'),
    ("investment_dividend", "股票分红", "income", 2, 'investment'),
    ("investment_product", "理财产品", "income", 2, 'investment'),
    ("investment_other", "其他理财", "income", 2, 'investment'),
    ("red_packet", "红包", "income", 1, None),
    ("red_packet_festival", "节日红包", "income", 2, 'red_packet'),
    ("red_packet_birthday", "生日红包", "income", 2, 'red_packet'),
    ("red_packet_return", "随礼回礼", "income", 2, 'red_packet'),
    ("bonus", "奖金", "income", 1, None),
    ("bonus_year_end", "年度奖金", "income", 2, 'bonus'),
    ("bonus_quarterly", "季度奖", "income", 2, 'bonus'),
    ("bonus_project", "项目奖金", "income", 2, 'bonus'),
    ("bonus_other", "其他奖金", "income", 2, 'bonus'),
    ("reimbursement", "报销", "income", 1, None),
    ("reimbursement_travel", "差旅报销", "income", 2, 'reimbursement'),
    ("reimbursement_meal", "餐费报销", "income", 2, 'reimbursement'),
    ("reimbursement_other", "其他报销", "income", 2, 'reimbursement'),
    ("part_time", "兼职", "income", 1, None),
    ("part_time_income", "兼职收入", "income", 2, 'part_time'),
    ("part_time_extra", "外快", "income", 2, 'part_time'),
    ("gift", "礼金", "income", 1, None),
    ("gift_wedding", "结婚礼金", "income", 2, 'gift'),
    ("gift_birthday", "生日礼金", "income", 2, 'gift'),
    ("gift_other", "其他礼金", "income", 2, 'gift'),
    ("interest", "利息", "income", 1, None),
    ("interest_bank", "银行利息", "income", 2, 'interest'),
    ("interest_other", "其他利息", "income", 2, 'interest'),
    ("refund", "退款", "income", 1, None),
    ("refund_shopping", "购物退款", "income", 2, 'refund'),
    ("refund_service", "服务退款", "income", 2, 'refund'),
    ("refund_other", "其他退款", "income", 2, 'refund'),
    ("invest_income", "投资收益", "income", 1, None),
    ("invest_income_stock", "股票收益", "income", 2, 'invest_income'),
    ("invest_income_fund", "基金投资", "income", 2, 'invest_income'),
    ("invest_income_other", "其他投资收益", "income", 2, 'invest_income'),
    ("second_hand", "二手交易", "income", 1, None),
    ("second_hand_idle", "闲置物品", "income", 2, 'second_hand'),
    ("second_hand_goods", "二手商品", "income", 2, 'second_hand'),
    ("social_benefit", "社会福利", "income", 1, None),
    ("social_benefit_unemployment", "失业保险", "income", 2, 'social_benefit'),
    ("social_benefit_maternity", "生育津贴", "income", 2, 'social_benefit'),
    ("social_benefit_other", "其他补贴", "income", 2, 'social_benefit'),
    ("tax_refund", "退税", "income", 1, None),
    ("tax_refund_personal", "个税退税", "income", 2, 'tax_refund'),
    ("tax_refund_other", "其他退费", "income", 2, 'tax_refund'),
    ("provident_fund", "公积金", "income", 1, None),
    ("provident_fund_withdrawal", "公积金提取", "income", 2, 'provident_fund'),
    ("provident_fund_interest", "公积金利息", "income", 2, 'provident_fund'),
    # ---- 支出 ----
    ("tax_insurance", "税与保险", "expense", 1, None),
    ("tax_consumption", "消费税", "expense", 2, 'tax_insurance'),
    ("tax_income_tax", "所得税", "expense", 2, 'tax_insurance'),
    ("tax_social_insurance", "社会保险", "expense", 2, 'tax_insurance'),
)


def _force_sync_id(
    snapshot: dict[str, Any], sync_id: str, *, kind: str, level: int, key: str
) -> None:
    """把快照里某个分类的 syncId 改写成确定性值(原地)。"""
    want = deterministic_category_sync_id(kind=kind, level=level, key=key)
    for row in snapshot.get("categories") or []:
        if str(row.get("syncId")) == str(sync_id):
            row["syncId"] = want
            return


def _existing_keys(rows_: list[Any]) -> set[tuple[str, str]]:
    """已存在的 `(name, kind)` 集合(大小写不敏感,与 mutator 的判定口径一致)。"""
    out: set[tuple[str, str]] = set()
    for row in rows_:
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

    返回 `(snapshot, created_names)`。已存在的分类原样保留 —— 不覆盖名字 /
    图标 / 排序,用户改过的东西不该被种子改回去。
    """
    from ..snapshot_mutator import create_category, ensure_snapshot_v2

    # 用 dict 持有 snapshot —— 直接在闭包里写
    # `snapshot, sid = create_category(snapshot, payload)`
    # 会让 Python 把 snapshot 当成内层函数的局部变量(赋值即声明),报
    # UnboundLocalError。
    box: dict[str, Any] = {"snap": ensure_snapshot_v2({"items": [], "count": 0})}
    have = _existing_keys(existing_categories)
    created: list[str] = []
    # seed key -> 已建分类的中文名。DEFAULT_CATEGORIES 里父级一定排在子级
    # 前面(按 level 升序),所以这里能直接拿到父名。
    name_of_key: dict[str, str] = {}

    for key, name, kind, level, parent_key in DEFAULT_CATEGORIES:
        name_of_key[key] = name
        if (name.strip().lower(), kind) in have:
            continue
        payload: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "level": level,
            # syncId 与 App 一致 → 两边都 seed 也不会撞车
            "syncId": deterministic_category_sync_id(kind=kind, level=level, key=key),
        }
        if parent_key:
            # create_category 的 mutator 只认 parent_name(名字),父级的解析
            # 发生在 projection.upsert_category —— 它按
            # (user_id, name, kind, level=1) 反查,所以必须给名字且父级 level=1
            payload["parent_name"] = name_of_key[parent_key]
        if actor_user_id:
            payload["updatedByUserId"] = actor_user_id
        box["snap"], sid = create_category(box["snap"], payload)
        # mutator 自己生成 syncId(`_new_sync_id("cat")`),不认 payload 里给的。
        # 这里就地改写成 App 那套确定性值 —— 不去动 mutator,那是所有写入方
        # 共用的路径,让它接受外部 syncId 风险太大;只在本 seeder 的快照上改。
        _force_sync_id(box["snap"], sid, kind=kind, level=level, key=key)
        have.add((name.strip().lower(), kind))
        created.append(name)

    snapshot = box["snap"]
    snapshot["count"] = len(snapshot.get("categories") or [])
    return snapshot, created
