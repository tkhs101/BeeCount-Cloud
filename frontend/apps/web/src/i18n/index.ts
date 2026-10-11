import type { Locale, LocaleDictionaries, LocaleMessages } from '@beecount/ui'

import en from './en'

/**
 * 首屏只静态打包 `en`。
 *
 * 之前三种语言都是静态 import —— **所有用户都下载全部三种语言,哪怕只读一种**,
 * 实测 231 kB(2026-10-12 改之前首屏 1672 kB)。
 *
 * **`en` 必须是静态的**:`t()` 里
 * `active[key] || fallback[key] || key` 的 fallback 就是 `dictionaries.en`。
 * 少了它,任何词典没覆盖到的 key 都会**直接显示原始 key 名**。
 *
 * `zh-CN` / `zh-TW` 只有在**该语言真的被选中**时才需要,走动态 import。
 * 未加载前它们在 `dictionaries` 里是 `{}` —— `t()` 会走 `en` 兜底,
 * 所以最坏情况是**短暂显示英文**,不是满屏原始 key。
 *
 * Provider 在切换语言前会先 `await loadDictionary(next)`,
 * 正常路径**看不到那个英文闪烁**。
 */
export async function loadDictionary(locale: Locale): Promise<LocaleMessages> {
  if (locale === 'en') return en
  if (locale === 'zh-CN') return (await import('./zh-CN')).default
  return (await import('./zh-TW')).default
}

/** 首屏词典:只有 `en` 是真的,其余是空壳(见上)。 */
export const initialDictionaries: LocaleDictionaries = {
  en,
  'zh-CN': {},
  'zh-TW': {}
}

export type TranslationKey = keyof typeof en

/**
 * 首屏渲染**之前**把用户实际使用的语言词典准备好 ——
 * 否则一个 zh-CN 用户会先看到一屏英文再跳中文。
 */
export async function prepareInitialDictionary(): Promise<LocaleDictionaries> {
  const { initialLocale } = await import('@beecount/ui')
  const locale = initialLocale()
  return { ...initialDictionaries, [locale]: await loadDictionary(locale) }
}