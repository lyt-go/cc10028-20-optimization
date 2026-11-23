import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  EntryRow,
  OverviewResult,
  PageResult,
  ProvenanceSearchResult,
  RecomputeResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 图谱溯证检索：翻页时把 snapshot_id 带回去，保证始终落在同一组证据上。 */
export async function searchProvenance(params: {
  entity: string
  scope?: string
  docId?: string
  relation?: string
  match?: string
  page?: number
  size?: number
  sort?: string
  snapshotId?: string
}): Promise<ProvenanceSearchResult> {
  const query = new URLSearchParams()
  query.set('entity', params.entity)
  if (params.scope) query.set('scope', params.scope)
  if (params.docId) query.set('doc_id', params.docId)
  if (params.relation) query.set('relation', params.relation)
  if (params.match) query.set('match', params.match)
  if (params.page) query.set('page', String(params.page))
  if (params.size) query.set('size', String(params.size))
  if (params.sort) query.set('sort', params.sort)
  if (params.snapshotId) query.set('snapshot_id', params.snapshotId)
  const response = await request(`/api/kg/provenance/search?${query.toString()}`)
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: '图谱溯证检索失败' }))
    throw new Error(String(detail.detail ?? '图谱溯证检索失败'))
  }
  return await response.json()
}

/** 用检索快照里点选的证据重算来源；只传选中项，未选文档不会被写入。 */
export async function recomputeProvenance(payload: {
  snapshotId: string
  evidenceIds: string[]
  reason?: string
}): Promise<RecomputeResult> {
  const response = await request('/api/kg/provenance/recompute', {
    method: 'POST',
    body: JSON.stringify({
      snapshot_id: payload.snapshotId,
      evidence_ids: payload.evidenceIds,
      reason: payload.reason ?? '',
    }),
  })
  const data = await response.json()
  if (!response.ok) throw new Error(String(data.detail ?? '来源重算失败'))
  return data as RecomputeResult
}
