/**
 * 饼图扇区切分 —— 决定「消费税能不能露出来」的逻辑。
 *
 * 背景:消费税扇区的金额天然很小(日本月消费税通常几千日元),而扇区是**按
 * 金额排序**取的。原本上限是 5,它会排在餐饮/住房/交通/购物之后,自己掉进
 * 灰色「其他」块 —— 那「饼图上单独看到我交了多少钱税」就直接落空了。
 * 上限提到 8(与官方 App 的 `category_pie_chart._maxSlices = 8` 对齐)。
 *
 * 这段逻辑原本藏在 useMemo 里,只能靠肉眼看图形;抽成纯函数后才测得了。
 */
import * as fs from 'node:fs'
import * as path from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { buildDonutSlices, MAX_SLICES_DEFAULT } from './HomeMonthCategoryDonut'

import { TAX_CATEGORY_NAME } from '../../lib/taxCategory'

const OTHER = '其他'
const UNCATEGORIZED = '未分类'

type Rank = { category_name: string; total: number; tx_count: number }

function rank(name: string, total: number, count = 1): Rank {
  return { category_name: name, total, tx_count: count }
}

describe('buildDonutSlices', () => {
  it('消费税扇区即使金额很小也能露出来', () => {
    // 真实量级:住房 100000 / 餐饮 80000 / 交通 20000 / 购物 15000 /
    // 餐饮外食 8000 / 日用品 5000 / 通讯 3000 / 医疗 2000 / 税与保险 298
    const ranks = [
      rank('住房', 100000),
      rank('餐饮', 80000),
      rank('交通', 20000),
      rank('购物', 15000),
      rank('餐饮外食', 8000),
      rank('日用品', 5000),
      rank('通讯', 3000),
      rank('医疗', 2000),
      rank(TAX_CATEGORY_NAME, 298)
    ]
    const slices = buildDonutSlices(
      ranks, OTHER, UNCATEGORIZED, MAX_SLICES_DEFAULT, [TAX_CATEGORY_NAME]
    )

    const names = slices.map((s) => s.name)
    expect(names).toContain(TAX_CATEGORY_NAME)
    // 只把上限从 5 提到 8 **不够** —— 这里有 9 个分类,纯排名制下它照样
    // 排第 9 被并进「其他」。必须靠 alwaysShow 钉住。
    expect(names.filter((n) => n === OTHER)).toHaveLength(1)
    expect(names.indexOf(TAX_CATEGORY_NAME)).toBeGreaterThanOrEqual(0)
    expect(names).not.toContain(OTHER + '(含税)')
  })

  it('钉住后总扇区数仍受 max 约束,且金额守恒', () => {
    const ranks = [
      rank('A', 100000), rank('B', 80000), rank('C', 20000), rank('D', 15000),
      rank('E', 8000), rank('F', 5000), rank('G', 3000), rank('H', 2000),
      rank(TAX_CATEGORY_NAME, 298)
    ]
    const slices = buildDonutSlices(
      ranks, OTHER, UNCATEGORIZED, 8, [TAX_CATEGORY_NAME]
    )
    // 8 个具名扇区 + 1 个「其他」= 9
    expect(slices).toHaveLength(9)
    expect(slices.filter((s) => s.name !== OTHER)).toHaveLength(8)
    expect(slices.map((s) => s.name)).toContain(TAX_CATEGORY_NAME)
    // 金额一分不差
    const sum = slices.reduce((s, r) => s + r.total, 0)
    expect(sum).toBe(ranks.reduce((s, r) => s + r.total, 0))
  })

  it('钉住的分类不占普通分类的槽位', () => {
    // 钉住 2 个 → 普通分类只剩 6 个位置,不是 8 个
    const ranks = [
      rank('A', 900), rank('B', 800), rank('C', 700), rank('D', 600),
      rank('E', 500), rank('F', 400), rank('G', 300), rank('H', 200),
      rank(TAX_CATEGORY_NAME, 298), rank('福利税', 1000)
    ]
    const slices = buildDonutSlices(
      ranks, OTHER, UNCATEGORIZED, 8, [TAX_CATEGORY_NAME, '福利税']
    )
    const named = slices.filter((s) => s.name !== OTHER).map((s) => s.name)
    expect(named).toContain(TAX_CATEGORY_NAME)
    expect(named).toContain('福利税')
    expect(named).toHaveLength(8)
    // 槽位让给了钉住项:H(200) 被挤进「其他」
    expect(named).not.toContain('H')
  })

  it('钉住的分类不存在时不报错', () => {
    const slices = buildDonutSlices(
      [rank('A', 100)], OTHER, UNCATEGORIZED, 8, ['并不存在的分类']
    )
    expect(slices.map((s) => s.name)).toEqual(['A'])
  })

  it('上限从 5 提到 8 —— 直接锁住这个决定', () => {
    // 这个数字是有理由的(见文件头注释),不是随手调的魔法数。
    // 一旦有人改回 5,税额扇区会静默掉进「其他」,而用户完全看不出发生了什么。
    expect(MAX_SLICES_DEFAULT).toBe(8)
  })

  it('金额总和守恒 —— 合并「其他」不能凭空增减', () => {
    const ranks = [
      rank('A', 100), rank('B', 50), rank('C', 25), rank('D', 10), rank('E', 5)
    ]
    const slices = buildDonutSlices(ranks, OTHER, UNCATEGORIZED, 3)
    const sum = slices.reduce((s, r) => s + r.total, 0)
    expect(sum).toBe(ranks.reduce((s, r) => s + r.total, 0))
    expect(slices.find((s) => s.name === OTHER)?.total).toBe(15) // 10 + 5
  })

  it('按金额降序切分', () => {
    const ranks = [rank('小', 1), rank('大', 100), rank('中', 50)]
    const slices = buildDonutSlices(ranks, OTHER, UNCATEGORIZED)
    expect(slices.map((s) => s.name)).toEqual(['大', '中', '小'])
  })

  it('金额为 0 的分类不占扇区', () => {
    const slices = buildDonutSlices(
      [rank('有', 100), rank('无', 0)],
      OTHER,
      UNCATEGORIZED
    )
    expect(slices.map((s) => s.name)).toEqual(['有'])
  })

  it('全部放得下时不产生「其他」块', () => {
    const slices = buildDonutSlices(
      [rank('A', 100), rank('B', 50)],
      OTHER,
      UNCATEGORIZED
    )
    expect(slices.map((s) => s.name)).not.toContain(OTHER)
  })

  it('没有分类名时用「未分类」兜底', () => {
    const slices = buildDonutSlices(
      [{ category_name: '', total: 100, tx_count: 1 }],
      OTHER,
      UNCATEGORIZED
    )
    expect(slices[0].name).toBe(UNCATEGORIZED)
  })

  it('空数据返回空扇区,不死循环也不崩', () => {
    expect(buildDonutSlices([], OTHER, UNCATEGORIZED)).toEqual([])
  })
})

/**
 * **接线**层面的断言 —— 上面那些测的是纯函数,没覆盖组件真的把钉住名单
 * 传进去了。曾经出现过:组件里的 `[TAX_CATEGORY_NAME]` 被清成 `[]`,
 * 而纯函数测试照样全绿(它们自己显式传参),税额扇区就静默掉进「其他」。
 *
 * 所以这里直接解析组件 AST,断言 `buildDonutSlices` 的调用实参里确实带着
 * 钉住名单。手法与 `hookPlacement.test.ts` 相同:抓的是「tsc / build 抓不到的
 * 那类静默丢失」。
 */
describe('HomeMonthCategoryDonut 接线', () => {
  it('buildDonutSlices 的调用带着钉住的消费税分类', () => {
    const file = path.resolve(__dirname, 'HomeMonthCategoryDonut.tsx')
    const sf = ts.createSourceFile(
      file, fs.readFileSync(file, 'utf8'), ts.ScriptTarget.ESNext, true
    )

    const calls: ts.CallExpression[] = []
    const visit = (n: ts.Node) => {
      if (
        ts.isCallExpression(n) &&
        ts.isIdentifier(n.expression) &&
        n.expression.text === 'buildDonutSlices'
      ) {
        calls.push(n)
      }
      ts.forEachChild(n, visit)
    }
    ts.forEachChild(sf, visit)

    expect(calls.length).toBeGreaterThan(0)
    const call = calls[0]
    const pinned = call.arguments[4]
    expect(pinned, '第 5 个参数(钉住名单)缺失 —— 消费税扇区会被并进「其他」')
      .toBeDefined()
    expect(pinned!.getText()).toContain('TAX_CATEGORY_NAME')

    // 顺带锁住扇区上限
    const maxArg = call.arguments[3]
    expect(maxArg?.getText()).toContain('MAX_SLICES_DEFAULT')
  })
})
