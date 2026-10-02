import { useMemo } from 'react'
import type {
  AttachmentRef,
  WorkspaceCategory,
  WorkspaceTag,
  WorkspaceTransaction,
} from '@beecount/api-client'
import {
  Button,
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  useLocale,
  useT,
} from '@beecount/ui'
import { Amount, CategoryIcon, periodLabel, TransactionList } from '@beecount/web-features'

import { TAX_CATEGORY_NAME } from '../../lib/taxCategory'
import { ArrowRight, Edit3, TrendingDown, TrendingUp } from 'lucide-react'

import { useAuth } from '../../context/AuthContext'
import { formatCompactTick } from '../../i18n/format'
import type { DetailScope } from '../../lib/txDialogEvents'
import { DetailScopeToggle } from './DetailScopeToggle'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

interface Props {
  category: WorkspaceCategory | null
  /** 账本作用域:'all' 跨账本聚合,'current' 仅当前账本。控制交易列表 +
   *  统计的 ledger filter,顶部 toggle 可切换。 */
  scope: DetailScope
  onScopeChange: (next: DetailScope) => void
  /** 当前账本币种,KPI / 趋势图金额都按这个币种展示。 */
  currency: string
  /** 当前账本下该分类的全量(或近 N 条)交易,用于客户端聚合 KPI / 趋势 / Top 列表。 */
  statsTransactions: WorkspaceTransaction[]
  /** 已展示的分页交易(用于 TransactionList,允许更小的初始页 + 滚动加载)。 */
  transactions: WorkspaceTransaction[]
  /** 当前账本下该分类的总交易数(分页用,不一定等于 statsTransactions.length)。 */
  total: number
  offset: number
  loading: boolean
  /** stats 是否还在加载 — 控制 KPI 行的骨架/空态。 */
  statsLoading: boolean
  /** stats 是否被截断(总数 > 拉取上限),展示精度提示。 */
  statsTruncated: boolean
  tags: WorkspaceTag[]
  iconPreviewUrlByFileId?: Record<string, string>
  canManage?: boolean
  onClose: () => void
  onLoadMore: (categorySyncId: string, offset: number) => void
  onEdit: (category: WorkspaceCategory) => void
  onPreviewAttachment?: (refs: AttachmentRef[], startIndex: number) => Promise<void>
  onJumpToTransactions?: (category: WorkspaceCategory) => void
  /** 当前账本的每月起始日（1-28），用于把交易折算到正确的记账周期桶。默认 1 = 自然月。 */
  ledgerMonthStartDay?: number
}

interface StatsAgg {
  count: number
  /** 税前合计(与饼图分类切片同口径 —— 这里曾用原币全额算,和饼图对不上) */
  total: number
  /** 含税合计(实付),= total + taxTotal */
  grossTotal: number
  /** 该分类下所有交易的税额合计(0020) */
  taxTotal: number
  avg: number
  max: { amount: number; tx: WorkspaceTransaction | null }
  monthly: { bucket: string; amount: number; count: number }[]
  topAccounts: { name: string; amount: number; count: number }[]
  topTags: { name: string; color: string | null; count: number; amount: number }[]
  peak: { bucket: string; amount: number } | null
}

/**
 * 分类详情弹窗 — 富统计版。
 *
 * 视觉层次:
 *  1. Hero:大号 CategoryIcon + 名字 + 类型/父分类 badge
 *  2. KPI 行:交易数 / 累计金额 / 笔均 / 单笔最高
 *  3. 12 期月度趋势柱图(按当前账本币种,只算该分类金额)
 *  4. Top 账户(出现频次 + 累计金额) + Top 标签(co-occurrence)并排
 *  5. 该分类下的交易列表(分页/无限滚动)
 *
 * 数据约定:
 *  - 由 GlobalEntityDialogs 调用 fetchWorkspaceTransactions({ categorySyncId,
 *    ledgerId }) 拉到 statsTransactions(限定当前账本),所有 KPI / 趋势 /
 *    Top 都在客户端聚合。Trade-off:简单实现 vs 完整精度 — server 没有
 *    "单分类完整聚合" endpoint,但每个分类的 tx 量在单账本下通常不大,
 *    拉 limit=1000 已经能覆盖 95%+ 场景。超出时显示截断提示。
 *  - transactions 是给 TransactionList 用的分页流,可以跟 statsTransactions
 *    重叠,但 stats 不依赖它(避免分页过程中 KPI 抖动)。
 */
export function CategoryDetailDialog({
  category,
  scope,
  onScopeChange,
  currency,
  statsTransactions,
  transactions,
  total,
  offset,
  loading,
  statsLoading,
  statsTruncated,
  tags,
  iconPreviewUrlByFileId,
  canManage = true,
  onClose,
  onLoadMore,
  onEdit,
  onPreviewAttachment,
  onJumpToTransactions,
  ledgerMonthStartDay = 1,
}: Props) {
  const t = useT()
  const { locale } = useLocale()
  const chinese = locale.startsWith('zh')
  const { profileMe } = useAuth()
  const noteDisplayMode = profileMe?.appearance?.note_display_mode ?? 'category'

  const tagColorByName = useMemo(() => {
    const map = new Map<string, string | null>()
    for (const tg of tags) {
      if (tg.name) map.set(tg.name, tg.color || null)
    }
    return map
  }, [tags])

  const stats = useMemo<StatsAgg | null>(() => {
    if (!category) return null
    const agg = aggregate(statsTransactions, ledgerMonthStartDay)
    // 把 tag 颜色用 tags 字典回填,aggregate() 自身不依赖外部数据。
    agg.topTags = agg.topTags.map((tg) => ({
      ...tg,
      color: tagColorByName.get(tg.name) ?? null,
    }))
    return agg
  }, [category, statsTransactions, tagColorByName, ledgerMonthStartDay])

  const kindLabel = category
    ? category.kind === 'expense'
      ? t('enum.txType.expense')
      : category.kind === 'income'
        ? t('enum.txType.income')
        : t('enum.txType.transfer')
    : ''
  const kindToneClass =
    category?.kind === 'expense'
      ? 'text-expense'
      : category?.kind === 'income'
        ? 'text-income'
        : 'text-muted-foreground'
  const kindAmountTone =
    category?.kind === 'expense'
      ? 'negative'
      : category?.kind === 'income'
        ? 'positive'
        : 'default'

  return (
    <Dialog open={Boolean(category)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="flex max-h-[88vh] max-w-2xl flex-col gap-0 overflow-hidden p-0">
        <DialogHeader className="flex flex-row items-start justify-between gap-3 border-b border-border/60 bg-gradient-to-br from-muted/40 to-transparent px-6 py-4">
          <DialogTitle className="flex items-center gap-3">
            {category ? (
              <span
                className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-xl ${
                  category.kind === 'expense'
                    ? 'bg-expense/10 text-expense'
                    : category.kind === 'income'
                      ? 'bg-income/10 text-income'
                      : 'bg-muted/40 text-foreground'
                }`}
              >
                <CategoryIcon
                  icon={category.icon}
                  iconType={category.icon_type}
                  iconCloudFileId={category.icon_cloud_file_id}
                  iconPreviewUrlByFileId={iconPreviewUrlByFileId}
                  size={28}
                />
              </span>
            ) : null}
            <div className="flex min-w-0 flex-col">
              <span className="truncate text-lg font-semibold">{category?.name || ''}</span>
              {/* 消费税(0020):饼图上「税与保险」是一个**虚拟扇区** —— 它还包含
                  从餐饮/购物等分类里剥出来的消费税,而那些交易并不真的挂在
                  「税与保险」下。所以点进来看到的合计必然**小于**扇区值。
                  不说清楚的话用户会以为数据丢了。 */}
              {category?.name?.trim() === TAX_CATEGORY_NAME ? (
                <span className="mt-1 text-[11px] font-normal normal-case tracking-normal text-muted-foreground">
                  {t('detail.category.taxSliceHint')}
                </span>
              ) : null}
              <span
                className={`mt-0.5 flex items-center gap-1.5 text-[11px] font-normal uppercase tracking-[0.18em] ${kindToneClass}`}
              >
                {category?.kind === 'expense' ? (
                  <TrendingDown className="h-3 w-3" />
                ) : category?.kind === 'income' ? (
                  <TrendingUp className="h-3 w-3" />
                ) : null}
                <span>{kindLabel}</span>
                {category?.parent_name ? (
                  <>
                    <span className="text-muted-foreground/60">·</span>
                    <span className="normal-case tracking-normal text-muted-foreground">
                      {category.parent_name}
                    </span>
                  </>
                ) : null}
              </span>
            </div>
          </DialogTitle>
          <DetailScopeToggle value={scope} onChange={onScopeChange} className="mt-1 shrink-0" />
        </DialogHeader>

        {category ? (
          <div className="flex min-h-0 flex-1 flex-col">
            <div className="min-h-0 flex-1 overflow-y-auto">
              <KpiRow
                stats={stats}
                statsLoading={statsLoading}
                currency={currency}
                amountTone={kindAmountTone}
                t={t}
              />

              {statsTruncated ? (
                <div className="border-b border-border/60 bg-amber-500/5 px-6 py-2 text-[11px] text-muted-foreground">
                  {t('detail.category.statsTruncated').replace(
                    '{count}',
                    String(statsTransactions.length),
                  )}
                </div>
              ) : null}

              <TrendBars
                monthly={stats?.monthly || []}
                kind={category.kind}
                currency={currency}
                t={t}
                chinese={chinese}
                startDay={ledgerMonthStartDay}
              />

              <div className="grid gap-4 border-b border-border/60 px-6 py-4 sm:grid-cols-2">
                <TopList
                  title={t('detail.category.topAccounts')}
                  empty={t('detail.category.topEmpty')}
                  items={stats?.topAccounts || []}
                  currency={currency}
                  amountTone={kindAmountTone}
                  countLabel={t('home.topCat.countUnit')}
                />
                <TopList
                  title={t('detail.category.topTags')}
                  empty={t('detail.category.topEmpty')}
                  items={(stats?.topTags || []).map((tg) => ({
                    name: tg.name,
                    amount: tg.amount,
                    count: tg.count,
                    accentColor: tg.color || undefined,
                  }))}
                  currency={currency}
                  amountTone={kindAmountTone}
                  countLabel={t('home.topCat.countUnit')}
                />
              </div>

              <div className="border-b border-border/60 px-6 py-3">
                <div className="flex items-center justify-between">
                  <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
                    {t('detail.category.recentTxs')}
                  </div>
                  {onJumpToTransactions ? (
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 text-[11px] text-primary hover:underline"
                      onClick={() => onJumpToTransactions(category)}
                    >
                      {t('detail.category.viewAll')}
                      <ArrowRight className="h-3 w-3" />
                    </button>
                  ) : null}
                </div>
              </div>

              <TransactionList
                items={transactions}
                tags={tags}
                variant="compact"
                loading={loading}
                hasMore={transactions.length < total}
                onLoadMore={() => {
                  if (!loading && category) onLoadMore(category.id, offset)
                }}
                onPreviewAttachment={onPreviewAttachment}
                emptyTitle={t('detail.category.empty.title')}
                emptyDescription={t('detail.category.empty.desc')}
                showLedger={scope === 'all'}
                noteDisplayMode={noteDisplayMode}
              />
            </div>

            <div className="flex flex-row items-center justify-end gap-2 border-t border-border/60 bg-muted/20 px-6 py-3">
              <Button variant="outline" size="sm" onClick={onClose}>
                {t('dialog.cancel')}
              </Button>
              <Button size="sm" disabled={!canManage} onClick={() => onEdit(category)}>
                <Edit3 className="mr-1 h-3.5 w-3.5" />
                {t('common.edit')}
              </Button>
            </div>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

function KpiRow({
  stats,
  statsLoading,
  currency,
  amountTone,
  t,
}: {
  stats: StatsAgg | null
  statsLoading: boolean
  currency: string
  amountTone: 'negative' | 'positive' | 'default'
  t: (k: string) => string
}) {
  const empty = !statsLoading && (!stats || stats.count === 0)
  return (
    <>
      <div className="grid grid-cols-4 gap-3 border-b border-border/60 bg-muted/20 px-6 py-4 text-center">
        <KpiCell label={t('detail.stats.txCount')} loading={statsLoading} empty={empty}>
          <span className="font-mono text-xl font-bold tabular-nums">{stats?.count ?? 0}</span>
        </KpiCell>
        <KpiCell label={t('detail.category.kpi.total')} loading={statsLoading} empty={empty}>
          <Amount
            value={stats?.total ?? 0}
            currency={currency}
            size="md"
            bold
            tone={amountTone}
          />
        </KpiCell>
        <KpiCell label={t('detail.category.kpi.avg')} loading={statsLoading} empty={empty}>
          <Amount
            value={stats?.avg ?? 0}
            currency={currency}
            size="md"
            bold
            tone={amountTone}
          />
        </KpiCell>
        <KpiCell label={t('detail.category.kpi.max')} loading={statsLoading} empty={empty}>
          <Amount
            value={stats?.max.amount ?? 0}
            currency={currency}
            size="md"
            bold
            tone={amountTone}
          />
        </KpiCell>
      </div>
      {/* 消费税(0020):上面的「累计金额」是税前(与饼图切片同口径)。有这个
          分类含税时补一行「消费税 X · 实付合计 Y」,免得用户以为钱少了。 */}
      {!empty && stats && stats.taxTotal > 0 ? (
        <p className="border-b border-border/60 bg-muted/20 px-6 pb-3 text-center text-xs tabular-nums text-muted-foreground">
          {t('detail.transaction.tax')}{' '}
          {stats.taxTotal.toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })}
          {' · '}
          {t('detail.transaction.grossAmount')}{' '}
          {stats.grossTotal.toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })}
        </p>
      ) : null}
    </>
  )
}

function KpiCell({
  label,
  loading,
  empty,
  children,
}: {
  label: string
  loading: boolean
  empty: boolean
  children: React.ReactNode
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="mt-0.5 flex h-7 items-center justify-center">
        {loading ? (
          <span className="h-4 w-12 animate-pulse rounded bg-muted" aria-hidden />
        ) : empty ? (
          <span className="font-mono text-base text-muted-foreground">—</span>
        ) : (
          children
        )}
      </div>
    </div>
  )
}

function TrendBars({
  monthly,
  kind,
  currency,
  t,
  chinese,
  startDay = 1,
}: {
  monthly: { bucket: string; amount: number; count: number }[]
  kind: WorkspaceCategory['kind']
  currency: string
  t: (k: string) => string
  chinese: boolean
  startDay?: number
}) {
  // 取最近 12 个月。如果不足 12 期,补空格保证轴长度一致,视觉上能看出"才记账几个月"。
  const slice = useMemo(() => fillTrailingMonths(monthly, 12, startDay), [monthly, startDay])
  const hasData = slice.some((s) => s.amount > 0)
  const barFill =
    kind === 'expense'
      ? 'rgb(var(--expense-rgb))'
      : kind === 'income'
        ? 'rgb(var(--income-rgb))'
        : 'hsl(var(--muted-foreground))'
  const peak = slice.reduce(
    (best, s) => (s.amount > best.amount ? s : best),
    { bucket: '', amount: 0, count: 0 },
  )

  return (
    <div className="border-b border-border/60 px-6 py-4">
      <div className="flex items-center justify-between">
        <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
          {t('detail.category.trend.title')}
        </div>
        {peak.amount > 0 ? (
          <div className="flex items-center gap-1.5 text-[11px]">
            <span className="text-muted-foreground">{t('detail.category.trend.peak')}</span>
            <span className="font-mono tabular-nums text-foreground">{peak.bucket}</span>
            <Amount
              value={peak.amount}
              currency={currency}
              size="xs"
              tone={
                kind === 'expense' ? 'negative' : kind === 'income' ? 'positive' : 'default'
              }
              bold
            />
          </div>
        ) : null}
      </div>
      <div className="mt-2 h-32">
        {!hasData ? (
          <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
            {t('detail.category.trend.empty')}
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={slice} margin={{ left: 0, right: 4, top: 4, bottom: 0 }}>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="hsl(var(--border))"
                vertical={false}
              />
              <XAxis
                dataKey="bucket"
                tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 10 }}
                stroke="hsl(var(--border))"
                tickFormatter={(b: string) => {
                  const parts = b.split('-')
                  return parts.length >= 2 ? parts.slice(1).join('-') : b
                }}
                interval={0}
              />
              <YAxis
                tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 10 }}
                stroke="hsl(var(--border))"
                tickFormatter={(v: number) =>
                  formatCompactTick(v, { chinese, wanUnit: t('common.unit.10k') })
                }
                width={40}
              />
              <Tooltip
                contentStyle={{
                  background: 'hsl(var(--popover))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: 6,
                  fontSize: 12,
                }}
                cursor={{ fill: 'hsl(var(--muted) / 0.4)' }}
                formatter={((v: number) => [
                  v.toLocaleString(undefined, {
                    minimumFractionDigits: 0,
                    maximumFractionDigits: 0,
                  }),
                  t('detail.category.trend.amount'),
                ]) as unknown as never}
                labelFormatter={((b: string) => b) as unknown as never}
              />
              <Bar dataKey="amount" radius={[3, 3, 0, 0]}>
                {slice.map((s) => (
                  <Cell
                    key={s.bucket}
                    fill={barFill}
                    fillOpacity={s.bucket === peak.bucket && peak.amount > 0 ? 1 : 0.7}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
      <p className="mt-1 text-[10px] text-muted-foreground">
        {t('detail.category.trend.hint')}
      </p>
    </div>
  )
}

function TopList({
  title,
  empty,
  items,
  currency,
  amountTone,
  countLabel,
}: {
  title: string
  empty: string
  items: { name: string; amount: number; count: number; accentColor?: string }[]
  currency: string
  amountTone: 'negative' | 'positive' | 'default'
  countLabel: string
}) {
  const top = items.slice(0, 3)
  const max = Math.max(1, ...top.map((i) => i.amount))
  return (
    <div className="rounded-md border border-border/40 bg-muted/10 p-3">
      <div className="mb-2 text-[11px] uppercase tracking-widest text-muted-foreground">
        {title}
      </div>
      {top.length === 0 ? (
        <div className="flex h-16 items-center justify-center text-xs text-muted-foreground">
          {empty}
        </div>
      ) : (
        <ul className="space-y-2">
          {top.map((row, i) => {
            const barPct = (row.amount / max) * 100
            const dotStyle = row.accentColor
              ? { background: row.accentColor }
              : undefined
            return (
              <li key={`${row.name}-${i}`}>
                <div className="flex items-center justify-between gap-2 text-xs">
                  <span className="inline-flex min-w-0 items-center gap-1.5">
                    {row.accentColor ? (
                      <span
                        className="h-2 w-2 shrink-0 rounded-full"
                        style={dotStyle}
                        aria-hidden
                      />
                    ) : (
                      <span className="w-4 shrink-0 text-center text-[10px] text-muted-foreground tabular-nums">
                        {i + 1}
                      </span>
                    )}
                    <span className="truncate">{row.name}</span>
                    <span className="shrink-0 text-[10px] text-muted-foreground">
                      {row.count}
                      {countLabel}
                    </span>
                  </span>
                  <Amount
                    value={row.amount}
                    currency={currency}
                    size="xs"
                    bold
                    tone={amountTone}
                  />
                </div>
                <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-muted/40">
                  <div
                    className={`h-full rounded-full ${
                      amountTone === 'negative'
                        ? 'bg-expense/70'
                        : amountTone === 'positive'
                          ? 'bg-income/70'
                          : 'bg-foreground/30'
                    }`}
                    style={{ width: `${barPct}%` }}
                  />
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

// --------------------------------------------------------------------------
// Aggregation helpers
// --------------------------------------------------------------------------

/**
 * 原币税额 → 折本位币税额(0020)。
 *
 * 与 server 的 `routers/read/_shared.tax_in_base_currency` **同一套公式**:
 * `tax / amount` 的比率与币种无关,按该笔自身的隐含汇率换算,保证税额和主
 * 金额走同一个汇率、不会各自漂移。
 *
 * 结果夹在 `[0, base]` 内 —— 脏数据(税额大于金额)也保证「净额 ≥ 0」且
 * 「净额 + 税 ≤ 实付」,不变式不会被打破。
 */
export function taxInBaseCurrency(
  taxAmount: number | null | undefined,
  rawAmount: number | null | undefined,
  baseAmount: number
): number {
  if (taxAmount == null) return 0
  const tax = Math.abs(Number(taxAmount) || 0)
  if (!(tax > 0)) return 0
  const base = Math.abs(Number(baseAmount) || 0)
  if (!(base > 0)) return 0
  const raw = Math.abs(Number(rawAmount) || 0)
  if (!(raw > 0)) return Math.min(tax, base)
  return Math.max(0, Math.min(base, base * (tax / raw)))
}

export function aggregate(
  transactions: WorkspaceTransaction[],
  startDay = 1
): StatsAgg {
  let total = 0
  let taxTotal = 0
  let counted = 0
  let maxAmount = 0
  let maxTx: WorkspaceTransaction | null = null
  const monthlyMap = new Map<string, { amount: number; count: number }>()
  const accountMap = new Map<string, { amount: number; count: number }>()
  // tagName → { count, amount, color? }。color 在调用方按 tag_id 反查。
  const tagMap = new Map<string, { count: number; amount: number; color: string | null }>()

  for (const tx of transactions) {
    // 排除标记为「不计入收支统计」的交易 —— server 的 workspace_analytics 在
    // SQL 层就把它们滤掉了(`exclude_from_stats == false`),而这里是客户端
    // 聚合,漏掉这一层就会让「分类详情合计」比「饼图切片」大,点进去对不上账。
    // 下面 TransactionList 仍会列出这些交易(用户要能看到自己排除了什么),
    // 所以只在这里滤,不在拉数据时滤。
    if (tx.exclude_from_stats) continue

    // 其余口径对齐 server 的账本维度统计(workspace_analytics,0018/0020):
    //   基数 = coalesce(native_amount, amount)  ← 这里原来只读原币 amount,
    //     外币交易会和饼图对不上
    //   净额 = 基数 - 折本位币税额                ← 饼图分类切片是税前
    //
    // 税额必须**按比率折成本位币**再减:`base * (tax / amount)`。直接拿原币
    // 税额去减本位币基数是错的 —— 50 CNY / 税 5 / 汇率 20 的交易会算出
    // 1000 − 5 = 995,而正确值是 1000 − 100 = 900。server 侧
    // `tax_in_base_currency` 做的就是这件事,这里必须同口径。
    const base = Math.abs(Number(tx.native_amount ?? tx.amount) || 0)
    const taxNative = taxInBaseCurrency(tx.tax_amount, tx.amount, base)
    const amt = base - taxNative
    counted += 1
    taxTotal += taxNative
    total += amt
    if (amt > maxAmount) {
      maxAmount = amt
      maxTx = tx
    }
    const bucket = monthBucketLocal(tx.happened_at, startDay)
    if (bucket) {
      const slot = monthlyMap.get(bucket) || { amount: 0, count: 0 }
      slot.amount += amt
      slot.count += 1
      monthlyMap.set(bucket, slot)
    }
    const accountName =
      tx.account_name || tx.from_account_name || tx.to_account_name || ''
    if (accountName) {
      const slot = accountMap.get(accountName) || { amount: 0, count: 0 }
      slot.amount += amt
      slot.count += 1
      accountMap.set(accountName, slot)
    }
    const tagNames = (tx.tags_list || [])
      .map((tg) => (tg || '').trim())
      .filter((tg) => tg.length > 0)
    for (const tagName of tagNames) {
      const slot = tagMap.get(tagName) || { count: 0, amount: 0, color: null }
      slot.count += 1
      slot.amount += amt
      tagMap.set(tagName, slot)
    }
  }

  const monthly = Array.from(monthlyMap.entries())
    .map(([bucket, v]) => ({ bucket, amount: v.amount, count: v.count }))
    .sort((a, b) => a.bucket.localeCompare(b.bucket))

  const topAccounts = Array.from(accountMap.entries())
    .map(([name, v]) => ({ name, amount: v.amount, count: v.count }))
    .sort((a, b) => b.amount - a.amount)

  const topTags = Array.from(tagMap.entries())
    .map(([name, v]) => ({ name, amount: v.amount, count: v.count, color: v.color }))
    .sort((a, b) => b.count - a.count)

  let peak: { bucket: string; amount: number } | null = null
  for (const m of monthly) {
    if (!peak || m.amount > peak.amount) peak = { bucket: m.bucket, amount: m.amount }
  }

  return {
    // 注意是**过滤后**的笔数,不是 transactions.length —— 被
    // exclude_from_stats 排除的那些在饼图里没有,笔数也得跟着少
    count: counted,
    total,
    grossTotal: total + taxTotal,
    taxTotal,
    avg: counted > 0 ? total / counted : 0,
    max: { amount: maxAmount, tx: maxTx },
    monthly,
    topAccounts,
    topTags,
    peak,
  }
}

/** 用本地时区把 ISO 时间换成记账周期标签 YYYY-MM。 */
function monthBucketLocal(iso: string | null | undefined, startDay = 1): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return periodLabel(d, startDay)
}

/** 把不足 N 期的 monthly 数组补齐到 trailing N 个记账周期,空周期 amount=0 占位。 */
function fillTrailingMonths(
  monthly: { bucket: string; amount: number; count: number }[],
  n: number,
  startDay = 1,
): { bucket: string; amount: number; count: number }[] {
  const map = new Map<string, { amount: number; count: number }>()
  for (const m of monthly) map.set(m.bucket, { amount: m.amount, count: m.count })
  const out: { bucket: string; amount: number; count: number }[] = []
  const [ly, lm] = periodLabel(new Date(), startDay).split('-').map(Number)
  for (let i = n - 1; i >= 0; i -= 1) {
    const d = new Date(ly, lm - 1 - i, 1)
    const bucket = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
    const slot = map.get(bucket)
    out.push({ bucket, amount: slot?.amount || 0, count: slot?.count || 0 })
  }
  return out
}
