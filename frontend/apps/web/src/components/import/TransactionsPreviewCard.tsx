import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  useT,
} from '@beecount/ui'
import type { ImportTransactionSample } from '@beecount/api-client'

interface Props {
  samples: ImportTransactionSample[]
  totalRows: number
}

/**
 * 解析后的前 10 笔交易预览 —— 让用户在确认之前能看到"长这样"。
 *
 * 不满意 → 点顶部「编辑映射」改 mapping → 重 preview → 这里跟着变。
 */
export function TransactionsPreviewCard({ samples, totalRows }: Props) {
  const t = useT()

  // 两列按数据有无出现 —— 常量列会把预览表撑宽,而绝大多数 CSV 既没有
  // 税额也没有拆分。**只扫前几十行样本**:全量扫一遍为两个布尔值不划算,
  // 而样本之后没有的数据本来也看不到。
  const hasTax = samples.some((tx) =>
    tx.tax_amount != null && tx.tax_amount > 0,
  )
  const hasSplits = samples.some((tx) => (tx.splits?.length ?? 0) >= 2)
  return (
    <Card className="bc-panel">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="text-sm">{t('import.preview.title')}</CardTitle>
        <span className="text-[11px] text-muted-foreground">
          {t('import.preview.showing', {
            shown: samples.length,
            total: totalRows,
          })}
        </span>
      </CardHeader>
      <CardContent className="p-0">
        {samples.length === 0 ? (
          <p className="px-4 py-8 text-center text-xs text-muted-foreground">
            {t('import.preview.empty')}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px]">
              <thead className="bg-muted/30 text-[10px] uppercase tracking-wider text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left">#</th>
                  <th className="px-3 py-2 text-left">{t('import.preview.col.time')}</th>
                  <th className="px-3 py-2 text-left">{t('import.preview.col.type')}</th>
                  <th className="px-3 py-2 text-right">{t('import.preview.col.amount')}</th>
                  <th className="px-3 py-2 text-left">{t('import.preview.col.category')}</th>
                  <th className="px-3 py-2 text-left">{t('import.preview.col.account')}</th>
                  {/* 税额 / 组合支付两列**按数据有无**显示。

                      常量列会把预览表撑宽,而绝大多数 CSV 既没有税额也没有
                      拆分。所以只在样本里真的出现时才加。

                      没有 splits 列时用户**完全看不出**这笔会被拆成几个账户,
                      保存后才发现父交易的「账户」是空的 —— 那正是本 fork
                      记录过的静默账目损坏。 */}
                  {hasTax ? (
                    <th className="px-3 py-2 text-right">
                      {t('import.preview.col.tax')}
                    </th>
                  ) : null}
                  {hasSplits ? (
                    <th className="px-3 py-2 text-left">
                      {t('import.preview.col.splits')}
                    </th>
                  ) : null}
                  <th className="px-3 py-2 text-left">{t('import.preview.col.tags')}</th>
                  <th className="px-3 py-2 text-left">{t('import.preview.col.note')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/40">
                {samples.map((tx, idx) => (
                  <tr key={idx} className="hover:bg-muted/20">
                    {hasTax ? (
                      <td className="px-3 py-1.5 text-right tabular-nums">
                        {tx.tax_amount != null && tx.tax_amount > 0
                          ? tx.tax_amount.toFixed(2)
                          : '—'}
                      </td>
                    ) : null}
                    {hasSplits ? (
                      <td className="px-3 py-1.5">
                        {tx.splits && tx.splits.length >= 2
                          ? tx.splits.map(([name, amount], i) => (
                              <span key={i} className="mr-1 whitespace-nowrap">
                                {name}:{amount}
                                {i < (tx.splits?.length ?? 0) - 1
                                  ? ' |'
                                  : ''}
                              </span>
                            ))
                          : '—'}
                      </td>
                    ) : null}
                    <td className="px-3 py-1.5 text-muted-foreground">
                      L{tx.source_row_number}
                    </td>
                    <td className="px-3 py-1.5 font-mono">{formatDate(tx.happened_at)}</td>
                    <td className="px-3 py-1.5">
                      <TypeChip type={tx.tx_type} />
                    </td>
                    <td className={`px-3 py-1.5 text-right font-mono tabular-nums ${
                      tx.tx_type === 'expense'
                        ? 'text-expense'
                        : tx.tx_type === 'income'
                          ? 'text-income'
                          : ''
                    }`}>
                      {tx.tx_type === 'expense' ? '−' : tx.tx_type === 'income' ? '+' : ''}
                      {formatAmount(tx.amount)}
                    </td>
                    <td className="px-3 py-1.5">
                      {tx.parent_category_name ? (
                        <span className="text-muted-foreground">
                          {tx.parent_category_name} /
                        </span>
                      ) : null}
                      {tx.category_name || ''}
                    </td>
                    <td className="px-3 py-1.5">
                      {tx.tx_type === 'transfer'
                        ? `${tx.from_account_name || '-'} → ${tx.to_account_name || '-'}`
                        : tx.account_name || ''}
                    </td>
                    <td className="px-3 py-1.5">
                      {tx.tag_names.length > 0 ? (
                        <span className="flex flex-wrap gap-1">
                          {tx.tag_names.map((tag) => (
                            <span
                              key={tag}
                              className="rounded border border-border/60 px-1 py-0.5 text-[10px] text-muted-foreground"
                            >
                              {tag}
                            </span>
                          ))}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-3 py-1.5 max-w-[200px] truncate text-muted-foreground" title={tx.note || ''}>
                      {tx.note || ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function TypeChip({ type }: { type: string }) {
  const t = useT()
  const classes =
    type === 'expense'
      ? 'border-expense/40 text-expense'
      : type === 'income'
        ? 'border-income/40 text-income'
        : 'border-border/60 text-muted-foreground'
  return (
    <span className={`inline-block rounded border px-1.5 text-[10px] ${classes}`}>
      {t(`enum.txType.${type}`)}
    </span>
  )
}

function formatDate(iso: string): string {
  try {
    const d = new Date(iso)
    const yyyy = d.getFullYear()
    const mm = String(d.getMonth() + 1).padStart(2, '0')
    const dd = String(d.getDate()).padStart(2, '0')
    const hh = String(d.getHours()).padStart(2, '0')
    const mi = String(d.getMinutes()).padStart(2, '0')
    return `${yyyy}-${mm}-${dd} ${hh}:${mi}`
  } catch {
    return iso
  }
}

function formatAmount(raw: string): string {
  const n = Number(raw)
  if (Number.isNaN(n)) return raw
  return Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}
