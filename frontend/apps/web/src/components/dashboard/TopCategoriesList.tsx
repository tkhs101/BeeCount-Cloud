import { Card, CardContent, CardHeader, CardTitle, useT } from '@beecount/ui'

interface Rank {
  category_name: string
  total: number
  tx_count: number
}

type Variant = 'expense' | 'income'

interface Props {
  ranks: Rank[]
  variant?: Variant
  title?: string
  onClickCategory?: (name: string) => void
}

/** 与饼图的扇区上限保持一致 —— 两处对不上会让同一份数据呈现两种口径。 */
const TOP_CATEGORY_LIMIT = 8

export function TopCategoriesList({ ranks, variant = 'expense', title, onClickCategory }: Props) {
  const t = useT()
  // 与 HomeMonthCategoryDonut 的 MAX_SLICES 保持一致(8)。
  // 消费税扇区金额天然很小,停在 5 的话它进不了列表 —— 而这个列表正是
  // 用户核对「各类花了多少」的地方,税额缺席等于功能没做。
  const top = ranks.slice(0, TOP_CATEGORY_LIMIT)
  const maxTotal = Math.max(1, ...top.map((r) => r.total))
  // 占比基数:**所有分类总和**(不只前 N),反映真实分布。如果只用前 N
  // 的 total 算占比,top 1 永远是 100% 这种数字,信息量 = 0。
  const grandTotal = Math.max(1, ranks.reduce((sum, r) => sum + r.total, 0))

  const fmt = (v: number) =>
    v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  const fmtPct = (v: number) => {
    // < 1% 显示 "<1%",避免 "0.4%" 之类极小看着像 0
    if (v < 1) return v < 0.05 ? '0%' : '<1%'
    return `${Math.round(v)}%`
  }

  const isExpense = variant === 'expense'
  const barClass = isExpense
    ? 'bg-gradient-to-r from-expense/70 to-expense group-hover:from-expense group-hover:to-expense/90'
    : 'bg-gradient-to-r from-income/70 to-income group-hover:from-income group-hover:to-income/90'
  const defaultTitle = isExpense ? t('home.topCat.expenseTitle') : t('home.topCat.incomeTitle')
  const emptyLabel = isExpense ? t('home.topCat.empty.expense') : t('home.topCat.empty.income')

  return (
    <Card className="bc-panel overflow-hidden">
      <CardHeader>
        <CardTitle className="text-base">{title || defaultTitle}</CardTitle>
      </CardHeader>
      <CardContent>
        {top.length === 0 ? (
          <div className="flex h-32 items-center justify-center text-xs text-muted-foreground">
            {emptyLabel}
          </div>
        ) : (
          <ul className="space-y-2.5">
            {top.map((r, i) => {
              // 进度条用 maxTotal 归一化(Top 1 = 满条 = 视觉锚点);
              // 占比文字用 grandTotal 归一化(反映真实份额)。
              const barPct = (r.total / maxTotal) * 100
              const sharePct = (r.total / grandTotal) * 100
              return (
                <li
                  key={r.category_name}
                  className={`group ${onClickCategory ? 'cursor-pointer' : ''}`}
                  onClick={() => onClickCategory?.(r.category_name)}
                >
                  <div className="flex items-center justify-between text-sm">
                    <span className="inline-flex items-center gap-2">
                      <span className="flex h-5 w-5 items-center justify-center rounded-full bg-muted text-[10px] font-semibold">
                        {i + 1}
                      </span>
                      <span className="font-medium">{r.category_name || t('home.topCat.uncategorized')}</span>
                      <span className="text-[11px] text-muted-foreground">{r.tx_count} {t('home.topCat.countUnit')}</span>
                    </span>
                    <span className="inline-flex items-baseline gap-1.5 font-mono tabular-nums">
                      <span className="text-sm">{fmt(r.total)}</span>
                      <span className="text-[11px] text-muted-foreground">
                        {fmtPct(sharePct)}
                      </span>
                    </span>
                  </div>
                  <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-muted/50">
                    <div
                      className={`h-full rounded-full transition-all ${barClass}`}
                      style={{ width: `${barPct}%` }}
                    />
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
