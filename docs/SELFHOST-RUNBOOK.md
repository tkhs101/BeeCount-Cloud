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

本 fork 的 MCP 有 **31 个 tool**（官方 18 个）：在官方基础上补了消费税税额字段、
MCP 附件、预算增删、**账户 / 标签的完整增删改**、账户余额查询。

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
