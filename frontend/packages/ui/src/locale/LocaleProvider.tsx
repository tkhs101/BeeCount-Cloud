import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState
} from 'react'

import {
  applyDocumentLocale,
  initialLocale,
  persistLocale,
  type Locale
} from './locale-script'

export type LocaleMessages = Record<string, string>
export type LocaleDictionaries = Record<Locale, LocaleMessages>
export type TranslateParams = Record<string, string | number>

type LocaleContextValue = {
  locale: Locale
  /** 解析后一定切换完成;调用方不 await 也能用(词典已在内存时是同步的)。 */
  setLocale: (next: Locale) => void
  t: (key: string, params?: TranslateParams) => string
}

type LocaleProviderProps = PropsWithChildren<{
  /**
   * 首屏已加载的词典。**可以是空壳**(`{}` 表示该语言还没加载),
   * 这时 `t()` 走 `en` 兜底 —— 显示英文,而不是原始 key。
   */
  dictionaries: LocaleDictionaries
  /**
   * 按需加载某个语言的词典(动态 `import()`)。
   *
   * 不传就退回旧行为:`setLocale` 立即生效,切到没加载的语言会显示英文兜底。
   */
  loadDictionary?: (locale: Locale) => Promise<LocaleMessages>
}>

const LocaleContext = createContext<LocaleContextValue | null>(null)

/**
 * 词典是否**已加载**。
 *
 * ⚠️ 不能写 `if (!dictionaries[locale])` —— 空对象 `{}` 是**真值**,
 * 未加载的语言会因此被误判成「已加载」,`setLocale` 于是跳过加载直接切换,
 * 用户永远停在一屏英文上,而且**没有任何报错**。
 */
function isLoaded(messages: LocaleMessages | undefined): boolean {
  return messages != null && Object.keys(messages).length > 0
}

function applyTemplate(template: string, params?: TranslateParams): string {
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (_match, token: string) => {
    const value = params[token]
    if (value === undefined || value === null) return ''
    return String(value)
  })
}

export function LocaleProvider({
  children,
  dictionaries,
  loadDictionary
}: LocaleProviderProps) {
  const [locale, setLocaleState] = useState<Locale>(() => initialLocale())
  const [cache, setCache] = useState<LocaleDictionaries>(dictionaries)

  const setLocale = useCallback(
    (next: Locale) => {
      // 词典已在内存 -> 同步切,调用方不 await 也完全正确(保持旧行为)
      if (isLoaded(cache[next])) {
        setLocaleState(next)
        persistLocale(next)
        return
      }
      if (!loadDictionary) {
        // 没有加载器就退回旧的「立即切换」语义,最坏显示英文兜底
        setLocaleState(next)
        persistLocale(next)
        return
      }
      // ⚠️ 先把词典拿到手再切语言 —— 反过来的话会闪一屏英文
      void loadDictionary(next)
        .then((messages) => {
          setCache((prev) => ({ ...prev, [next]: messages }))
          setLocaleState(next)
          persistLocale(next)
        })
        .catch(() => {
          // 加载失败仍然切换:t() 会退回 en,好过点了没反应
          setLocaleState(next)
          persistLocale(next)
        })
    },
    [cache, loadDictionary]
  )

  useEffect(() => {
    applyDocumentLocale(locale)
  }, [locale])

  const t = useCallback(
    (key: string, params?: TranslateParams) => {
      const active = cache[locale] || cache.en
      const fallback = cache.en
      const template = active[key] || fallback[key] || key
      return applyTemplate(template, params)
    },
    [cache, locale]
  )

  const value = useMemo(
    () => ({
      locale,
      setLocale,
      t
    }),
    [locale, setLocale, t]
  )

  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>
}

export function useLocale(): LocaleContextValue {
  const ctx = useContext(LocaleContext)
  if (!ctx) {
    throw new Error('useLocale must be used within LocaleProvider')
  }
  return ctx
}

export function useT(): LocaleContextValue['t'] {
  return useLocale().t
}
