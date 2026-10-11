import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

/**
 * **代码里 `t('...')` 用到的 key,必须在 `en.ts` 里定义。**
 *
 * ## 它防什么(一条真实发生过的)
 *
 * `GlobalEditDialogs.tsx:324/329` 调用了 `t('notice.transactionUpdated')` 和
 * `t('notice.transactionCreated')`,而**三个词典里都没有这两个键**。
 *
 * 因为 `t()` 是 `active[key] || fallback[key] || key` —— **词典缺键 = 把原始
 * key 名直接显示给用户**:
 *
 * > `notice.transactionCreated`
 *
 * 没有任何报错、没有控制台警告、**vitest 全绿、tsc 0 错**。
 * 这正是 CLAUDE.md 里「前后端字段名契约」那一族的**i18n 版本**。
 *
 * ## 为什么 `en` 必须是判据
 *
 * 因为 `fallback` 就是 `dictionaries.en`(CLAUDE.md 明确写了「`en` 必须静态
 * 打包」)。`en` 缺一个键,任何语言都会漏出原始 key —— 所以拿 `en` 当判据,
 * 一道测试就覆盖全部三种语言。
 *
 * ## 为什么不用现成的 parity 测试
 *
 * `autoRepayParity.test.ts` 校验的是「**这个键在三种语言里都存在**」。
 * 但本例里**三个词典都缺** —— parity 测试**全绿**。
 * 它拦得住「加键时忘了同步语言」,拦不住「**加了调用却没加键**」。
 * **两个方向,得两个判据。**
 */

const SRC = dirname(fileURLToPath(import.meta.url))

/** 遍历 `src/` 下的 .ts / .tsx。 */
function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      if (entry !== 'i18n' && entry !== 'node_modules') walk(full, out)
    } else if (/\.tsx?$/.test(entry)) {
      // ⚠️ **跳过测试文件本身** —— 它在注释和断言里写了 `t('...')` 的**示例**,
      // 那些不是真的调用。不排除的话会得到一条恒定的假阳性,
      // 而**恒定的假阳性会让人习惯性忽略整条报错**,比没有护栏更糟。
      if (!/\.test\.tsx?$/.test(entry)) out.push(full)
    }
  }
  return out
}

/** 代码里所有 `t('...')` / `t("...")` 的实参。 */
function keysUsedInCode(): string[] {
  const used = new Set<string>()
  for (const file of walk(SRC)) {
    const src = readFileSync(file, 'utf8')
    for (const m of src.matchAll(/\bt\(\s*'([\w.]+)'/g)) used.add(m[1])
    for (const m of src.matchAll(/\bt\(\s*"([\w.]+)"/g)) used.add(m[1])
  }
  return [...used]
}

/** `en.ts` 里定义的所有键。 */
function keysDefined(): Set<string> {
  const src = readFileSync(join(SRC, 'i18n', 'en.ts'), 'utf8')
  return new Set([...src.matchAll(/^ {2}'([\w.]+)':/gm)].map((m) => m[1]))
}

const USED = keysUsedInCode()
const DEFINED = keysDefined()

describe('i18n 键覆盖(代码用到 => en.ts 必须定义)', () => {
  it(`扫到了足够的键(>= ${800})(否则下面全绿等于没扫)`, () => {
    // ⚠️ **下限自检**:正则失效 / 目录变了导致扫到 0 个时,
    // 「没发现问题」不能等于「通过」。
    expect(USED.length).toBeGreaterThanOrEqual(800)
    expect(DEFINED.size).toBeGreaterThanOrEqual(1000)
  })

  it('代码用到的每个 t(...) 键在 en.ts 里都有定义', () => {
    const missing = USED.filter((k) => !DEFINED.has(k)).sort()
    expect(
      missing,
      missing.length
        ? `${missing.length} 个键被 t() 调用但 en.ts 没定义 —— 用户会看到原始 key 名:\n  ` +
          missing.join('\n  ')
        : '',
    ).toEqual([])
  })
})