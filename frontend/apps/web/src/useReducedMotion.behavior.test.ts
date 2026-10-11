import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  REDUCED_MOTION_QUERY,
  prefersReducedMotion,
  subscribeReducedMotion
} from '@beecount/web-features'

/**
 * `useReducedMotion` 的行为护栏(2026-10-12 补)。
 *
 * 这个钩子是为了把 126 kB 的 framer-motion 从**首屏**里换掉才写的
 * (见 `CLAUDE.md` 「换原生 matchMedia」那条)。它本身要 React 渲染环境,
 * 仓里没有 jsdom —— 所以按「纯逻辑提到模块级就能真跑」的做法,
 * 把两个关键函数提取成导出(`prefersReducedMotion` / `subscribeReducedMotion`),
 * 在这里**真跑**它们。
 *
 * ⚠️ 真正要锁的是**老 WebKit 的 `addListener` 兜底**:删掉它,
 * 旧 Safari 上用户改「减弱动画」就静默失效,**没有任何报错**。
 */

type FakeMql = {
  matches: boolean
  addEventListener?: ReturnType<typeof vi.fn>
  removeEventListener?: ReturnType<typeof vi.fn>
  addListener?: ReturnType<typeof vi.fn>
  removeListener?: ReturnType<typeof vi.fn>
  fire: (matches: boolean) => void
}

/**
 * ⚠️ vitest 没开 jsdom,**`window` 本身不存在** —— 只 stub `matchMedia` 不够,
 * 被测代码第一行 `typeof window === 'undefined'` 就早退了(我第一版就栽在这,
 * 症状是「stub 了却拿到 false」)。两个都得 stub。
 */
function stubMatchMedia(mql: FakeMql | null): void {
  vi.stubGlobal('window', mql ? { matchMedia: () => mql } : {})
  vi.stubGlobal('matchMedia', mql ? () => mql : undefined)
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('prefersReducedMotion', () => {
  it('用正确的媒体查询串', () => {
    const seen: string[] = []
    vi.stubGlobal('window', {
      matchMedia: (q: string) => {
        seen.push(q)
        return { matches: true }
      }
    })
    expect(prefersReducedMotion()).toBe(true)
    expect(seen).toEqual(['(prefers-reduced-motion: reduce)'])
    expect(REDUCED_MOTION_QUERY).toBe('(prefers-reduced-motion: reduce)')
  })

  it('matchMedia 不存在时一律 false(老浏览器/SSR 不该炸)', () => {
    stubMatchMedia(null)
    expect(prefersReducedMotion()).toBe(false)
  })
})

describe('subscribeReducedMotion', () => {
  it('优先走 addEventListener,并能取消订阅', () => {
    const removeEventListener = vi.fn()
    const mql: FakeMql = {
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener,
      fire: () => {}
    }
    stubMatchMedia(mql)

    const seen: boolean[] = []
    const stop = subscribeReducedMotion((v) => seen.push(v))
    expect(mql.addEventListener).toHaveBeenCalledWith('change', expect.any(Function))

    mql.fire?.(true)
    stop()
    expect(removeEventListener).toHaveBeenCalledWith('change', expect.any(Function))
    expect(seen.length).toBeLessThanOrEqual(1)
  })

  it('⚠️ 没有 addEventListener 时必须退回老 API 的 addListener', () => {
    const removeListener = vi.fn()
    const mql: FakeMql = {
      matches: false,
      // 故意不提供 addEventListener —— 老 WebKit 就是这个形状
      addListener: vi.fn(),
      removeListener,
      fire: () => {}
    }
    stubMatchMedia(mql)

    const seen: boolean[] = []
    const stop = subscribeReducedMotion((v) => seen.push(v))

    // 退到 addListener 才算这条分支真的还在
    expect(mql.addListener).toHaveBeenCalledTimes(1)
    const handler = mql.addListener!.mock.calls[0][0] as (e: { matches: boolean }) => void
    handler({ matches: true })
    expect(seen).toEqual([true])

    stop()
    expect(removeListener).toHaveBeenCalledTimes(1)
  })

  it('matchMedia 不存在时返回可调用的 no-op(不能是 undefined)', () => {
    stubMatchMedia(null)
    const stop = subscribeReducedMotion(() => {})
    expect(typeof stop).toBe('function')
    expect(() => stop()).not.toThrow()
  })
})