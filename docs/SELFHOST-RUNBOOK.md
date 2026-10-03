# 自托管部署与验收手册

照着做即可。完整设计依据见
[plans/2026-10-03-selfhost-tax-feature-fork.md](aegis/plans/2026-10-03-selfhost-tax-feature-fork.md)。

> **不用 Docker**（源码安装）。

---

## 0. 前置

| 项 | 要求 |
|---|---|
| 服务器 | VPS，Python 3.12+、Node 20+、一个域名 |
| 数据库 | SQLite（默认，够用）。数据全在 `data/` 一个目录 |
| 备份 | **做完第 1 步就配**，别等出事 |

---

## 1. 装

```bash
apt install -y python3.12 python3.12-venv nodejs npm   # Node 20+
git clone <你的 fork> /opt/beecount && cd /opt/beecount
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**构建前端**（后端会伺服它）：

```bash
cd frontend
corepack enable
pnpm install --no-frozen-lockfile
pnpm -C apps/web build          # 产物在 frontend/apps/web/dist
cd ..
```

**配置** `/opt/beecount/.env`：

```bash
cat > .env <<EOF
JWT_SECRET=<32+ 字节随机串>          # openssl rand -hex 32
DATABASE_URL=sqlite:////opt/beecount/data/beecount.db
DATA_DIR=/opt/beecount/data
ATTACHMENT_STORAGE_DIR=/opt/beecount/data/attachments
BACKUP_STORAGE_DIR=/opt/beecount/data/backups

# ⚠️ 这行最容易漏。默认是 /app/static（Docker 路径），不改 Web 面板直接 404
WEB_STATIC_DIR=/opt/beecount/frontend/apps/web/dist

REGISTRATION_ENABLED=true          # 仅首次建号用，建完改回 false
EOF
```

> **生成密钥**：`openssl rand -hex 32`。不写也能跑（首启会生成到
> `data/.jwt_secret`），但显式写出来你自己知道是什么。

## 2. 迁移并启动

```bash
.venv/bin/alembic upgrade head     # 应停在 0020_tx_tax_amount
.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8869
```

**systemd**（`/etc/systemd/system/beecount.service`）：

```ini
[Unit]
Description=BeeCount Cloud
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/beecount
EnvironmentFile=/opt/beecount/.env
ExecStart=/opt/beecount/.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8869
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now beecount
systemctl status beecount
```

## 3. 反代 + HTTPS

MCP 用 Bearer token 传，**必须 HTTPS**。

Caddy（`/etc/caddy/Caddyfile`）：

```
your-domain.com {
    reverse_proxy 127.0.0.1:8869
}
```

```bash
systemctl reload caddy
```

## 4. 建第一个账号

浏览器打开 `https://your-domain.com` → 注册（此刻 `REGISTRATION_ENABLED=true`）。

**注册完立刻把 `.env` 里的 `REGISTRATION_ENABLED` 改回 `false` 并重启**：

```bash
sed -i 's/REGISTRATION_ENABLED=true/REGISTRATION_ENABLED=false/' .env
systemctl restart beecount
```

## 5. 建分类 —— **不需要了**

建账本时会自动播撒一套默认分类（44 个，含「税与保险」及其下的
消费税 / 所得税 / 社会保险）。新建账本后直接就能记账。

默认分类表在 `src/services/default_categories.py`，是一张可直接编辑的常量表，
按需增删改后重启即可。想完全关掉：`.env` 里加 `SEED_DEFAULT_CATEGORIES=false`。

> 规则是**每个用户只播撒一次**（他一个分类都没有时才播）。之后你怎么增删改分类
> 都不会被干预。

**仍然要做的一件事**：把 MCP 指到你的实例

```bash
claude mcp add --transport http beecount https://your-domain.com/api/v1/mcp \
  --header "Authorization: Bearer bcmcp_你的token"
```

Token 在 Web 控制台 → 设置 → 开发者 → 新建，勾 `mcp:read` + `mcp:write`，
有效期选「永不」。**明文只显示一次。**

本 fork 的 MCP 有 **36 个 tool**（官方 18 个）。补的 18 个按用途分三块：

| 类别 | 工具 |
|---|---|
| **实体管理**（官方只读/半残） | `create/update/delete_account`、`create/update/delete_tag`、`update/delete_category`、`delete_budget` |
| **分析**（官方完全答不了） | `get_account_balance`、`compare_periods`（环比/同比）、`get_spending_breakdown`（商户/标签/账户维度）、`get_spending_pattern`（星期/时段/金额分布） |
| **批量与导出** | `delete_transactions_batch`、`export_transactions_csv` |
| 消费税 / 附件（issue #510 / #513） | 各工具的 `tax_amount`、`attach_receipt`、`create_transaction_with_receipt`、`create_budget` |

所有删除类工具都是**两阶段确认**（先返 `confirmation_required`，确认后再调）。

## 6. 记一笔含税的，验收

```
create_transaction(
  amount=3280, tx_type="expense",
  category="餐饮", tax_amount=298,
  note="KING BEAR NOW", happened_at="2026-10-03T20:00:00+00:00"
)
```

**期望结果**：

| 看哪里 | 应该是 |
|---|---|
| Web 交易详情 | 税前 2,982 · 消费税 298 · 实付 3,280 |
| 首页饼图 | 「餐饮」切片 2,982（**税前**）；「税与保险」独立成一块 298 |
| 本月支出总额 | 3,280（**实付**，没被拆散） |
| `get_analytics_summary` | `expense=3280`、`tax_total=298` |

> 饼图上限是 8 个扇区，消费税金额最小、**固定被钉住显示**，不会被并进「其他」。

再测一次超界输入，确认报的是 400 而不是「内部错误」：

```
create_transaction(amount=100, category="餐饮", tax_amount=999)
# 期望: tax_amount must be less than amount
```

## 6b. 组合支付与新账户类型

**新建账本后自动有 181 个分类**（含「税与保险」），不用手动建任何分类。

三种新账户类型可在「资产 → 新建账户」里选：

| 类型 | 用途 |
|---|---|
| **银行账户** | 只用来记余额 / 振込，没有卡（填开户行，不填卡号） |
| **积分卡** | 1 积分 = 1 日元；返积分手动记一笔收入 |
| **应收款** | 别人欠你的钱（余额为正，算资产） |

组合支付：新建交易时「组合支付」区块加 ≥2 行，各填账户和金额。

**期望结果**：

- 两个账户余额**各减自己那份**（招行卡 -3000、现金 -2000）
- 分类支出**只算一次 5000**（不是 5000+5000）
- 编辑这笔交易只改备注 → 拆分**不丢**
- 删被拆分引用的账户 → **被拒绝**（不是静默删掉）

**故意不做**：跨币种拆分、转账拆分、收入拆分。

## 6c. 信用卡自动还款

点开信用卡 → 详情弹窗 → 底部「组合支付」那一段下方 = 自动还款开关。

**前置**:这张卡必须填了**账单日**和**还款日**（新建信用卡时的灰盒子里）。
没填就没有绑定入口 —— 少一个锚点就没有「什么时候还」的语义。

**绑定**：关状态下选一个**同币种**的扣款账户（隐藏账户也在候选里，会标
「（已隐藏）」），然后点「开启」。

**期望结果**

- 还款日当天凌晨 3 点自动生成一笔转账，`备注`是「自动还款 2026-10」
- 交易列表里那笔的账户是「储蓄卡 → 信用卡」（**不是**支出）
- 扣款账户余额减少，卡的欠款减少
- 详情弹窗显示「上次自动还款：2026-10」

**故意不做的事**（不是 bug）

- **跨币种还款**：转账两端共用一个金额，¥10000 会原样加到 JPY 卡上
- **主动还钱后不会重复还**：你手动还了，自动任务会自动跳过
- **金额不够时部分还款**，但**不会把扣款账户扣成负数**
- **同账期只还一次**：自动还完又刷了一笔，下期才还
- 账单日 31 遇 2 月**顺延到 28**；某些月份因短月顺延可能不还款（正常）

**手动触发**（调度被禁用时也能用）

```bash
curl -X POST "https://你的域名/api/v1/write/ledgers/<账本id>/accounts/<卡id>/autorepay/run"   -H "Authorization: Bearer $TOKEN"
```

仍然走完整判断链 —— 今天不是还款日就什么也不做。

**排查**

| 现象 | 原因 |
|---|---|
| 开了没反应 | `SCHEDULER_TIMEZONE` 没配 → 按 UTC 算，差一整天 |
| 没反应且日志有 `autorepay.scheduler disabled` | **多进程部署**，已自动禁用。用上面的手动触发 |
| 日志 `database is locked` | 正常，会退避重试 3 次 |
| 手动触发返回 **502** | 还款真的失败了，`detail` 里有原因。502 而不是 200 是有意的 —— 早先吞成 200 时排查绕了很久 |
| 手动触发返回 **400** | 配置有问题（跨币种 / 绑了自己 / 缺账单日） |
| 还款卡在某一期不动 | 看日志有无 `IDEMPOTENCY_KEY_REUSED`；幂等键带金额，若欠款变了会换新键，旧键等 TTL 过期即可 |
| 显示「扣款账户已不存在」 | 扣款账户被删或币种变了 → 还款会跳过，重新绑一个 |

## 7. 附小票

```
attach_receipt(sync_id=<上一步返回的 sync_id>, image_base64="<base64>")
```

Web 交易详情应出现缩略图。

**从手机相册分享**：iPhone Safari 打开你的域名 → 添加到主屏幕 → 之后在
「照片」里长按图片 → 分享 → BeeCount，会直接打开新建交易并把小票挂上。

## 8. 备份（别跳过）

三条路径任选，其中**管理面板「备份」是本 fork 修好的**（上游对新账本必然
404）：

```bash
# 手动
/opt/beecount/scripts/backup_sqlite.sh \
  /opt/beecount/data/beecount.db /opt/beecount/data/backups/sqlite

# 定时（每天 3:17，避开整点）
17 3 * * * /opt/beecount/scripts/backup_sqlite.sh \
  /opt/beecount/data/beecount.db /opt/beecount/data/backups/sqlite
```

管理面板里还能配 rclone 定时备份（走 `VACUUM INTO`）。

> ⚠️ **不要用 `cp` 直接拷 `.db`** —— WAL 模式下会丢未提交写入。用上面的脚本，
> 它走 `sqlite3 .backup`。

**定期把 `data/backups/` 同步到别处**（rclone / 另一台机器）。备份和服务器在
同一块盘上，等于没有备份。

## 9. 升级

```bash
cd /opt/beecount
git pull
.venv/bin/pip install -r requirements.txt
cd frontend && pnpm install --no-frozen-lockfile && pnpm -C apps/web build && cd ..
.venv/bin/alembic upgrade head
systemctl restart beecount
```

数据在 `data/`，升级不动它。回滚同理（`git checkout <旧 commit>` 后重复上面）。

---

## 排查

| 症状 | 多半是 |
|---|---|
| Web 面板 404 | `WEB_STATIC_DIR` 没改，仍指向 `/app/static` |
| MCP 401/403 | Token 过期或被撤销；`mcp:write` 没勾 |
| 饼图没有「税与保险」 | 分类没建，或 `TAX_CATEGORY_NAME` env 改了（前端钉的是默认值「税与保险」） |
| 税额切片消失 | 饼图扇区上限问题已用「钉住」规避；若仍不见，看第 6 步饼图检查 |
| 上传小票失败 | `data/attachments` 不可写，或超了 `ATTACHMENT_MAX_UPLOAD_BYTES`（默认 64MB） |

## 部署后复验

```bash
npm i -D playwright && npx playwright install chromium
BC_BASE_URL=https://your-domain.com BC_TOKEN=bcmcp_xxx \
  node scripts/ui-smoke-tax.mjs
```

这份脚本会真开浏览器走一遍：新建交易 → 填税额 → 提交 → 查服务端 → 查饼图
→ 模拟 PWA 分享小票。**退出码 0 = 全通过。**
