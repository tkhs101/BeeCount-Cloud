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

describe('hook placement (no unreachable hooks)', () => {
  // 这些是本次改动涉及 hook 的文件 —— 覆盖到就是够用,别把全仓都扫进来
  const watched = [
    'pages/sections/TransactionsPage.tsx',
    'components/GlobalEditDialogs.tsx',
    'pages/sections/ShareIncomingPage.tsx',
  ]

  for (const rel of watched) {
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