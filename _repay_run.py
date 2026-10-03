import httpx
from datetime import date
B = "http://127.0.0.1:8871/api/v1"
H = {"X-Device-ID": "smoke2", "Content-Type": "application/json"}
c = httpx.Client(base_url=B, timeout=30)
r = c.post("/auth/login", headers=H, json={
    "email": "smoke2@t.com", "password": "SmokeTest1!x",
    "client_type": "web", "device_name": "s", "platform": "web"})
H["Authorization"] = f"Bearer {r.json()['access_token']}"

accs = c.get("/read/workspace/accounts", headers=H, params={"ledger_id":"smlg"}).json()
card = next(a for a in accs if a["name"]=="招行信用卡")
before = {a["name"]: a["balance"] for a in accs}
print("还款前:", before)

# 今天不是还款日(10/4,还款日 25 号)→ 应被跳过且不扣钱
r = c.post(f"/write/ledgers/smlg/accounts/{card['id']}/autorepay/run", headers=H)
print("非还款日手动触发 HTTP:", r.status_code)
after = {a["name"]: a["balance"] for a in
         c.get("/read/workspace/accounts", headers=H, params={"ledger_id":"smlg"}).json()}
print("非还款日触发后:", after)
print("  余额未变(正确):", before == after)

# 查交易列表:应只有那一笔 8000 消费,没有自动还款
txs = c.get("/read/ledgers/smlg/transactions", headers=H).json()
print("交易数:", len(txs), "| 类型:", [t["tx_type"] for t in txs])
print("  无自动还款交易(正确):", not any(
    (t.get("note") or "").startswith("自动还款") for t in txs))
