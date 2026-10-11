import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

/**
 * 词典文件的**编码健康**护栏。
 *
 * ## 为什么需要它 —— 一条真实发生过的损坏
 *
 * 上一轮用 bash heredoc 写 zh-CN 词典时,「今天」被静默毁成 2 个 `U+FFFD`。
 * 那次损坏**两道门都抓不到**:
 *
 * | 门 | 结果 | 为什么 |
 * |---|---|---|
 * | `tsc -b` | 绿 | 替换字符是**合法**的字符串字面量 |
 * | `autoRepayParity.test.ts` | 绿 | 键**存在**,只是内容坏了 |
 * | vitest(除本条外) | 绿 | 没人在断言中文文案内容 |
 *
 * **只有中文用户会看到乱码。** 当时是靠手工数出来的 —— 手工检查意味着
 * 下次还会漏,所以补成一道每次提交都会跑的门。
 *
 * ## 为什么是「默认红」而不是「文档里写一句」
 *
 * CLAUDE.md 记着:注释和文档**不强制任何东西**。而这类损坏 100% 是人写出来的
 * (heredoc / 复制粘贴 / 编码转换),要拦住它得有真门。
 */
const I18N_DIR = join(dirname(fileURLToPath(import.meta.url)), 'i18n')

/** 替换字符 U+FFFD:解码失败 / 字节被截断时留下的痕迹。 */
const REPLACEMENT_CHAR = String.fromCharCode(0xfffd)

/**
 * ⚠️ **数量下限**:防止「目录路径写错 / 匹配式失效」时 `files` 为空,
 * 于是「什么都没检查」= **全绿**。
 *
 * 这正是 CLAUDE.md 记的那类恒真 —— 判据找不到东西时,必须自己先红。
 */
const MIN_FILES = 4

const FILES = readdirSync(I18N_DIR).filter((f) => f.endsWith('.ts'))

/**
 * 控制字符范围,用 `fromCharCode` **逐个构造**而不是往源文件里打转义或字面量。
 *
 * ⚠️ 这不是洁癖:本文件第一版就是用 `/[\x00-\x08...]/` 写的,
 * 而 **bash heredoc 把 `\x0B` 解释成了真控制字符**,正则从中间被截断、
 * 后面两行被吞,esbuild 直接报语法错。
 *
 * **我正在写的这条护栏,被它要防的那类损坏打中过。** 所以这里不用字面量。
 */
const CONTROL_CHARS = new Set(
  Array.from({ length: 32 }, (_, i) => String.fromCharCode(i)).filter(
    (c) => c !== '\n' && c !== '\t' && c !== '\r',
  ),
)

function controlCharLines(src: string): number[] {
  const bad: number[] = []
  src.split('\n').forEach((line, i) => {
    if ([...line].some((ch) => CONTROL_CHARS.has(ch))) bad.push(i + 1)
  })
  return bad
}

describe('i18n 词典文件编码健康', () => {
  it(`找得到词典文件(>= ${MIN_FILES} 个,否则下面的检查形同虚设)`, () => {
    expect(FILES.length).toBeGreaterThanOrEqual(MIN_FILES)
    // 三种语言必须都在 —— 少一个说明路径或命名变了,不是「文件坏了」
    expect(FILES).toContain('en.ts')
    expect(FILES).toContain('zh-CN.ts')
    expect(FILES).toContain('zh-TW.ts')
  })

  it.each(FILES)('%s 不含替换字符 U+FFFD', (name) => {
    const src = readFileSync(join(I18N_DIR, name), 'utf8')
    const bad = src
      .split('\n')
      .map((line, i) => (line.includes(REPLACEMENT_CHAR) ? i + 1 : 0))
      .filter(Boolean)
    // 报**行号**,而不只是「第 N 个」—— 否则等于没告诉别人去哪儿改
    expect(
      bad,
      bad.length
        ? `${name} 第 ${bad.join(', ')} 行含 U+FFFD —— 写入时字节被截断/编码错了`
        : '',
    ).toEqual([])
  })

  it.each(['zh-CN.ts', 'zh-TW.ts'])('%s 不含控制字符', (name) => {
    const bad = controlCharLines(readFileSync(join(I18N_DIR, name), 'utf8'))
    expect(
      bad,
      bad.length ? `${name} 第 ${bad.join(', ')} 行含控制字符` : '',
    ).toEqual([])
  })
})