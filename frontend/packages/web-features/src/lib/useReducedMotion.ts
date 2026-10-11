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
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return false
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  })

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches)
    // 老 WebKit 只有 addListener;两边都挂上,重复挂载时 addEventListener 优先
    if (mq.addEventListener) {
      mq.addEventListener('change', onChange)
      return () => mq.removeEventListener('change', onChange)
    }
    mq.addListener(onChange)
    return () => mq.removeListener(onChange)
  }, [])

  return reduced
}