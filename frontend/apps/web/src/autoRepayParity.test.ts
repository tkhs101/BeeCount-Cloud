import * as fs from 'node:fs'
import * as path from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  canAutoRepay,
  repaySourceCandidates,
  sourceAccountIssue,
} from '@beecount/web-features'

/** 自动还款前端的护栏。
 *
 * 手法与 `txSplitsParity.test.ts` 一致:抓的是 **tsc 和 build 都抓不到的**
 * 那类错误 —— 类型完全合法,接线漏了。
 *
 * 后端那条教训(`tax_amount` / `splits` / `autorepay_*` 三次同类反向桥漏读)
 * 说明:新字段最容易漏的不是「值算错」,而是**某条路径根本没带上它**。
 */

const WEB_SRC = __dirname
const FEATURES_SRC = path.resolve(WEB_SRC, '../../../packages/web-features/src')

const _read = (rel: string) =>
  fs.readFileSync(path.resolve(WEB_SRC, rel), 'utf8')

const DIALOG = 'components/dialogs/AccountDetailDialog.tsx'
const DIALOGS = 'components/GlobalEntityDialogs.tsx'

const CARD = {
  id: 'acc-card',
  name: '招行信用卡',
  currency: 'JPY',
  hidden: false,
}
const SAVINGS = {
  id: 'acc-savings',
  name: '招行储蓄卡',
  currency: 'JPY',
  hidden: false,
}

describe('自动还款 —— 能力判定', () => {
  it('只有 credit_card 能绑', () => {
    expect(canAutoRepay({ account_type: 'credit_card', payment_due_day: 25 })).toBe(true)
    for (const t of ['bank_card', 'cash', 'bank_account', 'point_card',
                     'receivable', 'loan', 'investment', 'other']) {
      expect(canAutoRepay({ account_type: t, payment_due_day: 25 })).toBe(false)
    }
  })

  it('没有还款日就不能绑 —— 没有「什么时候还」的锚点', () => {
    // 这条挡的是「用户在 UI 上填完提交才被服务端拒」
    expect(canAutoRepay({ account_type: 'credit_card', payment_due_day: null })).toBe(false)
    expect(canAutoRepay({ account_type: 'credit_card' })).toBe(false)
    expect(canAutoRepay({ account_type: 'credit_card', payment_due_day: 0 })).toBe(false)
    expect(canAutoRepay({ account_type: 'credit_card', payment_due_day: 32 })).toBe(false)
  })

  it('没有绑定的信用卡可AutoRepay —— 含 null account_type', () => {
    expect(canAutoRepay({ account_type: null, payment_due_day: 25 })).toBe(false)
    expect(canAutoRepay({ account_type: '', payment_due_day: 25 })).toBe(false)
  })
})

describe('自动还款 —— 候选扣款账户', () => {
  it('排除卡自己', () => {
    const got = repaySourceCandidates([CARD, SAVINGS], CARD)
    expect(got.map((a) => a.id)).toEqual([SAVINGS.id])
  })

  it('排除跨币种', () => {
    const cny = { id: 'cny', name: '人民币卡', currency: 'CNY', hidden: false }
    const got = repaySourceCandidates([SAVINGS, cny], CARD)
    expect(got.map((a) => a.id)).toEqual([SAVINGS.id])
  })

  it('**保留 hidden 账户**', () => {
    // ⚠️ 这一条是刻意的。`TransactionsPanel` 把 hidden 排除在所有选择器
    // 之外;若沿用那套,已有规则指向一个后来被隐藏的账户时 ——
    // **一条规则在跑,用户在界面上完全看不见**。
    const hiddenAcc = { id: 'h', name: '隐藏卡', currency: 'JPY', hidden: true }
    const got = repaySourceCandidates([SAVINGS, hiddenAcc], CARD)
    expect(got.map((a) => a.id)).toContain('h')
  })

  it('币种缺失时视为不匹配,不做宽松放行', () => {
    const noCur = { id: 'x', name: '无币种', currency: null, hidden: false }
    expect(repaySourceCandidates([noCur], CARD)).toEqual([])
  })
})

describe('自动还款 —— 配置问题可见', () => {
  it('未启用不算问题', () => {
    expect(sourceAccountIssue(
      { enabled: false, from_account_sync_id: null, last_period: null },
      [],
    )).toBe('ok')
  })

  it('启用但没选账户 = missing', () => {
    expect(sourceAccountIssue(
      { enabled: true, from_account_sync_id: null, last_period: null },
      [SAVINGS],
    )).toBe('missing')
  })

  it('扣款账户不在候选里 = missing(账户被删/被隐藏/币种变了)', () => {
    // 这条让 UI 能显示「扣款账户不见了,还款会被跳过」,
    // 而不是只显示「已启用」让用户以为一切正常。
    expect(sourceAccountIssue(
      { enabled: true, from_account_sync_id: 'gone', last_period: null },
      [SAVINGS],
    )).toBe('missing')
  })

  it('正常配置 = ok', () => {
    expect(sourceAccountIssue(
      { enabled: true, from_account_sync_id: SAVINGS.id, last_period: '2026-10' },
      [SAVINGS],
    )).toBe('ok')
  })
})

describe('自动还款 —— 接线护栏', () => {
  it('api-client 读类型声明了三个字段', () => {
    const t = fs.readFileSync(
      path.resolve(WEB_SRC, '../../../packages/api-client/src/types.ts'), 'utf8')
    expect(t).toMatch(/autorepay_enabled\?:\s*boolean \| null/)
    expect(t).toMatch(/autorepay_from_account_sync_id\?:\s*string \| null/)
    expect(t).toMatch(/autorepay_last_period\?:\s*string \| null/)
  })

  it('写 payload 用 `from_account_sync_id` 之外的 sync_id 字段名', () => {
    const t = fs.readFileSync(
      path.resolve(WEB_SRC, '../../../packages/api-client/src/types.ts'), 'utf8')
    // 写路径的键是 `..._id`(请求),读路径是 `..._sync_id`(响应)。
    // 混淆会发一个后端不认的字段名 —— 后端会**静默忽略**它(extra=ignore),
    // 于是配置永远绑不上,且不报错。
    expect(t).toMatch(/autorepay_from_account_id\?:\s*string \| null/)
  })

  it('详情弹窗渲染了自动还款区块', () => {
    const src = _read(DIALOG)
    expect(src).toContain('AutoRepaySection')
    // 只在信用卡 + 有还款日时渲染
    expect(src).toMatch(/isCreditCard && paymentDueDay/)
  })

  it('调用方传了候选账户与保存回调', () => {
    const src = _read(DIALOGS)
    expect(src).toContain('allAccounts')
    expect(src).toContain('onSaveAutoRepay')
    // 候选账户必须真的去取,而不是传空数组
    expect(src).toContain('fetchWorkspaceAccounts')
  })

  it('**真的传了取到的列表**,而不是空数组', () => {
    // 上一版只查 `allAccounts` 字符串存在 —— 改成 `allAccounts={[]}`
    // 照样通过。绑定区在这种情况下永远显示「没有可用的同币种扣款账户」,
    // 而界面看起来一切正常(user 能进区块、能开关,只是选不出账户)。
    const src = _read(DIALOGS)
    expect(src).toMatch(/allAccounts=\{allAccounts\}/)
  })

  it('候选账户是异步取的,取失败时降级为空列表而非抛错', () => {
    const src = _read(DIALOGS)
    expect(src).toMatch(/catch\s*\{/)
    // 取不到就让绑定区显示提示,不弹错误打断用户
    expect(src).not.toMatch(/fetchWorkspaceAccounts[\s\S]{0,400}throw new Error/)
  })

  it('保存走 updateAccount 且带 base_change_id', () => {
    const src = _read(DIALOGS)
    expect(src).toMatch(/updateAccount\(/)
    expect(src).toMatch(/autorepay_enabled:\s*patch\.enabled/)
    expect(src).toMatch(/autorepay_from_account_id:\s*patch\.from_account_sync_id/)
  })

  it('暂停保留配置,不发送 null 覆盖', () => {
    // 「这个月先不还」= 关掉,不是删掉重填。若暂停时把
    // from_account_sync_id 发成 null,用户下次启用要重选。
    const src = _read(DIALOG)
    expect(src).toMatch(
      /enabled\s*\?\s*\(card\.autorepay_from_account_sync_id \?\? null\)/,
    )
  })

  it('i18n 三语都有 detail.autoRepay.*', () => {
    for (const f of ['en.ts', 'zh-CN.ts', 'zh-TW.ts']) {
      const src = _read(path.join('i18n', f))
      const keys = src.match(/'detail\.autoRepay\.\w+'/g) || []
      expect(keys.length, `${f} 缺 detail.autoRepay.*`).toBeGreaterThanOrEqual(9)
    }
  })
})

describe('自动还款 —— 保存后的状态闭环(P0-1)', () => {
  const DIALOGS = 'components/GlobalEntityDialogs.tsx'

  it('保存成功必须**同时**更新弹窗正在读的那个 state', () => {
    // 抓到过真 bug:PATCH 200、库里真的生效了,而按钮仍显示「开启」。
    // 根因是 `saveAutoRepay` 只 `setAllAccounts`(喂候选下拉),
    // 而弹窗读的是 `account` —— 那个 state 只在 `onOpenDetailAccount`
    // 事件里被 set 过一次,全文件没有别的刷新机制。
    //
    // 用户看到的就是**点了没反应**,会再点一次、再点一次,
    // 然后判定功能坏了。而后端每月 25 号真的会扣钱。
    const src = _read(DIALOGS)
    expect(src).toMatch(/setAllAccounts\(/)
    expect(src).toMatch(/setAccount\(\(prev\)/)
    // 两个都要在 saveAutoRepay 的函数体内 —— 光「文件里有 setAccount」
    // 不够:那个 event handler 里本来就有一次,断言不到点上。
    const fn = src.slice(
      src.indexOf('const saveAutoRepay'),
      src.indexOf('const saveAutoRepay') + 1600,
    )
    expect(fn).toContain('setAllAccounts')
    expect(fn).toContain('setAccount((prev) => (prev && prev.id === target.id')
  })

  it('更新的是同一个目标账户,不是整份列表刷新', () => {
    // 整份重取会让弹窗里的分页/排序状态被打断,而这里只需要改一行。
    const src = _read(DIALOGS)
    const fn = src.slice(
      src.indexOf('const saveAutoRepay'),
      src.indexOf('const saveAutoRepay') + 1600,
    )
    expect(fn).toContain('prev.id === target.id')
  })
})


describe('自动还款 —— 手动触发必须有前端入口', () => {
  const _read = (rel: string) =>
    fs.readFileSync(path.resolve(__dirname, rel), 'utf8')

  it('端点是从 Web 真能调到的,不是死代码', () => {
    // 后端 `POST /write/ledgers/{id}/accounts/{id}/autorepay/run` 为它专门
    // 设计过错误码(failed→502 / 配错→400),理由是「用户看到的是点了没反应」。
    // 但 Web 上一直没有按钮 —— 那些错误码从未被用户见过。
    const src = _read('components/GlobalEntityDialogs.tsx')
    expect(src).toMatch(/autorepay\/run/)
    expect(src).toMatch(/method: 'POST'/)
  })

  it('结果必须显示给用户,不能只 setState 不渲染', () => {
    // 我第一版写了 `setRunResult(...)` 却没有任何地方渲染它 ——
    // compile 全过、测试全绿,而用户点了按钮依然什么都看不到。
    // 这跟后端那条「失败被吞成 HTTP 200」是同一枚硬币。
    const src = _read('components/GlobalEntityDialogs.tsx')
    expect(src).toContain('autoRepayRunResult={runResult}')

    const dlg = _read('components/dialogs/AccountDetailDialog.tsx')
    expect(dlg).toContain('runResult={autoRepayRunResult}')
    // 渲染出口
    expect(dlg).toMatch(/\{runResult \? \(/)
  })

  it('「立即试跑」按钮只在已启用时可点', () => {
    // 没启用就试跑只会得到 skipped_disabled —— 点了没反应,
    // 用户又以为是 bug。
    const dlg = _read('components/dialogs/AccountDetailDialog.tsx')
    expect(dlg).toMatch(/disabled=\{busy \|\| !enabled\}/)
  })

  it('i18n 三语都有 detail.autoRepay.runNow', () => {
    for (const f of ['en.ts', 'zh-CN.ts', 'zh-TW.ts']) {
      const src = _read(path.join('i18n', f))
      expect(src, `${f} 缺 detail.autoRepay.runNow`)
        .toMatch(/'detail\.autoRepay\.runNow'/)
    }
  })
})
