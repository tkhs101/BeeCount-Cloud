import { useEffect, useState } from 'react'

/**
 * 是否命中 `prefers-reduced-motion`。
 *
 * ## 为什么不用 framer-motion 的 `useReducedMotion`
 *
 * 那个钩子的实现本身就是一行 `matchMedia('(prefers-reduced-motion: reduce)')`
 * —— 而 `Amount.tsx` 为了这一行,把 **126 kB 的动画引擎**拖进了
 * **入口的静态 import 图**。Vite 只给「入口静态依赖」的 chunk 发
 * `modulepreload`,于是 framer-motion 被列为**首屏下载**(2026-10-12 实测:
 * 首屏预取 382 kB 里有它 126 kB)。
 *
 * `Amount` 是全站所有金额文案都走的组件,几乎每个页面都要用 ——
 * 它不该是拖着一个动画引擎进首屏的那个。
 *
 * 语义与 framer-motion 的实现**完全一致**(它内部也是 matchMedia),
 * 差别只在**少了服务端渲染的默认值处理** —— 本项目是纯客户端 SPA,
 * 没有 SSR。
 */
export const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)'

/** 首次读取。SSR / 老浏览器一律当「不减弱」。 */
export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false
  return window.matchMedia(REDUCED_MOTION_QUERY).matches
}

/**
 * 订阅变化,返回取消订阅函数。
 *
 * ⚠️ **老 WebKit 的 `addListener` 兜底不是历史包袱,是必需的分支** ——
 * 删掉它,旧 Safari 上「用户在系统里改了减弱动画」就静默不再生效,
 * 而且**没有任何报错**。
 * 提出来单独导出就是为了让这条分支能被 `useReducedMotion.behavior.test.ts`
 * 真跑(钩子本身要 React 渲染环境,仓里没有 jsdom)。
 */
export function subscribeReducedMotion(
  onChange: (reduced: boolean) => void
): () => void {
  const noop = () => {}
  if (typeof window === 'undefined' || !window.matchMedia) return noop
  const mq = window.matchMedia(REDUCED_MOTION_QUERY)
  const handler = (e: MediaQueryListEvent) => onChange(e.matches)
  // 老 WebKit 只有 addListener;两边都挂上,重复挂载时 addEventListener 优先
  if (mq.addEventListener) {
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }
  mq.addListener(handler)
  return () => mq.removeListener(handler)
}

export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(prefersReducedMotion)

  useEffect(() => subscribeReducedMotion(setReduced), [])

  return reduced
}