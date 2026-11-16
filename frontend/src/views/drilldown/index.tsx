import { useCallback, useEffect, useMemo, useState } from 'react'

import { fetchJson, request } from '@/api/client'

const ENDPOINT = '/api/drilldown'
const columns = [
  "路径编号",
  "起始维度",
  "下钻层级序列",
  "每层聚合粒度",
  "默认度量",
  "路径深度",
  "缓存秒数",
  "数据新鲜度"
]
const actions = [
  "提交路径评审",
  "启用路径",
  "归档路径"
]
const statuses = [
  "草稿",
  "路径评审中",
  "已启用",
  "已归档"
]
const stats = [
  {
    "label": "下钻路径总数",
    "value": 0
  },
  {
    "label": "已启用路径",
    "value": 0
  },
  {
    "label": "评审中路径",
    "value": 0
  }
]
const sortOptions = ["层级序号", "度量值", "维度路径", "数据截止时间", "记录数"]

type Row = Record<string, string | number | null>

type LayerRow = {
  id: number
  层级序号: number
  层级维度: string
  维度路径: string
  度量: string
  度量值: number
  记录数: number
  数据截止时间: string
  标准维度: string
  标准维度版本编号: string
  口径指纹: string
}

type Snapshot = {
  id: number
  快照编号: string
  路径编号: string
  版本序号: number
  起始维度: string
  默认度量: string
  层级序列: string[]
  层级数: number
  明细行数: number
  数据新鲜度: string
  来源批次: string[]
  层汇总: {
    层级序号: number
    层级路径: string
    明细行数: number
    度量小计: number
    数据截止时间: string
    标准维度: string
    标准维度版本编号: string
    冲突: boolean
  }[]
  版本绑定: {
    层级序号: number
    维度名: string
    标准维度: string
    标准维度版本编号: string
    冲突: boolean
    权衡说明: string
  }[]
}

type CheckResult = {
  结论: string
  口径漂移: boolean
  路径配置已变更: boolean
  层级核对: {
    层级序号: number
    维度名: string
    快照绑定版本编号: string
    当前版本编号: string
    快照标准维度: string
    当前标准维度: string
    状态: string
  }[]
}

type LayerResult = {
  snapshot: Snapshot
  核对: CheckResult | null
  层级序列: string[]
  定位层级: number | null
  items: LayerRow[]
  total: number
  page: number
  size: number
  排序: { 字段: string; 方向: string }
}

export default function DrilldownView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [activeSnapshot, setActiveSnapshot] = useState<Snapshot | null>(null)
  const [layerResult, setLayerResult] = useState<LayerResult | null>(null)
  const [check, setCheck] = useState<CheckResult | null>(null)
  const [layerFilter, setLayerFilter] = useState({ level: '', locate: '', sortField: sortOptions[0], sortOrder: 'asc' })
  const [busy, setBusy] = useState(false)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('下钻路径列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '多维下钻列表读取失败')
    }
  }, [filters])

  const reloadSnapshots = useCallback(async () => {
    const payload = await fetchJson<{ items: Snapshot[] }>(`${ENDPOINT}/snapshots?size=50`)
    setSnapshots(payload.items ?? [])
    return payload.items ?? []
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    reloadSnapshots().catch(() => undefined)
  }, [reloadSnapshots])

  async function loadLayers(snapshot: Snapshot, overrides?: Partial<typeof layerFilter>) {
    const next = { ...layerFilter, ...overrides }
    setActiveSnapshot(snapshot)
    const query = new URLSearchParams()
    query.set('sort_field', next.sortField)
    query.set('sort_order', next.sortOrder)
    query.set('size', '100')
    if (next.level) query.set('level', next.level)
    if (next.locate) query.set('locate', next.locate)
    try {
      const payload = await fetchJson<LayerResult>(`${ENDPOINT}/snapshots/${snapshot.id}/layers?${query.toString()}`)
      setLayerResult(payload)
      setCheck(payload.核对)
      setErrorMessage('')
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分层聚合明细读取失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('多维下钻动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '多维下钻操作失败')
    }
  }

  async function generateSnapshot(row: Row) {
    setBusy(true)
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/snapshots`, {
        method: 'POST',
        body: JSON.stringify({ values: { 提交人: '值班管理员' } }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '快照生成未被接纳')
      }
      await reloadSnapshots()
      await loadLayers(payload.entry as Snapshot, { level: '', locate: '' })
      setErrorMessage(payload.message)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '快照生成失败')
    } finally {
      setBusy(false)
    }
  }

  const drift = check?.口径漂移 || check?.路径配置已变更

  return (
    <section className="page" data-module="drilldown">
      <header className="page-head">
        <div>
          <h2>多维下钻管理</h2>
          <p className="page-desc">定义多维度数据下钻的分析路径，从高层汇总到明细逐层探索；每条路径可生成带维度映射版本的下钻路径版本快照。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('下钻路径登记入口尚未接入审批流')}>
            登记下钻路径
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出多维下钻清单
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
          <span className="stat-label">已生成路径版本快照</span>
          <strong className="stat-value">{snapshots.length}</strong>
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
            <th>版本快照</th>
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
              <td className="row-actions">
                <button
                  className="link"
                  type="button"
                  disabled={busy}
                  onClick={() => void generateSnapshot({ ...row })}
                  title="绑定每层的标准维度版本并生成分层聚合明细数据集"
                >
                  生成路径版本快照
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无多维下钻数据，可先登记下钻路径</td>
            </tr>
          )}
        </tbody>
      </table>

      <section className="snapshot-panel">
        <header className="panel-head">
          <div>
            <h3>下钻路径版本快照</h3>
            <p className="page-desc">每一层都绑定一个标准维度版本；维度口径变化后，历史快照仍按生成时的口径展示并标记漂移。</p>
          </div>
          <button className="btn" type="button" onClick={() => void reloadSnapshots()}>刷新快照</button>
        </header>

        {snapshots.length === 0 && <p className="empty-state">还没有路径版本快照，先在上表对已启用路径生成快照。</p>}

        <div className="snapshot-list">
          {snapshots.map((snapshot) => (
            <button
              key={snapshot.id}
              type="button"
              className={`snapshot-chip${activeSnapshot?.id === snapshot.id ? ' active' : ''}`}
              onClick={() => void loadLayers(snapshot, { level: '', locate: '' })}
            >
              <strong>{snapshot.快照编号}</strong>
              <span>{snapshot.路径编号} V{snapshot.版本序号}</span>
              <span>{snapshot.层级序列.join(' → ')}</span>
              <span>新鲜度 {snapshot.数据新鲜度 || '—'}</span>
            </button>
          ))}
        </div>

        {activeSnapshot && (
          <>
            <div className={`drift-banner${drift ? ' warn' : ''}`}>
              <span>{check?.结论 ?? '正在核对快照口径…'}</span>
              {check && (
                <span className="muted">
                  {check.口径漂移 ? '存在口径漂移' : '口径一致'}
                  {check.路径配置已变更 ? ' · 路径层级序列已变更' : ''}
                </span>
              )}
            </div>

            <table className="data-table">
              <thead>
                <tr>
                  <th>层级</th>
                  <th>层级路径</th>
                  <th>明细行数</th>
                  <th>度量小计</th>
                  <th>数据截止时间</th>
                  <th>标准维度</th>
                  <th>绑定版本</th>
                  <th>当前版本</th>
                  <th>口径状态</th>
                </tr>
              </thead>
              <tbody>
                {activeSnapshot.层汇总.map((layer) => {
                  const binding = check?.层级核对.find((item) => item.层级序号 === layer.层级序号)
                  return (
                    <tr key={layer.层级序号}>
                      <td>{layer.层级序号}</td>
                      <td>{layer.层级路径}</td>
                      <td>{layer.明细行数}</td>
                      <td>{layer.度量小计}</td>
                      <td>{layer.数据截止时间 || '—'}</td>
                      <td>
                        {layer.标准维度}
                        {layer.冲突 && <span className="tag warn-tag">口径冲突已裁决</span>}
                      </td>
                      <td>{layer.标准维度版本编号 ?? '—'}</td>
                      <td>{binding?.当前版本编号 ?? '—'}</td>
                      <td className={binding && binding.状态 !== '一致' ? 'error-text' : ''}>{binding?.状态 ?? '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>

            <form
              className="filter-bar"
              onSubmit={(event) => { event.preventDefault(); void loadLayers(activeSnapshot) }}
            >
              <label className="filter-item">
                <span>起始维度</span>
                <input value={activeSnapshot.起始维度} readOnly />
              </label>
              <label className="filter-item">
                <span>只看层级</span>
                <select
                  value={layerFilter.level}
                  onChange={(event) => {
                    const value = event.target.value
                    setLayerFilter((prev) => ({ ...prev, level: value }))
                    void loadLayers(activeSnapshot, { level: value, locate: '' })
                  }}
                >
                  <option value="">全部层级</option>
                  {activeSnapshot.层级序列.map((item, index) => (
                    <option key={item} value={index + 1}>{index + 1}. {item}</option>
                  ))}
                </select>
              </label>
              <label className="filter-item">
                <span>定位分组值</span>
                <input
                  value={layerFilter.locate}
                  placeholder="如 深圳"
                  onChange={(event) => setLayerFilter((prev) => ({ ...prev, locate: event.target.value }))}
                />
              </label>
              <label className="filter-item">
                <span>排序字段</span>
                <select
                  value={layerFilter.sortField}
                  onChange={(event) => {
                    const value = event.target.value
                    setLayerFilter((prev) => ({ ...prev, sortField: value }))
                    void loadLayers(activeSnapshot, { sortField: value })
                  }}
                >
                  {sortOptions.map((option) => <option key={option} value={option}>{option}</option>)}
                </select>
              </label>
              <button
                className="btn"
                type="button"
                onClick={() => {
                  const next = layerFilter.sortOrder === 'asc' ? 'desc' : 'asc'
                  setLayerFilter((prev) => ({ ...prev, sortOrder: next }))
                  void loadLayers(activeSnapshot, { sortOrder: next })
                }}
              >
                {layerFilter.sortOrder === 'asc' ? '升序' : '降序'}
              </button>
              <button className="btn" type="submit">应用</button>
            </form>

            <table className="data-table">
              <thead>
                <tr>
                  <th>层级</th>
                  <th>层级维度</th>
                  <th>维度路径</th>
                  <th>{layerResult?.snapshot.默认度量 ?? '度量值'}</th>
                  <th>记录数</th>
                  <th>数据截止时间</th>
                  <th>绑定版本</th>
                </tr>
              </thead>
              <tbody>
                {(layerResult?.items ?? []).map((layer) => (
                  <tr key={layer.id}>
                    <td>{layer.层级序号}</td>
                    <td>{layer.层级维度}</td>
                    <td>{layer.维度路径}</td>
                    <td>{layer.度量值}</td>
                    <td>{layer.记录数}</td>
                    <td>{layer.数据截止时间 || '—'}</td>
                    <td>{layer.标准维度} · {layer.标准维度版本编号 ?? '—'}</td>
                  </tr>
                ))}
                {(layerResult?.items ?? []).length === 0 && (
                  <tr>
                    <td className="empty-state" colSpan={7}>
                      {layerResult?.定位层级 ? `已定位到第 ${layerResult.定位层级} 层以下` : '当前条件下没有分层聚合明细'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
            {layerResult && (
              <p className="page-desc">
                共 {layerResult.total} 条明细 · 排序 {layerResult.排序.字段}（{layerResult.排序.方向}）
                {layerResult.定位层级 ? ` · 定位层级 ${layerResult.定位层级}` : ''}
              </p>
            )}
          </>
        )}
      </section>

      <footer className="page-foot">
        <span>共 {total} 条多维下钻记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
