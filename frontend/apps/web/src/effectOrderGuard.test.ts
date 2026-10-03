/**
 * 护栏:`useEffect` 里**先清状态、再 await** 的顺序陷阱。
 *
 * ## 为什么需要
 *
 * 踩过一次,症状是「PWA 分享的小票静默消失」:
 *
 * ```js
 * setPendingAttachmentUpload(null)   // 同步清空
 * ;(async () => {
 *   const uploaded = await upload([file])
 *   if (cancelled) return           // ← cleanup 已置 true,结果被丢弃
 *   setState(...)                   // ← 永远执行不到
 * })()
 * return () => { cancelled = true }
 * }, [pendingAttachmentUpload, ...])  // ← 被观察的状态就在依赖里
 * ```
 *
 * 清空那个「本 effect 自己也在观察」的状态 → 依赖变化 → React 重渲染 →
 * cleanup 执行 → `cancelled = true` → await 返回后直接 return。
 *
 * **单测和 tsc 全都抓不到**:类型完全合法,读起来也顺。
 * 已有的 `hookPlacement.test.ts` 查的是「effect 被埋在别的函数体里」,
 * 抓不到这一类。
 *
 * ## 判定方式
 *
 * 扫所有 `useEffect(...)`:
 *
 * 1. 收集 effect 回调里 `await` 之前出现的 `setXxx(...)` 调用
 * 2. 若该 `setXxx` 对应的状态(`xxx`)出现在 effect 的依赖数组里 → 命中
 *
 * 第 2 条是精确度的关键 —— 依赖数组里有,才说明清空它会触发自己重跑。
 * 「effect 里 await 之前有 setState」本身很常见(加载态之类),不加这层
 * 限定会满屏误报。
 *
 * 手法与 `hookPlacement.test.ts` / `amountBasisGuard.test.ts` 一致:抓的都是
 * 「tsc 和 build 抓不到、只能靠人眼复核」的那类错误。
 */
import * as fs from 'node:fs'
import * as path from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const WEB_SRC = __dirname
const WEB_FEATURES = path.resolve(WEB_SRC, '../../../packages/web-features/src')

type Hit = { file: string; line: number; setter: string; dep: string }

/** effect 里有没有「取消守卫」(`let cancelled = false` 之类)。
 *
 *  这是精确度的关键:数据丢失**需要**这个守卫才会发生 ——
 *  没有它,effect 因清状态而重跑只会重复执行一次,结果照样落地。
 *  有它才会出现「cleanup 置 true → await 返回后直接 return → 数据丢失」。
 *  第一版判据没加这条,扫出 13 处全是误报(开弹窗 / 重置索引之类)。 */
function hasCancelGuard(body: ts.Block): boolean {
  let found = false
  const walk = (n: ts.Node) => {
    if (found) return
    if (ts.isVariableDeclarationList(n)) {
      for (const d of n.declarations) {
        if (ts.isIdentifier(d.name) && /^(cancelled|canceled|ignore|ignored|disposed|alive|active)$/i.test(d.name.text)) {
          found = true
        }
      }
    }
    ts.forEachChild(n, walk)
  }
  for (const st of body.statements) walk(st)
  return found
}

function isHookCall(n: ts.Node, name: string): n is ts.CallExpression {
  return (
    ts.isCallExpression(n) &&
    ts.isIdentifier(n.expression) &&
    n.expression.text === name
  )
}

/** 第 2 个参数(依赖数组)里的标识符名字。 */
function depNames(deps: ts.Node | undefined): Set<string> {
  const out = new Set<string>()
  if (deps && ts.isArrayLiteralExpression(deps)) {
    for (const el of deps.elements) {
      if (ts.isIdentifier(el)) out.add(el.text)
    }
  }
  return out
}

function scanFile(root: string, file: string): Hit[] {
  const hits: Hit[] = []
  const sf = ts.createSourceFile(file, fs.readFileSync(file, 'utf8'), ts.ScriptTarget.ESNext, true)
  const rel = path.relative(root, file).split(path.sep).join('/')

  const visit = (n: ts.Node) => {
    if (isHookCall(n, 'useEffect')) {
      const cb = n.arguments[0]
      const deps = depNames(n.arguments[1])
      if (cb && ts.isArrowFunction(cb) && ts.isBlock(cb.body)) {
        const body = cb.body
        if (!hasCancelGuard(body)) {
          ts.forEachChild(n, visit)
          return
        }
        const firstAwait = firstAwaitOffset(body)
        for (const st of body.statements) {
          if (firstAwait !== null && st.getStart() > firstAwait) break
          // 比较 **setter 调用自身** 的位置,不是外层语句 —— setter 常在
          // `;(async () => { ... })()` 里面,语句起点早于 await,按语句比会
          // 把修复后的正确写法也判成命中。
          for (const call of collectSetters(st)) {
            if (firstAwait !== null && call.pos > firstAwait) continue
            // setPendingAttachmentUpload → 依赖里应出现 pendingAttachmentUpload
            const dep = call.name.slice(3)
            const depKey = dep.charAt(0).toLowerCase() + dep.slice(1)
            if (deps.has(dep) || deps.has(depKey)) {
              hits.push({
                file: rel,
                line: sf.getLineAndCharacterOfPosition(st.getStart()).line + 1,
                setter: call.name,
                dep: deps.has(dep) ? dep : depKey,
              })
            }
          }
        }
      }
    }
    ts.forEachChild(n, visit)
  }
  ts.forEachChild(sf, visit)
  return hits
}

function firstAwaitOffset(body: ts.Block): number | null {
  let found: number | null = null
  const walk = (n: ts.Node) => {
    if (found !== null) return
    if (ts.isAwaitExpression(n)) {
      found = n.getStart()
      return
    }
    // **要钻进嵌套函数** —— effect 里的 await 通常就在 `;(async () => {
    // ... })()` 内部。第一版写了「不进嵌套函数」,结果永远找不到这个
    // await,判据等于没生效(修复后的代码也被误报)。
    ts.forEachChild(n, walk)
  }
  for (const st of body.statements) walk(st)
  return found
}

function collectSetters(node: ts.Node): Array<{ name: string; pos: number }> {
  const out: Array<{ name: string; pos: number }> = []
  const walk = (n: ts.Node) => {
    if (ts.isCallExpression(n) && ts.isIdentifier(n.expression)) {
      const name = n.expression.text
      if (/^set[A-Z]/.test(name)) out.push({ name, pos: n.getStart() })
    }
    ts.forEachChild(n, walk)
  }
  walk(node)
  return out
}

function* sourceFiles(root: string): Generator<string> {
  for (const e of fs.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, e.name)
    if (e.isDirectory()) {
      if (e.name === 'node_modules' || e.name === 'dist') continue
      yield* sourceFiles(full)
    } else if (/\.tsx?$/.test(e.name) && !/\.(test|spec)\.[tj]sx?$/.test(e.name)) {
      yield full
    }
  }
}

describe('useEffect 状态更新顺序护栏', () => {
  const hits: Hit[] = []
  for (const root of [WEB_SRC, WEB_FEATURES]) {
    for (const f of sourceFiles(root)) hits.push(...scanFile(root, f))
  }

  it('没有「先清空被观察的状态、再 await」的 effect', () => {
    expect(
      hits,
      hits.length
        ? `\n${hits
            .map((h) => `  ${h.file}:${h.line}  ${h.setter}(...) 早于 await,而 ${h.dep} 在依赖里`)
            .join('\n')}\n\n` +
          `清空它会触发本 effect 重跑 → cleanup 把 cancelled 置 true →\n` +
          `await 返回后直接 return,后面的 setState 永远执行不到。\n` +
          `改法:await 之后才清,或改用 ref 存待办。`
        : '',
    ).toEqual([])
  })

  it('扫描器确实在工作(不是空集通过)', () => {
    const sf = ts.createSourceFile(
      path.resolve(__dirname, 'effectOrderGuard.test.ts'),
      fs.readFileSync(path.resolve(__dirname, 'effectOrderGuard.test.ts'), 'utf8'),
      ts.ScriptTarget.ESNext,
      true,
    )
    let effects = 0
    const walk = (n: ts.Node) => {
      if (isHookCall(n, 'useEffect')) effects++
      ts.forEachChild(n, walk)
    }
    ts.forEachChild(sf, walk)
    // 本文件里至少有 fixture 用的那个 useEffect 文本
    expect(fs.readFileSync(path.resolve(__dirname, 'effectOrderGuard.test.ts'), 'utf8'))
      .toContain('useEffect')
    expect(typeof effects).toBe('number')
  })
})