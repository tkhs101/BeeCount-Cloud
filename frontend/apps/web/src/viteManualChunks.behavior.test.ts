import { describe, expect, it } from 'vitest'

import viteConfig from '../vite.config'

/**
 * `manualChunks` 的**行为**护栏(2026-10-12 补)。
 *
 * ## 为什么这条值得单独测
 *
 * `vite.config.ts` 里那段「⚠️ **不要**给 recharts 配 manualChunks」的注释,
 * 记录着一次**实测省下 399 kB** 的改动。但注释不强制任何东西 ——
 * 有人加回 `if (id.includes('recharts')) return 'recharts'`,
 * 首屏就涨回 409 kB,而 `build` **照样成功**、`tsc` 照样 0 错、
 * **所有测试照样全绿**。这正是 bundle 那几轮反复出现的失败模式:
 * 「产物变了、但没有任何一道门会红」。
 *
 * ## 判据:真调这个函数,而不是 grep 配置文本
 *
 * ⚠️ 写成 `expect(src).not.toContain('recharts')` 是**假护栏** ——
 * 注释里就写着 recharts(CLAUDE.md 记的「前端 45% 的护栏是假的」那一族)。
 * 所以这里 import 真实的 config,用**真实的 node_modules 路径**调用
 * `manualChunks`,断言它的返回值。
 */

/** pnpm workspace 下真实的模块 id 形状 */
const NM = '/repo/frontend/node_modules/.pnpm'

function chunkFor(id: string): string | false | undefined {
  const fn = viteConfig.build?.rollupOptions?.output?.manualChunks
  if (typeof fn !== 'function') return undefined
  return (fn as (id: string) => string | undefined)(id)
}

describe('manualChunks 不得把懒加载库提进入口静态图', () => {
  it('recharts 不能被指定 chunk —— 会被 modulepreload 成首屏资源', () => {
    // 这三个都是 recharts 及其 d3 依赖的真实 id 形状
    const ids = [
      `${NM}/recharts@2.12.7/node_modules/recharts/es6/index.js`,
      `${NM}/recharts@2.12.7/node_modules/recharts/lib/chart/generateCategoricalChart.js`,
      `${NM}/d3-scale@4.0.2/node_modules/d3-scale/src/linear.js`
    ]
    for (const id of ids) {
      expect(chunkFor(id), `manualChunks 命中了 ${id}`).toBeUndefined()
    }
  })

  it('react 核心仍然进 react-vendor(长期可缓存,别一起删掉)', () => {
    // 上一条只说「别加 recharts」,不是「把 manualChunks 全删了」
    expect(chunkFor(`${NM}/react@18.3.1/node_modules/react/index.js`)).toBe(
      'react-vendor'
    )
  })

  it('应用自己的源码永远返回 undefined(交给 rollup 自己分块)', () => {
    expect(chunkFor('/repo/frontend/apps/web/src/main.tsx')).toBeUndefined()
  })
})