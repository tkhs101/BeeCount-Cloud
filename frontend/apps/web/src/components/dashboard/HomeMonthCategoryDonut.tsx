import { useMemo } from 'react'
import { Card, CardContent, CardHeader, CardTitle, useT } from '@beecount/ui'

import type { WorkspaceAnalyticsCategoryRank } from '@beecount/api-client'
import { Amount } from '@beecount/web-features'

import { TAX_CATEGORY_NAME } from '../../lib/taxCategory'

interface Props {
  /** 本月支出类别排行（scope=month&metric=expense 返回的 category_ranks）。 */
  ranks: WorkspaceAnalyticsCategoryRank[]
  currency?: string
}

/**
 * 本月支出分类占比环。SVG conic-gradient 做分段，Top 5 各一段，之外合并为
 * "其他"。参考 `AccountsPanel.AssetsCompositionMini`，同样风格避免引入 recharts
 * 分段饼图的多余依赖。
 */
export type DonutSlice = { name: string; total: number; count: number }

/**
 * 扇区上限从 5 提到 8 —— 与官方 App 的 `category_pie_chart._maxSlices = 8`
 * 对齐。理由不只是「好看」:
 *
 * **消费税扇区的金额天然很小**(日本月消费税通常几千日元),而扇区是按金额
 * 排序取的。停在 5 的话,它会排在餐饮/住房/交通/购物之后,自己掉进灰色
 * 「其他」块 —— 那样「饼图上单独看到我交了多少钱税」这个核心诉求就直接落空。
 * 8 个扇区 + 灰色「其他」仍然可读(右侧是完整图例列表,不靠扇区本身辨色)。
 */
export const MAX_SLICES_DEFAULT = 8

/**
 * 把分类排行切成饼图扇区:金额降序取前 `max` 个,其余合并成「其他」。
 *
 * 抽成纯函数是为了能测 —— 这段逻辑正是「消费税能不能露出来」的决定点,
 * 藏在 useMemo 里就只能靠肉眼看图形。测试见
 * `src/components/dashboard/donutSlices.test.ts`。
 */
export function buildDonutSlices(
  ranks: Array<{ category_name: string; total: number; tx_count: number }>,
  otherLabel: string,
  uncategorizedLabel: string,
  max: number = MAX_SLICES_DEFAULT,
  /** 这些分类**永远单独成扇区**,不参与金额排名竞争。 */
  alwaysShow: string[] = []
): DonutSlice[] {
  const sorted = ranks
    .slice()
    .sort((a, b) => b.total - a.total)
    .filter((r) => r.total > 0)
  // 钉住:先划走 alwaysShow,剩下的槽位给普通分类。
  //
  // 光把上限从 5 提到 8 **不够** —— 消费税扇区的金额天然最小(日本月消费税
  // 通常几千日元),只要普通分类超过 7 个,它照样排在第 8 位之后被并进「其他」。
  // 排名制对「天生就小的扇区」本质上不可靠,所以这里改成显式钉住。
  const pinned = sorted.filter((r) => alwaysShow.includes(r.category_name))
  const rankable = sorted.filter((r) => !alwaysShow.includes(r.category_name))
  const top = rankable.slice(0, Math.max(0, max - pinned.length))
  const rest = rankable.slice(Math.max(0, max - pinned.length))
  const restTotal = rest.reduce((s, r) => s + r.total, 0)
  const restCount = rest.reduce((s, r) => s + r.tx_count, 0)
  const all: DonutSlice[] = [...pinned, ...top]
    .sort((a, b) => b.total - a.total)
    .map((r) => ({
      name: r.category_name || uncategorizedLabel,
      total: r.total,
      count: r.tx_count
    }))
  if (restTotal > 0) {
    all.push({ name: otherLabel, total: restTotal, count: restCount })
  }
  return all
}

/** 饼图调色盘(Tailwind-500 系),与 BeeCount mobile 常用色保持同一视觉家族。 */
const CHART_PALETTE = [
  '#ef4444', '#f59e0b', '#3b82f6', '#10b981', '#a855f7',
  '#06b6d4', '#ec4899', '#84cc16'
]
/** 「其他」合并块专用中性灰 —— 不参与 PALETTE 轮转。 */
const OTHER_SLICE_COLOR = '#94a3b8'

export function HomeMonthCategoryDonut({ ranks, currency = 'CNY' }: Props) {
  const t = useT()
  const otherLabel = t('home.monthDonut.other')
  const slices = useMemo(
    () =>
      buildDonutSlices(
        ranks,
        otherLabel,
        t('home.monthDonut.uncategorized'),
        MAX_SLICES_DEFAULT,
        // 消费税扇区钉住 —— 它金额最小,靠排名永远争不过日常分类,
        // 而它恰恰是用户最想看到的那一块。
        [TAX_CATEGORY_NAME]
      ),
    [ranks, otherLabel, t]
  )
  const total = useMemo(() => slices.reduce((s, r) => s + r.total, 0), [slices])

  const PALETTE = CHART_PALETTE
  const OTHER_COLOR = OTHER_SLICE_COLOR

  const conic = useMemo(() => {
    if (total <= 0) return 'hsl(var(--muted))'
    let acc = 0
    const stops: string[] = []
    slices.forEach((s, i) => {
      const color = s.name === otherLabel ? OTHER_COLOR : PALETTE[i % PALETTE.length]
      const start = (acc / total) * 100
      acc += s.total
      const end = (acc / total) * 100
      stops.push(`${color} ${start.toFixed(3)}% ${end.toFixed(3)}%`)
    })
    return `conic-gradient(from -90deg, ${stops.join(',')})`
  }, [slices, total, otherLabel])

  return (
    <Card className="bc-panel overflow-hidden">
      <CardHeader className="flex flex-row items-end justify-between">
        <CardTitle className="text-base">{t('home.monthDonut.title')}</CardTitle>
        <span className="text-[11px] text-muted-foreground">
          {t('home.monthDonut.total')}{' '}
          <Amount
            value={total}
            currency={currency}
            size="xs"
            tone="negative"
            bold
            className="inline"
          />
        </span>
      </CardHeader>
      <CardContent>
        {slices.length === 0 ? (
          <div className="flex h-48 items-center justify-center text-xs text-muted-foreground">
            {t('home.monthDonut.empty')}
          </div>
        ) : (
          <div className="flex items-center gap-5">
            <div className="relative h-40 w-40 shrink-0">
              <div
                className="absolute inset-0 rounded-full"
                style={{ background: conic }}
                aria-hidden
              />
              <div className="absolute inset-[18%] rounded-full bg-card" aria-hidden />
              <div className="absolute inset-0 flex flex-col items-center justify-center">
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  {t('home.monthDonut.center')}
                </div>
                <Amount
                  value={total}
                  currency={currency}
                  size="sm"
                  bold
                  tone="negative"
                  className="mt-0.5"
                />
                <div className="mt-0.5 text-[10px] text-muted-foreground">
                  {t('home.monthDonut.categoryCount').replace('{count}', String(slices.length))}
                </div>
              </div>
            </div>
            <ul className="min-w-0 flex-1 space-y-1.5">
              {slices.map((s, i) => {
                const color =
                  s.name === otherLabel ? OTHER_COLOR : PALETTE[i % PALETTE.length]
                const pct = total > 0 ? (s.total / total) * 100 : 0
                return (
                  <li key={`${s.name}-${i}`} className="flex items-center gap-2 text-xs">
                    <span
                      className="h-2.5 w-2.5 shrink-0 rounded-sm"
                      style={{ background: color }}
                      aria-hidden
                    />
                    <span className="flex-1 truncate">{s.name}</span>
                    <span className="text-muted-foreground font-mono tabular-nums">
                      {pct.toFixed(1)}%
                    </span>
                    <Amount
                      value={s.total}
                      currency={currency}
                      size="xs"
                      className="w-20 text-right"
                    />
                  </li>
                )
              })}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
