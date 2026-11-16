import { useCallback, useEffect, useMemo, useState } from 'react'

import { fetchJson, request } from '@/api/client'

const ENDPOINT = '/api/dimensionmap'
const columns = [
  "规则编号",
  "原始维度名",
  "标准维度名",
  "映射类型",
  "值转换逻辑",
  "默认取值",
  "映射优先级",
  "最近更新时间"
]
const actions = [
  "提交规则评审",
  "生效规则",
  "废弃规则"
]
const statuses = [
  "草稿",
  "规则评审中",
  "已生效",
  "已废弃"
]
const stats = [
  {
    "label": "映射规则总数",
    "value": 0
  },
  {
    "label": "已生效规则",
    "value": 0
  },
  {
    "label": "评审中规则",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type Standard = {
  id: number
  源维度: string
  标准维度: string
  映射类型: string
  规则编号: string
  版本ID: number
  版本编号: string
  口径指纹: string
  覆盖模块: string[]
  同步时间: string
  冲突: boolean
  权衡说明: string
  候选规则: { 规则编号: string; 标准维度: string; 映射优先级: string; 最近更新时间: string }[]
}

type Version = {
  id: number
  版本编号: string
  源维度: string
  标准维度: string | null
  规则编号: string | null
  口径状态: string
  记录时间: string
  冲突: boolean
  权衡说明: string
}

export default function DimensionmapView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const [standards, setStandards] = useState<Standard[]>([])
  const [missing, setMissing] = useState<{ 源维度: string; 引用: { 模块: string; 字段: string }[] }[]>([])
  const [versions, setVersions] = useState<Version[]>([])
  const [selectedSource, setSelectedSource] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('映射规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '维度映射列表读取失败')
    }
  }, [filters])

  const reloadStandards = useCallback(async () => {
    const payload = await fetchJson<{ items: Standard[]; 缺少标准映射: { 源维度: string; 引用: { 模块: string; 字段: string }[] }[] }>(
      `${ENDPOINT}/standards`,
    )
    setStandards(payload.items ?? [])
    setMissing(payload.缺少标准映射 ?? [])
  }, [])

  const reloadVersions = useCallback(async (source?: string | null) => {
    const query = source ? `?source=${encodeURIComponent(source)}` : ''
    const payload = await fetchJson<{ items: Version[] }>(`${ENDPOINT}/versions${query}`)
    setVersions(payload.items ?? [])
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    reloadStandards().catch(() => undefined)
  }, [reloadStandards])

  useEffect(() => {
    reloadVersions(selectedSource).catch(() => undefined)
  }, [reloadVersions, selectedSource])

  async function syncAll() {
    try {
      const response = await request(`${ENDPOINT}/sync`, { method: 'POST', body: '{}' })
      if (!response.ok) {
        throw new Error('标准维度同步接口返回失败')
      }
      const payload = (await response.json()) as { 新增版本数: number; 冲突数: number; 同步时间: string }
      await reloadStandards()
      await reloadVersions(selectedSource)
      setErrorMessage(`标准维度映射已同步（${payload.同步时间}），新增版本 ${payload.新增版本数} 个，冲突 ${payload.冲突数} 处`)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '标准维度同步失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '维度映射动作未生效')
      }
      await reload()
      await reloadStandards()
      await reloadVersions(selectedSource)
      setErrorMessage(payload.message)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '维度映射操作失败')
    }
  }

  return (
    <section className="page" data-module="dimensionmap">
      <header className="page-head">
        <div>
          <h2>维度映射管理</h2>
          <p className="page-desc">管理分析维度的标准化映射规则，并把各模块的维度映射同步写入标准维度映射结果与版本。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('映射规则登记入口尚未接入审批流')}>
            登记映射规则
          </button>
          <button className="btn" type="button" onClick={() => void syncAll()}>
            同步标准维度映射
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出维度映射清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
        <article className="stat-card">
          <span className="stat-label">标准维度（有效口径）</span>
          <strong className="stat-value">{standards.length}</strong>
        </article>
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无维度映射数据，可先登记映射规则</td>
            </tr>
          )}
        </tbody>
      </table>

      <section className="snapshot-panel">
        <header className="panel-head">
          <div>
            <h3>标准维度映射结果</h3>
            <p className="page-desc">每个源维度只保留一个当前口径；同源多条已生效规则时按优先级与更新时间做一次权衡，并把结论留痕。</p>
          </div>
          <button className="btn" type="button" onClick={() => void reloadStandards()}>刷新</button>
        </header>

        <table className="data-table">
          <thead>
            <tr>
              <th>源维度</th>
              <th>标准维度</th>
              <th>采用规则</th>
              <th>版本</th>
              <th>覆盖模块</th>
              <th>冲突裁决</th>
              <th>同步时间</th>
            </tr>
          </thead>
          <tbody>
            {standards.map((row) => (
              <tr key={row.id} className="clickable-row" onClick={() => setSelectedSource(row.源维度)}>
                <td>{row.源维度}</td>
                <td>{row.标准维度}</td>
                <td>{row.规则编号}</td>
                <td>{row.版本编号 ?? '—'}</td>
                <td>{(row.覆盖模块 ?? []).join('、') || '—'}</td>
                <td>
                  {row.冲突 ? <span className="tag warn-tag">存在新旧口径冲突</span> : <span className="muted">无冲突</span>}
                  {row.冲突 && <div className="page-desc">{row.权衡说明}</div>}
                </td>
                <td>{row.同步时间 ?? '—'}</td>
              </tr>
            ))}
            {standards.length === 0 && (
              <tr><td className="empty-state" colSpan={7}>还没有标准维度映射结果，可先同步或生效映射规则</td></tr>
            )}
          </tbody>
        </table>

        {missing.length > 0 && (
          <div className="drift-banner warn">
            <span>
              缺少标准映射的维度：{missing.map((item) => `${item.源维度}（被 ${item.引用.map((ref) => ref.模块).join('、')} 引用）`).join('；')}
            </span>
            <span className="muted">这些维度会让依赖它的下钻路径无法生成分层结果</span>
          </div>
        )}

        <header className="panel-head" style={{ marginTop: 16 }}>
          <div>
            <h3>标准维度版本</h3>
            <p className="page-desc">
              {selectedSource ? `当前查看源维度「${selectedSource}」的历史口径` : '全部源维度的版本记录；点击上方标准结果可按源维度过滤'}
            </p>
          </div>
          <div className="row-actions">
            {selectedSource && (
              <button className="btn ghost" type="button" onClick={() => setSelectedSource(null)}>查看全部</button>
            )}
            <button className="btn" type="button" onClick={() => void reloadVersions(selectedSource)}>刷新版本</button>
          </div>
        </header>
        <table className="data-table">
          <thead>
            <tr>
              <th>版本编号</th>
              <th>源维度</th>
              <th>标准维度</th>
              <th>规则编号</th>
              <th>口径状态</th>
              <th>冲突</th>
              <th>记录时间</th>
              <th>权衡说明</th>
            </tr>
          </thead>
          <tbody>
            {versions.map((row) => (
              <tr key={row.id}>
                <td>{row.版本编号}</td>
                <td>{row.源维度}</td>
                <td>{row.标准维度 ?? '—'}</td>
                <td>{row.规则编号 ?? '—'}</td>
                <td>{row.口径状态}</td>
                <td>{row.冲突 ? '是' : '否'}</td>
                <td>{row.记录时间}</td>
                <td className="page-desc">{row.权衡说明}</td>
              </tr>
            ))}
            {versions.length === 0 && (
              <tr><td className="empty-state" colSpan={8}>该维度还没有版本记录</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <footer className="page-foot">
        <span>共 {total} 条维度映射记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
