"""生成一份演示数据 —— 本地看 Web 端时用。

空账本看不出任何东西:饼图是空的、趋势图是平的、预算进度没有对照。
这个脚本造 6 个月的日常记账数据,覆盖本 fork 的重点功能:

- **消费税** —— 一部分带 tax_amount 的支出,用来验证「税与保险」扇区
- **多分类** —— 超过饼图 8 扇区上限,能验证 Top-N + 税额钉住
- **收入 / 账户 / 标签 / 预算** —— 让资产页、标签页、预算卡都有内容
- **环比同比** —— 逐月金额不同,`compare_periods` 才问得出问题

用法:
    python scripts/seed-demo.py                       # 默认 http://127.0.0.1:8869
    BC_BASE_URL=https://你的域名 python scripts/seed-demo.py

幂等:账号已存在就直接复用(按 email 查),不会重复灌。
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta, timezone

import httpx

BASE = os.environ.get("BC_BASE_URL", "http://127.0.0.1:8869").rstrip("/")
API = "/api/v1"          # 见 config.Settings.api_prefix
EMAIL = os.environ.get("BC_EMAIL", "demo@beecount.local")
PASSWORD = os.environ.get("BC_PASSWORD", "demo123456")

rng = random.Random(20261003)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def login_or_register(c: httpx.Client) -> str:
    try:
        r = c.post("/api/v1/auth/login", json={
            "email": EMAIL, "password": PASSWORD,
            "client_type": "web", "device_name": "seed", "platform": "seed",
        })
        if r.status_code == 200:
            return r.json()["access_token"]
    except Exception:
        pass
    r = c.post("/api/v1/auth/register", json={
        "email": EMAIL, "password": PASSWORD,
        "client_type": "web", "device_name": "seed", "platform": "seed",
    })
    if r.status_code != 200:
        sys.exit(f"注册失败 {r.status_code}: {r.text[:300]}")
    return r.json()["access_token"]


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=60)
    token = login_or_register(c)
    H = {"Authorization": f"Bearer {token}", "X-Device-ID": "seed",
         "Content-Type": "application/json"}
    JH = {"Authorization": f"Bearer {token}", "X-Device-ID": "seed"}

    r = c.get(API + "/read/ledgers", headers=JH)
    ledgers = r.json() if r.status_code == 200 else []
    if ledgers:
        lid = ledgers[0].get("ledger_id") or ledgers[0].get("id")
        print(f"账本已存在: {lid}")
    else:
        r = c.post(API + "/write/ledgers", headers=H, json={
            "ledger_id": "daily", "ledger_name": "日常", "currency": "JPY"})
        if r.status_code != 200:
            sys.exit(f"建账本失败 {r.status_code}: {r.text[:300]}")
        lid = "daily"
        cats = c.get(API + "/read/workspace/categories", headers=JH).json()
        print(f"建账本完成 —— 自动播撒默认分类 {len(cats)} 个")

    existing = c.get(API + "/read/workspace/transactions",
                     headers=JH, params={"limit": 200}).json().get("items") or []
    if existing:
        print(f"已有 {len(existing)} 笔交易，跳过灌数据")
        return

    # ── 账户 / 标签 / 预算 ──────────────────────────────────────────────
    for acc in (
        {"name": "现金", "account_type": "cash", "currency": "JPY",
         "initial_balance": 300000.0},
        {"name": "招行储蓄卡", "account_type": "bank_card", "currency": "JPY",
         "initial_balance": 850000.0, "bank_name": "招商银行",
         "card_last_four": "6621"},
        {"name": "招行信用卡", "account_type": "credit_card", "currency": "JPY",
         "credit_limit": 500000.0, "billing_day": 5, "payment_due_day": 25},
    ):
        c.post(API + f"/write/ledgers/{lid}/accounts", headers=H,
               json={"base_change_id": 0, **acc})
    for tag in ("日常", "工作", "医疗", "人情", "大额"):
        c.post(API + f"/write/ledgers/{lid}/tags", headers=H,
               json={"base_change_id": 0, "name": tag})
    for bud in (
        {"amount": 60000.0, "budget_type": "total"},
        {"amount": 25000.0, "budget_type": "category", "category": "餐饮"},
    ):
        c.post(API + f"/write/ledgers/{lid}/budgets", headers=H,
               json={"base_change_id": 0, "period": "monthly", **bud})

    # ── 6 个月的日常支出 ────────────────────────────────────────────────
    # (分类, 备注, 金额区间, 含税概率, 标签)
    EXPENSES = [
        ("餐饮", None, (600, 2500), 0.70, ["日常"]),
        ("餐饮", None, (280, 700), 0.55, ["日常"]),          # 早餐/午餐
        ("公共交通", None, (180, 480), 0.35, ["日常"]),
        ("购物", None, (800, 12000), 0.75, ["日常"]),
        ("娱乐", None, (900, 6000), 0.60, ["日常"]),
        ("水电燃气", None, (6000, 16000), 0.80, ["日常"]),
        ("餐饮", None, (3000, 9000), 0.65, ["人情"]),
        ("医疗", None, (1000, 12000), 0.50, ["医疗"]),
        ("教育", None, (2000, 20000), 0.70, ["工作"]),
        ("服饰", None, (4000, 25000), 0.80, ["大额"]),
    ]
    SALARY = 385000.0

    today = datetime(2026, 10, 3, tzinfo=timezone.utc)
    made = 0
    for months_back in range(6, -1, -1):
        year = today.year
        month = today.month - months_back
        while month <= 0:
            month += 12
            year -= 1
        last_day = (datetime(year + (month == 12), (month % 12) + 1, 1,
                             tzinfo=timezone.utc) - timedelta(days=1)).day
        days_in = last_day if months_back else today.day
        month_scale = 0.86 + months_back * 0.05   # 逐月略增 → 环比能看出涨

        # 每月 25 号发工资。但**当前这个还没过完的月份**要改成当月 1 号,
        # 否则 10/3 打开只看到「本月收入 ¥0」,储蓄率一栏是空的,看着像坏了。
        payday = 25 if months_back else 1
        if days_in >= payday:
            c.post(API + f"/write/ledgers/{lid}/transactions", headers=H, json={
                "base_change_id": 0, "tx_type": "income",
                "amount": SALARY * (1.0 if months_back else 1.0),
                "happened_at": _iso(datetime(year, month, payday, 10, 0,
                                             tzinfo=timezone.utc)),
                "category_name": "工资", "category_kind": "income",
                "account_name": "招行储蓄卡", "note": "月薪",
            })
            made += 1

        for _ in range(rng.randint(26, 34)):
            day = rng.randint(1, days_in)
            cat, _fixed, (lo, hi), tax_rate, tags = rng.choice(EXPENSES)
            amount = round(rng.uniform(lo, hi) * month_scale / 10) * 10
            # 10 月 8% 消费税、其余 10%(日本 2019-11 起)
            tax_rate_pct = 0.08 if (year, month) >= (2026, 10) else 0.10
            tax = round(amount * tax_rate_pct / (1 + tax_rate_pct))
            payload = {
                "base_change_id": 0, "tx_type": "expense", "amount": amount,
                "happened_at": _iso(datetime(year, month, day,
                                             rng.randint(8, 23),
                                             rng.randint(0, 59),
                                             tzinfo=timezone.utc)),
                "category_name": cat, "category_kind": "expense",
                "account_name": rng.choice(["招行储蓄卡", "招行信用卡",
                                              "招行储蓄卡", "招行信用卡", "现金"]),
                "tags": tags,
            }
            if cat == "餐饮" and not tags:
                payload["category_name"] = rng.choice(["餐饮", "早餐", "午餐", "晚餐"])
            if rng.random() < tax_rate:
                payload["tax_amount"] = tax
            # 备注写商家 —— 这样 MCP 的 get_spending_breakdown(by="merchant") 有东西可聚合
            if cat == "餐饮":
                payload["note"] = rng.choice(
                    ["星巴克", "罗森", "麦当劳", "居酒屋", "松屋", "FamilyMart"])
            r = c.post(API + f"/write/ledgers/{lid}/transactions", headers=H, json=payload)
            if r.status_code == 200:
                made += 1

        # 每月房租
        c.post(API + f"/write/ledgers/{lid}/transactions", headers=H, json={
            "base_change_id": 0, "tx_type": "expense", "amount": 118000.0,
            "happened_at": _iso(datetime(year, month, 3, 12, 0,
                                         tzinfo=timezone.utc)),
            "category_name": "房租", "category_kind": "expense",
            "account_name": "招行储蓄卡", "tags": ["日常"],
            "note": "10月房租" if months_back == 0 else "房租",
        })
        made += 1

    # 一笔实际的居民税 / 医保 —— 记在「税与保险」下,验证那个扇区不只有剥出来的税
    c.post(API + f"/write/ledgers/{lid}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 64800.0,
        "happened_at": _iso(datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)),
        "category_name": "税与保险", "category_kind": "expense",
        "account_name": "招行储蓄卡", "tags": ["大额"], "note": "住民税 6期",
    })
    c.post(API + f"/write/ledgers/{lid}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 12800.0,
        "happened_at": _iso(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)),
        "category_name": "税与保险", "category_kind": "expense",
        "account_name": "招行储蓄卡", "note": "国民健康保险",
    })
    made += 2

    a = c.get(API + "/read/workspace/analytics", headers=JH,
              params={"scope": "all", "metric": "expense"}).json()
    ranks = a.get("category_ranks") or []
    print(f"\n灌入 {made} 笔交易")
    print(f"支出分类切片 {len(ranks)} 个（饼图上限 8 + 税与保险钉住）:")
    for x in ranks[:10]:
        print(f"   {x['category_name']:<10} {x['total']:>12,.0f}")
    print(f"\n打开 {BASE} 登录 {EMAIL} / {PASSWORD}")


if __name__ == "__main__":
    main()
