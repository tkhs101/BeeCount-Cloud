import { describe, expect, it } from 'vitest'

import { initialLocale } from '@beecount/ui'

import en from './i18n/en'
import { initialDictionaries, loadDictionary, prepareInitialDictionary } from './i18n/index'
import zhCN from './i18n/zh-CN'
import zhTW from './i18n/zh-TW'

/**
 * i18n 按需加载的行为护栏(2026-10-12 补)。
 *
 * 上一版 `i18n.test.ts` **静态导入三个词典**,按需加载那条路一条都没测 ——
 * 而那正是首屏省 164 kB 的实现。
 *
 * ⚠️ 这两条最要紧,它们各自对应一种**上线后才暴露**的失效:
 *
 * 1. **`en` 变空** -> `t()` 的 `active[key] || fallback[key] || key` 兜底失效,
 *    任何词典没覆盖到的 key 直接显示 `shell.language` 这种原始 key 名。
 *    所以 `en` **必须静态打包**(见 `i18n/index.ts` 的注释)。
 * 2. **动态 import 指错模块** -> `loadDictionary('zh-CN')` 悄悄返回 `en`。
 *    界面上全是英文、**没有任何报错** —— 中文用户以为自己切了语言没生效。
 *
 * 所以下面不只测「能加载」,还测「加载到的是**对的那个**」。
 */
describe('i18n 按需加载', () => {
  it('en 必须是静态兜底且非空 —— 它空了 t() 就会漏原始 key', () => {
    expect(Object.keys(en).length).toBeGreaterThan(100)
    expect(initialDictionaries.en).toBe(en)
  })

  it('未加载的语言在 initialDictionaries 里是空壳(不是 undefined)', () => {
    // `{}` 是**真值** —— 这正是 LocaleProvider 里要用 Object.keys().length
    // 而不是 `if (!dictionaries[locale])` 的原因。
    expect(initialDictionaries['zh-CN']).toEqual({})
    expect(initialDictionaries['zh-TW']).toEqual({})
    expect(Boolean(initialDictionaries['zh-CN'])).toBe(true)
  })

  it('loadDictionary 加载到的是对的那个词典,不是都回落到 en', async () => {
    const [loadedEn, loadedCn, loadedTw] = await Promise.all([
      loadDictionary('en'),
      loadDictionary('zh-CN'),
      loadDictionary('zh-TW')
    ])
    expect(loadedEn).toBe(en)
    expect(loadedCn).toBe(zhCN)
    expect(loadedTw).toBe(zhTW)

    // 三者内容必须真的不同 —— 否则就是「动态 import 指错模块」的静默失效
    const cn = Object.keys(loadedCn)
    const tw = Object.keys(loadedTw)
    expect(cn.length).toBeGreaterThan(100)
    expect(loadedCn['shell.language']).not.toBe(en['shell.language'])
    expect(loadedCn['shell.language']).not.toBe(loadedTw['shell.language'])
  })

  it('prepareInitialDictionary 让当前语言在首渲染前就绪', async () => {
    const dictionaries = await prepareInitialDictionary()
    const active = initialLocale()
    // 首屏渲染前必须已加载,否则会先闪一屏英文
    expect(Object.keys(dictionaries[active]).length).toBeGreaterThan(100)
    // 兜底词典任何时候都在场
    expect(dictionaries.en).toBe(en)
  })
})