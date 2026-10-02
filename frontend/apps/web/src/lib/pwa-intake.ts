/**
 * PWA 入站载荷的全局暂存,跨路由传递 File / 分享文本。
 *
 * 三种入口都会把内容放进这里,然后下游页面(ImportPage / TransactionsPage)
 * 在 useEffect 里 consume*Pending* 取走 + 清空:
 *   - Share Target POST  → SW 缓存 → ShareIncomingPage 转存
 *   - launchQueue (File) → main.tsx 监听 → ShareIncomingPage 转存
 *   - 其他自定义场景     → 任意页面 setPending* 注入
 *
 * 用 module 级变量而不是 sessionStorage:File 对象无法序列化,且这是一次性
 * intent,刷新页面就丢了也无所谓(用户重新发起即可)。
 */

let pendingImportFile: File | null = null
let pendingShareText: { title?: string; text?: string; url?: string } | null = null
/** 分享进来的图片(小票照)。和 pendingImportFile 一样是一次性 intent,
 *  但走不同下游:图片不是「待导入的表格」,而是「待附到某笔交易上的附件」。 */
let pendingShareImage: File | null = null

export function setPendingImportFile(file: File | null): void {
  pendingImportFile = file
}

export function consumePendingImportFile(): File | null {
  const file = pendingImportFile
  pendingImportFile = null
  return file
}

export function setPendingShareText(payload: { title?: string; text?: string; url?: string } | null): void {
  pendingShareText = payload
}

export function consumePendingShareText(): { title?: string; text?: string; url?: string } | null {
  const payload = pendingShareText
  pendingShareText = null
  return payload
}

export function setPendingShareImage(file: File | null): void {
  pendingShareImage = file
}

export function consumePendingShareImage(): File | null {
  const file = pendingShareImage
  pendingShareImage = null
  return file
}
