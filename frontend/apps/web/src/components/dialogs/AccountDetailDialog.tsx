import type {
  ReadAccount,
  WorkspaceTag,
  WorkspaceTransaction
} from '@beecount/api-client'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  useT
} from '@beecount/ui'
import {
  accountBalance,
  hasBankName,
  hasCardLastFour,
  repaySourceCandidates,
  sourceAccountIssue,
  TransactionList
} from '@beecount/web-features'
import {
  Button,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from '@beecount/ui'
import {
  Banknote,
  Calendar as CalendarIcon,
  CreditCard,
  Zap as ZapIcon
} from 'lucide-react'

import { useState, useEffect } from 'react'

import { useAuth } from '../../context/AuthContext'
import type { DetailScope } from '../../lib/txDialogEvents'
import { DetailScopeToggle } from './DetailScopeToggle'

type AccountWithStats = ReadAccount & {
  tx_count?: number | null
  income_total?: number | null
  expense_total?: number | null
  balance?: number | null
}

interface Props {
  account: AccountWithStats | null
  scope: DetailScope
  onScopeChange: (next: DetailScope) => void
  transactions: WorkspaceTransaction[]
  total: number
  offset: number
  loading: boolean
  tags: WorkspaceTag[]
  onClose: () => void
  onLoadMore: (accountName: string, offset: number) => void
  onPreviewAttachment?: (ctx: unknown) => void
  resolveAttachmentPreviewUrl?: (att: unknown) => string | null
  /** 自动还款(0022):候选扣款账户。**必须包含 hidden 账户** ——
   *  规则指向一个已隐藏的账户时,用户需要在界面上看见它在跑。 */
  allAccounts?: ReadAccount[]
  /** 保存自动还款配置。返回错误消息表示失败,null 表示成功。 */
  onSaveAutoRepay?: (patch: {
    enabled: boolean
    from_account_sync_id: string | null
  }) => Promise<string | null>
}

/** 点账户卡片弹出的详情:顶部账户名 + 当前余额/累计收入/累计支出 + 交易列表(无限滚动加载)。 */
export function AccountDetailDialog({
  account,
  scope,
  onScopeChange,
  transactions,
  total,
  offset,
  loading,
  tags,
  onClose,
  onLoadMore,
  onPreviewAttachment,
  resolveAttachmentPreviewUrl,
  allAccounts,
  onSaveAutoRepay,
}: Props) {
  const t = useT()
  const { profileMe } = useAuth()
  const noteDisplayMode = profileMe?.appearance?.note_display_mode ?? 'category'
  return (
    <Dialog open={Boolean(account)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="flex max-h-[85vh] max-w-2xl flex-col gap-0 overflow-hidden p-0">
        <DialogHeader className="flex flex-row items-center justify-between gap-3 border-b border-border/60 px-6 py-4">
          <DialogTitle className="truncate">{account?.name || ''}</DialogTitle>
          <DetailScopeToggle value={scope} onChange={onScopeChange} className="shrink-0" />
        </DialogHeader>
        {account ? (
          <div className="flex min-h-0 flex-1 flex-col">
            {/* 统计:优先 server 返回的 balance/income/expense,缺失时兜底 initial_balance.
                注意:这里的统计来自 props 上的 account 实体,本身是按上层 scope
                取的(GlobalEntityDialogs / AccountsPage 通过 fetchWorkspaceAccounts
                的 ledgerId 参数控制)。弹窗顶部 scope 切换只影响交易列表过滤,
                上面这块 KPI 沿用打开时的快照,不跟随 scope 实时切换 —
                跟 mobile 端 account_detail_page 行为一致。 */}
            <AccountStatsHeader account={account} t={t} />

            {/* 信用卡 / 银行卡专属信息:bank_name / 卡号末 4 / 信用额度 /
                账单日 / 还款日 + 倒计时。普通账户类型不渲染。 */}
            <AccountCardInfo account={account} t={t}
        allAccounts={allAccounts} onSaveAutoRepay={onSaveAutoRepay} />

            <div className="min-h-0 flex-1 overflow-y-auto">
              <TransactionList
                items={transactions}
                tags={tags}
                variant="compact"
                loading={loading}
                hasMore={transactions.length < total}
                onLoadMore={() => {
                  if (!loading) onLoadMore(account.name, offset)
                }}
                onPreviewAttachment={onPreviewAttachment as never}
                resolveAttachmentPreviewUrl={resolveAttachmentPreviewUrl as never}
                emptyTitle={t('transactions.empty.forAccount.title')}
                showLedger={scope === 'all'}
                noteDisplayMode={noteDisplayMode}
              />
            </div>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

function AccountStatsHeader({
  account,
  t,
}: {
  account: AccountWithStats
  t: (key: string) => string
}) {
  const hasServerStats = typeof account.balance === 'number'
  const balance = accountBalance(account)
  const fmt = (v: number) =>
    v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })

  // 信用卡按负债展示:只显当前欠款(= -balance,余额为负=欠款),不显示
  // 累计收入/支出(信用卡入账是还款/退款,非收入);额度/可用/账单在下方
  // AccountCardInfo。对齐 mobile account_detail_page。
  if ((account.account_type || '') === 'credit_card') {
    const owed = Math.max(0, -balance)
    return (
      <div className="border-b border-border/60 bg-muted/20 px-6 py-4 text-center">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
          {t('accounts.bankcard.currentOwed')}
        </div>
        <div className="mt-0.5 font-mono text-lg font-bold tabular-nums text-expense">
          {fmt(owed)}
        </div>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-3 gap-3 border-b border-border/60 bg-muted/20 px-6 py-4 text-center">
      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
          {t('detail.stats.currentBalance')}
        </div>
        <div
          className={`mt-0.5 font-mono text-base font-bold tabular-nums ${
            balance >= 0 ? 'text-foreground' : 'text-expense'
          }`}
        >
          {fmt(balance)}
        </div>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
          {t('detail.stats.accumIncome')}
        </div>
        <div className="mt-0.5 font-mono text-base font-bold tabular-nums text-income">
          {fmt(account.income_total ?? 0)}
        </div>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
          {t('detail.stats.accumExpense')}
        </div>
        <div className="mt-0.5 font-mono text-base font-bold tabular-nums text-expense">
          {fmt(account.expense_total ?? 0)}
        </div>
      </div>
    </div>
  )
}

/**
 * 信用卡 / 银行卡专属信息卡片。
 *
 * 展示规则:
 *   - bank_card: 银行 + 卡号末 4 位(2 项中只要任一有值就显示)
 *   - credit_card: 上面 2 项 + 信用额度 + 账单日 + 还款日 + 倒计时
 *   - 其它账户类型(cash / alipay / etc):整块不渲染
 *
 * 倒计时算法:今天日号 ≤ 目标日号 → 目标日号 - 今天日号;否则 → 跨月,
 * (本月剩余天数) + 目标日号。"今天"取本地时区,因为还款日是法律日历日,
 * 不需要 UTC。
 */
function AccountCardInfo({
  account,
  t,
  allAccounts,
  onSaveAutoRepay,
}: {
  account: AccountWithStats
  t: (key: string, params?: Record<string, string | number>) => string
  allAccounts?: ReadAccount[]
  onSaveAutoRepay?: (
    patch: { enabled: boolean; from_account_sync_id: string | null },
  ) => Promise<string | null>
}) {
  const accountType = account.account_type || ''
  const isCreditCard = accountType === 'credit_card'
  // 按能力判定,不 `isBankOrCredit` 一个布尔 —— bank_account(普通存款)
  // 要显示开户行但没有卡号,一个布尔表达不了。
  const showBank = hasBankName(accountType)
  const showCard = hasCardLastFour(accountType)
  if (!showBank && !showCard) return null

  const bankName = showBank ? (account.bank_name?.trim() || '') : ''
  const cardLastFour = showCard ? (account.card_last_four?.trim() || '') : ''
  const creditLimit = isCreditCard ? account.credit_limit : null
  const billingDay = isCreditCard ? account.billing_day : null
  const paymentDueDay = isCreditCard ? account.payment_due_day : null

  // 信用卡已用额度 = -balance(余额为负表示欠款),剩余额度 = limit - used。
  // 这是粗略估算 — 没考虑账单周期,只看终身累计。但作为"大致还能刷多少"
  // 的信号已经够用,后续要精确版本应该按 billing_day 分账期算。
  //
  // 为什么用 balance 而不是 expense_total - income_total:后者会漏掉"储蓄卡
  // 转账到信用卡还款"这种 transfer-in 操作 —— 转账既不是 expense 也不是
  // income,只在 backend 的 balance 计算里被加回去(workspace.py 的
  // transfer_to bucket)。balance 已经统一包含初始余额 + 全部 income /
  // expense / transfer-in / transfer-out,跟 mobile 端
  // `getCreditCardUsedAmount` (balance < 0 ? -balance : 0) 完全一致。
  // 修复 issue #26:储蓄卡转账到信用卡额度没恢复。
  const balance = accountBalance(account)
  const used =
    typeof creditLimit === 'number' ? Math.max(0, -balance) : null
  const remaining =
    typeof creditLimit === 'number' && used !== null
      ? Math.max(0, creditLimit - used)
      : null

  // 没有任何要展示的就直接不渲染(用户没填这些字段时)
  if (
    !bankName &&
    !cardLastFour &&
    creditLimit === null &&
    !billingDay &&
    !paymentDueDay
  ) {
    return null
  }

  const fmt = (v: number) =>
    v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })

  return (
    <div className="border-b border-border/60 bg-muted/10 px-6 py-3">
      {/* 第一行:银行 + 卡号末 4 位 + 类型 icon */}
      {(bankName || cardLastFour) ? (
        <div className="flex items-center gap-2 text-sm">
          {isCreditCard ? (
            <CreditCard className="h-4 w-4 text-muted-foreground" />
          ) : (
            <Banknote className="h-4 w-4 text-muted-foreground" />
          )}
          <span className="font-medium">{bankName || t('detail.account.bankUnknown')}</span>
          {cardLastFour ? (
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              •••• {cardLastFour}
            </span>
          ) : null}
        </div>
      ) : null}

      {/* 信用卡:额度 + 账单日 + 还款日 + 倒计时 */}
      {isCreditCard ? (
        <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
          {creditLimit !== null && creditLimit !== undefined ? (
            <CardInfoItem
              label={t('detail.account.creditLimit')}
              value={fmt(creditLimit)}
              hint={
                remaining !== null
                  ? t('detail.account.remaining', { value: fmt(remaining) })
                  : undefined
              }
            />
          ) : null}
          {billingDay ? (
            <CardInfoItem
              label={t('detail.account.billingDay')}
              value={t('detail.account.dayOfMonth', { day: billingDay })}
              hint={t('detail.account.daysUntil', { days: daysUntilDay(billingDay) })}
            />
          ) : null}
          {paymentDueDay ? (
            <CardInfoItem
              label={t('detail.account.paymentDueDay')}
              value={t('detail.account.dayOfMonth', { day: paymentDueDay })}
              hint={t('detail.account.daysUntil', { days: daysUntilDay(paymentDueDay) })}
              urgent={daysUntilDay(paymentDueDay) <= 3}
            />
          ) : null}
        </div>
      ) : null}
      {/* 自动还款(0022)—— 挂在还款日那一格下方:那里已经有「每月 N 号 /
        还有 N 天」,自动还款是这条信息的自然延伸,不新增一级导航。 */}
      {isCreditCard && paymentDueDay && onSaveAutoRepay && allAccounts ? (
        <AutoRepaySection
          card={account}
          accounts={allAccounts}
          onSave={onSaveAutoRepay}
        />
      ) : null}
    </div>
  )
}

function CardInfoItem({
  label,
  value,
  hint,
  urgent,
}: {
  label: string
  value: string
  hint?: string
  urgent?: boolean
}) {
  return (
    <div>
      <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        <CalendarIcon className="h-3 w-3" />
        <span>{label}</span>
      </div>
      <div className="mt-0.5 text-sm font-medium tabular-nums">{value}</div>
      {hint ? (
        <div
          className={`text-[10px] tabular-nums ${
            urgent ? 'text-expense font-semibold' : 'text-muted-foreground'
          }`}
        >
          {hint}
        </div>
      ) : null}
    </div>
  )
}

/**
 * 今天到目标日号还有多少天。
 *   - 今天 ≤ 目标 → 本月内,直接差值
 *   - 今天 > 目标 → 跨月,本月剩余 + 目标日号
 *
 * 目标日号超过当月最大天数(比如 31 号但 2 月)按当月最后一天兜底,跟
 * mobile 端 AccountDetailPage 算法对齐。
 */
function daysUntilDay(targetDay: number): number {
  if (!Number.isFinite(targetDay) || targetDay < 1 || targetDay > 31) return 0
  const now = new Date()
  const today = now.getDate()
  if (today <= targetDay) {
    // 同月内 — 验证当月有这一天(2 月没有 31 号)
    const lastDayThisMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate()
    const effective = Math.min(targetDay, lastDayThisMonth)
    return effective - today
  }
  // 跨月 — 算本月剩余 + 下月目标日(下月可能也没那一天)
  const lastDayThisMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate()
  const lastDayNextMonth = new Date(now.getFullYear(), now.getMonth() + 2, 0).getDate()
  const effective = Math.min(targetDay, lastDayNextMonth)
  return (lastDayThisMonth - today) + effective
}

// ============================================================================
// 信用卡自动还款(0022)
// ============================================================================

/** 自动还款的绑定 / 暂停 / 故障提示。
 *
 * ## 为什么「暂停」而不是「删除」
 *
 * 用户「这个月先不还」时期望的是**关掉**,不是删掉重填。删了要重选账户、
 * 重选币种、重新确认。所以 `enabled=false` 保留配置,只关执行。
 *
 * ## 为什么扣款账户候选要包含 hidden
 *
 * `TransactionsPanel` 把 hidden 账户排除在所有选择器之外。若沿用那套,
 * 用户给已隐藏的卡绑不了还款;而已有规则可能正指向一个后来被隐藏的账户 ——
 * 结果是**一条规则在跑,用户在界面上完全看不见**。
 *
 * ## 失败必须可见
 *
 * 扣款账户被删 / 币种变了 → 自动还款会静默跳过。界面上必须说清楚,
 * 不能只显示「已启用」让用户以为一切正常。
 */
function AutoRepaySection({
  card,
  accounts,
  onSave,
}: {
  card: ReadAccount & {
    autorepay_enabled?: boolean | null
    autorepay_from_account_sync_id?: string | null
    autorepay_last_period?: string | null
  }
  accounts: ReadAccount[]
  onSave: (patch: {
    enabled: boolean
    from_account_sync_id: string | null
  }) => Promise<string | null>
}) {
  // 本仓的 `useT()` 返回的就是 t 函数本身(不是 { t } 对象)——
  // 写成 `const { t } = useT()` 会拿到 t 自己的属性,全 undefined。
  const t = useT()
  const [sourceId, setSourceId] = useState<string>(
    card.autorepay_from_account_sync_id || '',
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 账户数据刷新后同步选中项(换账户编辑时会走到这里)
  useEffect(() => {
    setSourceId(card.autorepay_from_account_sync_id || '')
  }, [card.id, card.autorepay_from_account_sync_id])

  const enabled = Boolean(card.autorepay_enabled)
  const candidates = repaySourceCandidates(accounts, card)
  const issue = sourceAccountIssue(
    {
      enabled,
      from_account_sync_id: card.autorepay_from_account_sync_id ?? null,
      last_period: card.autorepay_last_period ?? null,
    },
    candidates,
  )

  const save = async (patch: { enabled: boolean; from_account_sync_id: string | null }) => {
    setBusy(true)
    setError(null)
    try {
      const msg = await onSave(patch)
      if (msg) setError(msg)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-3 rounded-md border bg-muted/20 p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <ZapIcon
              className={
                enabled && issue === 'ok'
                  ? 'h-4 w-4 shrink-0 text-expense'
                  : 'h-4 w-4 shrink-0 text-muted-foreground'
              }
            />
            <span className="truncate text-sm font-medium">
              {enabled
                ? t('detail.autoRepay.on', { day: String(card.payment_due_day ?? '') })
                : t('detail.autoRepay.off')}
            </span>
          </div>
          {enabled && issue !== 'ok' ? (
            <p className="mt-1 text-xs text-expense">
              {t('detail.autoRepay.sourceMissing')}
            </p>
          ) : null}
          {card.autorepay_last_period ? (
            <p className="mt-1 text-xs text-muted-foreground">
              {t('detail.autoRepay.lastPeriod', {
                period: card.autorepay_last_period,
              })}
            </p>
          ) : null}
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() =>
            save({
              enabled: !enabled,
              from_account_sync_id: enabled
                ? (card.autorepay_from_account_sync_id ?? null)
                : (sourceId || null),
            })
          }
        >
          {enabled ? t('detail.autoRepay.pause') : t('detail.autoRepay.enable')}
        </Button>
      </div>

      {!enabled ? (
        <div className="mt-3 space-y-1">
          <Label>{t('detail.autoRepay.source')}</Label>
          <Select
            value={sourceId}
            onValueChange={setSourceId}
            disabled={busy || candidates.length === 0}
          >
            <SelectTrigger className="h-10">
              <SelectValue placeholder={t('detail.autoRepay.sourcePlaceholder')} />
            </SelectTrigger>
            <SelectContent>
              {candidates.map((a) => (
                <SelectItem key={a.id} value={a.id}>
                  {a.name}
                  {a.hidden ? t('detail.autoRepay.hiddenSuffix') : ''}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {candidates.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              {t('detail.autoRepay.noCandidate')}
            </p>
          ) : null}
        </div>
      ) : null}

      {error ? <p className="mt-2 text-xs text-expense">{error}</p> : null}
    </div>
  )
}
