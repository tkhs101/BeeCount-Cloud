/**
 * 防「花括号插错位置」型 bug 的回归测试。
 *
 * 背景:分享小票的自动挂载 effect 一度被插进 `onUploadTxAttachments` 的箭头
 * 函数体里,而那个函数的 try / catch 都 return —— effect 成了**不可达语句**。
 * TypeScript 与 vite build 都抓不到:调用表达式语法完全合法,只是永远不执行。
 * 线上表现是 UI 已经 toast「小票已附上」,附件却从未挂上,小票静默丢失。
 *
 * 这里用 TypeScript 自己的 parser 做结构断言:所有 useEffect / useMemo /
 * useState / useRef 调用必须位于**组件函数的直接子语句**里,不能藏在别的
 * 函数体内。列进 CI 比靠人眼可靠。
 */
import { describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'
import ts from 'typescript'

// __dirname 本身就是 apps/web/src,所以下面的路径直接相对 src/
const SRC_DIR = __dirname

/** 从 node 一路往上,找最近的 FunctionLike 祖先(不含 node 自身)。 */
type ConcreteFn =
  | ts.FunctionDeclaration
  | ts.FunctionExpression
  | ts.ArrowFunction
  | ts.MethodDeclaration

/** 有函数体的函数类型(排除 SignatureDeclaration 之类无 body 的签名)。 */
function isConcreteFn(n: ts.Node): n is ConcreteFn {
  return (
    ts.isFunctionDeclaration(n) ||
    ts.isFunctionExpression(n) ||
    ts.isArrowFunction(n) ||
    ts.isMethodDeclaration(n)
  )
}

function nearestFunction(node: ts.Node): ConcreteFn | null {
  let cur: ts.Node | undefined = node.parent
  while (cur) {
    if (isConcreteFn(cur)) return cur
    cur = cur.parent
  }
  return null
}

/**
 * 收集组件内**被埋在其它函数体里**的 hook 调用。
 *
 * 判定要点(两条都踩过坑):
 * 1. 不能用「CallExpression 的 parent 在不在函数体直接语句里」—— hook 常嵌在
 *    `const [x, setX] = useState(...)` 的 VariableStatement 里,那样全是假阳性。
 * 2. **更不能「不钻进嵌套函数」** —— 恰恰是嵌套函数(箭头函数 / 回调)里藏
 *    hook 才是我们要抓的 bug。第一版就是这么写的,结果 bug 版代码照样绿。
 *
 * 正确做法:找每个 hook 调用**最近的 FunctionLike 祖先**,不是组件本身
 * 就说明它埋在别人的函数体里,永远不会按预期时机执行。
 */
function collectBuriedHooks(component: ConcreteFn): Array<{
  name: string
  line: number
}> {
  const found: Array<{ name: string; line: number }> = []
  const body = component.body
  if (!body || !ts.isBlock(body)) return found

  const visit = (node: ts.Node) => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
      const name = node.expression.text
      if (/^use[A-Z]/.test(name)) {
        const owner = nearestFunction(node)
        if (owner && owner !== component) {
          const { line } = node.getSourceFile().getLineAndCharacterOfPosition(node.getStart())
          found.push({ name, line: line + 1 })
        }
      }
    }
    ts.forEachChild(node, visit)
  }
  ts.forEachChild(body, visit)
  return found
}

/** 找出文件里的顶层组件函数(名字以大写开头)。 */
function topLevelComponents(sf: ts.SourceFile): ts.FunctionDeclaration[] {
  return sf.statements.filter(
    (s): s is ts.FunctionDeclaration =>
      ts.isFunctionDeclaration(s) &&
      !!s.name &&
      /^[A-Z]/.test(s.name.text) &&
      !!s.body,
  )
}

function parse(relPath: string): ts.SourceFile {
  const abs = path.join(SRC_DIR, relPath)
  return ts.createSourceFile(abs, fs.readFileSync(abs, 'utf8'), ts.ScriptTarget.ESNext, true)
}

/** 递归收集目录下的所有 `.tsx`(跳过测试文件与 node_modules)。 */
function collectTsx(dir: string, acc: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name.startsWith('.')) continue
    const abs = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      collectTsx(abs, acc)
    } else if (entry.name.endsWith('.tsx') && !entry.name.includes('.test.')) {
      acc.push(path.relative(SRC_DIR, abs).split(path.sep).join('/'))
    }
  }
  return acc
}

describe('hook placement (no unreachable hooks)', () => {
  // ⚠️ **自动发现所有 .tsx**,不是硬编码名单。
  //
  // 原先是 3 个文件的硬编码列表,理由是「本次改动涉及 hook 的文件,覆盖到
  // 就是够用,别把全仓都扫进来」。变异审计时实测:全仓 **79 个**文件里有
  // hook,护栏只盯 **3 个** —— 覆盖 3.8%。
  //
  // 硬编码名单与我在反向桥护栏里批评过的「opt-in 列表默认放行」是同一个
  // 反模式:**新文件加 hook 时默认不被检查**,而那正是要防的情况。
  // 扫全仓的代价只是测试慢一点(79 个小文件解析,亚秒级)。
  const watched = collectTsx(SRC_DIR)

  /** 真正含顶层组件的文件(入口文件如 `main.tsx` 是 `createRoot().render()`,
   * 没有组件定义,断言它们会误报)。 */
  const withComponents = watched.filter(
    (rel) => topLevelComponents(parse(rel)).length > 0,
  )

  it('自动发现确实扫到了足够多的文件(防扫描逻辑本身失效)', () => {
    // 这一条是给「扫描范围」上的护栏:`collectTsx` 返回空/极少时,
    // 下面的逐文件断言会**全部静默通过** —— 一个都不检查的护栏
    // 看起来和全绿一模一样。
    expect(watched.length).toBeGreaterThan(50)
    // 且其中要有足够多的**含组件**文件,否则「跳过无组件文件」这条
    // 规则可能把所有文件都跳掉了,同样等于没检查。
    expect(withComponents.length).toBeGreaterThan(20)
  })

  for (const rel of withComponents) {
    it(`${rel} 的 hook 都在组件函数体内直接执行`, () => {
      const sf = parse(rel)
      const components = topLevelComponents(sf)
      expect(components.length).toBeGreaterThan(0)

      const nested = components.flatMap((c) =>
        collectBuriedHooks(c).map((h) => ({ component: c.name!.text, ...h })),
      )
      expect(
        nested,
        `这些 hook 被埋在函数体里,永远不会执行:\n${nested
          .map((n) => `  ${n.component}:${n.line} use${n.name.slice(3)}`)
          .join('\n')}`,
      ).toEqual([])
    })
  }
})