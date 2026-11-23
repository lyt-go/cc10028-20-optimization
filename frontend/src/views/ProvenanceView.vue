<template>
  <section class="page" data-module="kg_provenance">
    <header class="page-head">
      <div>
        <h2>图谱溯证检索</h2>
        <p class="page-desc">
          按实体检索图谱边的来源证据。一次查询锁定一组来源，翻页或改排序都不会切换证据；
          点选某条证据可以直接重算它所属的抽取来源。
        </p>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="runSearch">
      <label class="filter-item">
        <span>实体</span>
        <input v-model="form.entity" data-testid="provenance-entity" placeholder="如 某科技集团" required />
      </label>
      <label class="filter-item">
        <span>检索范围</span>
        <select v-model="form.scope" data-testid="provenance-scope">
          <option value="published">已发布边（当前边来历）</option>
          <option value="pending">未入图来源（需补抽）</option>
          <option value="all">全部</option>
        </select>
      </label>
      <label class="filter-item">
        <span>文档编号</span>
        <input v-model="form.docId" data-testid="provenance-doc" placeholder="可选" />
      </label>
      <label class="filter-item">
        <span>关系类型</span>
        <input v-model="form.relation" data-testid="provenance-relation" placeholder="可选，如 EMPLOY" />
      </label>
      <label class="filter-item">
        <span>匹配方式</span>
        <select v-model="form.match" data-testid="provenance-match">
          <option value="exact">精确同名</option>
          <option value="contains">包含</option>
        </select>
      </label>
      <label class="filter-item">
        <span>排序</span>
        <select v-model="form.sort" data-testid="provenance-sort">
          <option value="-confidence">置信度从高到低</option>
          <option value="confidence">置信度从低到高</option>
          <option value="doc_id">文档编号</option>
          <option value="observed_at">来源时间</option>
        </select>
      </label>
      <button class="btn primary" data-testid="provenance-query" type="submit" :disabled="loading">检索</button>
      <button class="btn ghost" data-testid="provenance-reset" type="button" @click="reset">重置</button>
    </form>

    <div v-if="result" class="stat-row">
      <article class="stat-card">
        <span class="stat-label">证据总量（本查询）</span>
        <strong class="stat-value" data-testid="provenance-total">{{ result.total }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已发布边</span>
        <strong class="stat-value">{{ result.counts.published }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">未入图来源</span>
        <strong class="stat-value">{{ result.counts.pending }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">涉及文档</span>
        <strong class="stat-value">{{ result.counts.documents }}</strong>
      </article>
    </div>

    <p v-if="result" class="provenance-pin" data-testid="provenance-pin">
      本次查询固定来源集合 <code>{{ result.snapshot_id }}</code>，共
      {{ result.source_set_size }} 条；翻页与排序不会更换来源集合。
    </p>

    <table class="data-table">
      <thead>
        <tr>
          <th>选择</th>
          <th>证据编号</th>
          <th>范围</th>
          <th>文档</th>
          <th>头实体</th>
          <th>关系</th>
          <th>尾实体</th>
          <th>置信度</th>
          <th>来源分块</th>
          <th>抽取版本</th>
          <th>图谱版本</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in result?.items ?? []"
          :key="item.evidence_id"
          :data-testid="`provenance-row-${item.evidence_id}`"
          :data-scope="item.scope"
        >
          <td>
            <input
              type="checkbox"
              :data-testid="`provenance-pick-${item.evidence_id}`"
              :checked="selected.has(item.evidence_id)"
              @change="toggle(item.evidence_id, ($event.target as HTMLInputElement).checked)"
            />
          </td>
          <td><code>{{ item.evidence_id }}</code></td>
          <td>{{ scopeLabel(item.scope) }}</td>
          <td>{{ item.doc_id }}</td>
          <td>{{ item.head }}</td>
          <td>{{ item.relation }}</td>
          <td>{{ item.tail }}</td>
          <td>{{ item.confidence.toFixed(2) }}</td>
          <td>{{ item.chunk_ids.join('、') || '—' }}</td>
          <td>{{ item.extraction_version }}</td>
          <td>{{ item.graph_version || '—' }}</td>
        </tr>
        <tr v-if="result && !result.items.length">
          <td colspan="11" class="empty-state">该实体在当前范围内没有证据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>
        第 {{ result?.page ?? 1 }} 页 / 共 {{ result?.total ?? 0 }} 条
        <button
          class="link"
          data-testid="provenance-prev"
          type="button"
          :disabled="!result || result.page <= 1 || loading"
          @click="turnPage(-1)"
        >上一页</button>
        <button
          class="link"
          data-testid="provenance-next"
          type="button"
          :disabled="!result || result.page * result.size >= result.total || loading"
          @click="turnPage(1)"
        >下一页</button>
      </span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <p v-if="selectedWarning" class="error-text" data-testid="provenance-select-warning">
      {{ selectedWarning }}
    </p>

    <section class="recompute-panel" data-testid="provenance-recompute-panel">
      <h3>来源重算</h3>
      <p class="page-desc">
        只会重算勾选的证据。未勾选的文档、以及同一文档内未勾选的实体对都不会被写入。
      </p>
      <button
        class="btn primary"
        data-testid="provenance-recompute"
        type="button"
        :disabled="!selected.size || !result || loading"
        @click="recompute"
      >
        重算所选来源（{{ selected.size }}）
      </button>
      <div v-if="recomputeResult" class="recompute-result" data-testid="provenance-recompute-result">
        <p>
          批次 <code>{{ recomputeResult.batch }}</code>：写入
          {{ recomputeResult.records_written }} 条，更新
          {{ recomputeResult.updated_relations }} 个实体对，涉及文档
          {{ recomputeResult.documents.join('、') || '无' }}。
        </p>
        <ul>
          <li v-for="detail in recomputeResult.details" :key="detail.doc_id">
            {{ detail.doc_id }}：重算 {{ detail.recomputed_pairs.length }} 对，未改动
            {{ detail.other_pairs.length }} 对；产物 {{ detail.output_file }}
          </li>
        </ul>
      </div>
    </section>

    <p class="page-foot">
      <span>
        未入图来源说明：新到的关系来源尚未进入已发布图时，会出现在「未入图来源」范围里，
        选中后重算可补齐对应实体对的证据。
      </span>
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { recomputeProvenance, searchProvenance } from '@/api/backend-service'
import type { ProvenanceSearchResult, RecomputeResult } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
void props.moduleKey

const form = reactive({
  entity: '',
  scope: 'published',
  docId: '',
  relation: '',
  match: 'exact',
  sort: '-confidence',
})
const result = ref<ProvenanceSearchResult | null>(null)
const recomputeResult = ref<RecomputeResult | null>(null)
const selected = ref(new Set<string>())
const loading = ref(false)
const errorMessage = ref('')

const selectedWarning = computed(() => {
  if (!result.value || !selected.value.size) return ''
  const visible = new Set(result.value.items.map((item) => item.evidence_id))
  const hidden = [...selected.value].filter((id) => !visible.has(id))
  if (!hidden.length) return ''
  return `有 ${hidden.length} 条已勾选证据不在当前页，仍属于同一来源集合，重算时会一并用上。`
})

function scopeLabel(scope: string): string {
  if (scope === 'published') return '已发布边'
  if (scope === 'pending') return '未入图来源'
  return scope
}

function toggle(evidenceId: string, checked: boolean) {
  const next = new Set(selected.value)
  if (checked) next.add(evidenceId)
  else next.delete(evidenceId)
  selected.value = next
}

async function runSearch() {
  if (!form.entity.trim()) {
    errorMessage.value = '请先填写要检索的实体'
    return
  }
  loading.value = true
  errorMessage.value = ''
  recomputeResult.value = null
  try {
    // 新条件一律重新检索：一次检索对应一个固定的来源集合快照。
    result.value = await searchProvenance({
      entity: form.entity.trim(),
      scope: form.scope,
      docId: form.docId.trim() || undefined,
      relation: form.relation.trim() || undefined,
      match: form.match,
      sort: form.sort,
      page: 1,
      size: 10,
    })
    selected.value = new Set()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '检索失败'
  } finally {
    loading.value = false
  }
}

async function turnPage(delta: number) {
  if (!result.value) return
  const target = result.value.page + delta
  if (target < 1) return
  loading.value = true
  errorMessage.value = ''
  try {
    result.value = await searchProvenance({
      entity: result.value.entity,
      scope: result.value.scope,
      docId: result.value.doc_id || undefined,
      relation: result.value.relation || undefined,
      match: result.value.match,
      sort: form.sort,
      page: target,
      size: result.value.size,
      snapshotId: result.value.snapshot_id,
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '翻页失败'
  } finally {
    loading.value = false
  }
}

async function recompute() {
  if (!result.value || !selected.value.size) return
  loading.value = true
  errorMessage.value = ''
  try {
    recomputeResult.value = await recomputeProvenance({
      snapshotId: result.value.snapshot_id,
      evidenceIds: [...selected.value],
      reason: '图谱溯证检索后点选重算',
    })
    selected.value = new Set()
    // 重算后重新检索：新证据会进入一组新的来源集合，旧快照保持可回查。
    await runSearch()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重算失败'
  } finally {
    loading.value = false
  }
}

function reset() {
  form.entity = ''
  form.scope = 'published'
  form.docId = ''
  form.relation = ''
  form.match = 'exact'
  form.sort = '-confidence'
  result.value = null
  recomputeResult.value = null
  selected.value = new Set()
  errorMessage.value = ''
}

onMounted(() => {
  void runSearch()
})
</script>
