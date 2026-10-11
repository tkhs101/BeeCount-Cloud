import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
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
// SRC = <frontend>/apps/web/src  ->  上三级 = <frontend>，其下有 packages/
const FRONTEND_ROOT = join(SRC, '..', '..', '..')
const PACKAGES_ROOT = join(FRONTEND_ROOT, 'packages')

/** 要扫的根目录 —— ⚠️ **两个都要**,少一个就会漏。 */
const ROOTS = [SRC, PACKAGES_ROOT]

/**
 * ⚠️ **根目录必须真的存在**。
 *
 * 第一版把 `join(WEB_ROOT, '..', '..', 'packages')` 写成了 `../packages`
 * (仓库根),运行时直接 ENOENT —— 而 ENOENT 是在 **collect 阶段**炸的,
 * 输出是 `Tests  no tests`,**看起来像「没发现任何问题」**。
 *
 * 所以这里在模块加载时就断言,让它变成一条**有名有姓的失败**。
 */
if (!existsSync(PACKAGES_ROOT)) {
  throw new Error(
    `PACKAGES_ROOT 不存在: ${PACKAGES_ROOT}\n` +
      '  -> 路径算错了。新增 workspace 包时要同步改这里,否则又是一个洞。',
  )
}

/** 遍历 .ts / .tsx。 */
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
  for (const root of ROOTS) {
    // 📌 **第一版只扫了 `apps/web/src`**,于是 `packages/web-features` 里的
    // 4 个键(`txFlagExcludeFromStats` 等)**整整漏了一轮**。
    // 它们是交易面板上真实会渲染的标签 —— 用户看到的是字面的 key 名。
    //
    // ⚠️ 所以**根目录列表本身就是契约的一部分**:
    // 新增一个 workspace 包,必须同时加进 `ROOTS`,否则又是一个洞。
    for (const file of walk(root)) {
      const src = readFileSync(file, 'utf8')
      for (const m of src.matchAll(/\bt\(\s*'([\w.]+)'/g)) used.add(m[1])
      for (const m of src.matchAll(/\bt\(\s*"([\w.]+)"/g)) used.add(m[1])
    }
  }
  return [...used]
}

/** `en.ts` 里定义的所有键。 */
function keysDefined(): Set<string> {
  const src = readFileSync(join(SRC, 'i18n', 'en.ts'), 'utf8')
  // 📌 **两种写法都要认**:
  //   `'some.key': 'v'`   (多数)
  //   `some.key: 'v'`     (不带引号 —— 「账单标记」那一块就是这么写的)
  //
  // 第一版只认带引号的,于是 `txFlagExcludeFromStats` 等四个被当成「缺失」,
  // 我又加了一遍 —— 被 `tsc` 的 **TS1117**(重复键)当场抓住。
  // **vitest 绿、tsc 红** —— 这正是「前端两道门独立」的又一个实例。
  const keys = new Set<string>()
  for (const m of src.matchAll(/^ {2}'([\w.]+)':/gm)) keys.add(m[1])
  for (const m of src.matchAll(/^ {2}([a-z][\w.]*) *:/gm)) keys.add(m[1])
  // ⚠️ **上面的 `[\w.]` 里的点号不能省**。第一版写成 `([a-z][\w]*)`(不允许点),
  // 于是 `currency.placeholder` 这种「不带引号**且带点**」的键抓不到 ——
  // 而它恰恰是「只有 packages/ 在用」那条断言的探针,结果打过去的变异**没红**。
  //
  // 教训:**断言的探针对象,必须先确认判据真能认出它**。
  return keys
}

const USED = keysUsedInCode()
const DEFINED = keysDefined()

describe('i18n 键覆盖(代码用到 => en.ts 必须定义)', () => {
  it(`扫到了足够的键(>= ${1200})(否则下面全绿等于没扫)`, () => {
    // ⚠️ **下限自检**:正则失效 / 目录变了导致扫到 0 个时,
    // 「没发现问题」不能等于「通过」。
    expect(USED.length).toBeGreaterThanOrEqual(1200)
    expect(DEFINED.size).toBeGreaterThanOrEqual(1000)
  })

  it('**`packages/` 确实被扫到了** —— 第一版漏了整个目录', () => {
    // 📌 这条是**针对具体事故**写的:第一版 `ROOTS` 只有 `apps/web/src`,
    // `packages/web-features` 里的键整整漏了一轮。
    //
    // 光有「总数 >= N」不够 —— 少扫一个目录也可能仍然超过下限。
    // 所以**直接断言一个只有 packages/ 才会用到的键在里面**。
    //
    // ⚠️ 用 `currency.placeholder` —— 实测它**只在 `packages/web-features` 里被调用**
    // (`apps/web/src` 命中 0 次),所以「它出现在 USED 里」就等于「packages/ 被扫到了」。
    // 选它是因为它**确实存在于 en.ts**,断言里硬编一个自己以为存在的键名
    // 会让这条测试变成一次「我的猜测对不对」的测验(我第一次就猜错了)。
    expect(USED).toContain('currency.placeholder')
    // 同一个键**也必须能被查到定义** —— 「扫到了」和「认得出」是两件事
    expect(DEFINED.has('currency.placeholder')).toBe(true)
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