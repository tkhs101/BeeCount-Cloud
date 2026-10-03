"""验证「到期日真的还款」。把还款日设成今天。"""
import httpx
import os, sys
sys.path.insert(0, ".")
from src.services.backup.scheduler import _resolve_scheduler_tz
from datetime import datetime, timezone
B = "http://127.0.0.1:8879/api/v1"
H = {"X-Device-ID": "dbg", "Content-Type": "application/json"}
c = httpx.Client(base_url=B, timeout=30)
r = c.post("/auth/login", headers=H, json={
    "email": "dbg@t.com", "password": "DbgTest123!x",
    "client_type": "web", "device_name": "d", "platform": "web"})
H["Authorization"] = f"Bearer {r.json()['access_token']}"

# 与服务端同一套时区解析 —— 冒烟脚本最容易在这里和实现分叉
TODAY = datetime.now(_resolve_scheduler_tz()).date()
day = TODAY.day

accs = c.get("/read/workspace/accounts", headers=H, params={"ledger_id":"lg2"}).json()
card = next(a for a in accs if a["name"]=="卡")
# 补一个储蓄卡
if not any(a["name"]=="储" for a in accs):
    c.post("/write/ledgers/lg2/accounts", headers=H, json={
        "base_change_id":0,"name":"储","account_type":"bank_card",
        "initial_balance":50000.0})
    accs = c.get("/read/workspace/accounts", headers=H, params={"ledger_id":"lg2"}).json()
save = next(a for a in accs if a["name"]=="储")

print(f"今天 = {TODAY} (第 {day} 号)")
print("1. 把卡改成「账单日=今天、还款日=今天」+ 绑自动还款")
r = c.patch(f"/write/ledgers/lg2/accounts/{card['id']}", headers=H, json={
    "base_change_id":0,"billing_day":day,"payment_due_day":day,
    "autorepay_enabled":True,"autorepay_from_account_id":save["id"]})
print("   HTTP", r.status_code, r.text[:100] if r.status_code!=200 else "")

print("2. 记一笔消费")
c.post("/write/ledgers/lg2/transactions", headers=H, json={
    "base_change_id":0,"tx_type":"expense","amount":3000.0,
    "happened_at":f"{TODAY.isoformat()}T10:00:00+00:00",
    "account_id":card["id"],"category_name":"餐饮","category_kind":"expense"})

accs = c.get("/read/workspace/accounts", headers=H, params={"ledger_id":"lg2"}).json()
before = {a["name"]: a["balance"] for a in accs}
print("   还款前:", {k: before[k] for k in ("卡","储")})

print("3. 手动触发(= 调度器今天会做的事)")
r = c.post(f"/write/ledgers/lg2/accounts/{card['id']}/autorepay/run", headers=H)
print("   HTTP", r.status_code, r.text[:250] if r.status_code!=200 else "")

accs = c.get("/read/workspace/accounts", headers=H, params={"ledger_id":"lg2"}).json()
after = {a["name"]: a["balance"] for a in accs}
print("   还款后:", {k: after[k] for k in ("卡","储")})
print("   卡欠款清零:", after["卡"] == 0.0)
print("   储蓄卡扣了 3000:", before["储"] - after["储"] == 3000.0)

txs = c.get("/read/ledgers/lg2/transactions", headers=H).json()
auto = [t for t in txs if (t.get("note") or "").startswith("自动还款")]
print(f"4. 交易列表 {len(txs)} 笔,其中自动还款 {len(auto)} 笔")
for t in auto:
    print(f"   {t['tx_type']} {t['amount']} {t.get('from_account_name')} → {t.get('to_account_name')} 「{t.get('note')}」")

print("5. 再触发一次 —— 必须不重复扣")
r = c.post(f"/write/ledgers/lg2/accounts/{card['id']}/autorepay/run", headers=H)
accs = c.get("/read/workspace/accounts", headers=H, params={"ledger_id":"lg2"}).json()
after2 = {a["name"]: a["balance"] for a in accs}
print("   储蓄卡余额未再变:", after == after2)
