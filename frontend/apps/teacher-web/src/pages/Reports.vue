<template>
  <div class="page">
    <page-header title="评价报告" description="回顾课堂分析，结合原始证据整理教学反馈。" eyebrow="REPORTS">
      <template #extra>
        <a-button @click="refreshReports()">刷新列表</a-button>
      </template>
    </page-header>

    <a-alert
      v-if="loadError"
      type="error"
      show-icon
      message="报告列表加载失败"
      :description="loadError"
      style="margin-bottom:16px"
    >
      <template #action>
        <a-button size="small" @click="refreshReports()">重试</a-button>
      </template>
    </a-alert>

    <a-card title="课堂报告">
      <p class="table-scroll-note">左右滑动表格，查看完整信息。</p>
      <a-table
        :dataSource="reports"
        :columns="columns"
        :loading="loading"
        rowKey="id"
        :scroll="{ x: 1200 }"
        :pagination="pagination"
        @change="handleTableChange"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'status'">
            <a-tag v-if="!record.generated" color="default">未生成</a-tag>
            <a-tag v-else :color="record.status === 'published' ? 'success' : 'processing'">
              {{ record.status === 'published' ? '已发布' : '草稿' }}
            </a-tag>
          </template>
          <template v-if="column.key === 'date'">
            {{ formatDate(record.date) }}
          </template>
          <template v-if="column.key === 'climateScore'">
            <span v-if="record.climateScore != null">{{ record.climateScore.toFixed(1) }}分</span>
            <span v-else>--</span>
          </template>
          <template v-if="column.key === 'dataSource'">
            <a-tag :color="record.dataSource === 'DEMO' ? 'orange' : record.dataSource === 'REAL' ? 'green' : 'default'">
              {{ record.dataSource === 'DEMO' ? '演示' : record.dataSource === 'REAL' ? '真实证据' : '无数据' }}
            </a-tag>
          </template>
          <template v-if="column.key === 'action'">
            <a-space>
              <a v-if="record.generated" @click="viewReport(record.id)">查看</a>
              <a-divider v-if="record.generated" type="vertical" />
              <a
                :class="{ disabled: isBusy(record.id, 'generate') }"
                @click="generateReport(record)"
              >
                {{ isBusy(record.id, 'generate') ? '生成中...' : (record.generated ? '重新生成' : '生成报告') }}
              </a>
              <template v-if="record.generated && record.status === 'draft'">
                <a-divider type="vertical" />
                <a
                  :class="{ disabled: !canPublish(record) || isBusy(record.id, 'publish') }"
                  :title="publishBlockedReason(record)"
                  @click="publishReport(record)"
                >
                  {{ isBusy(record.id, 'publish') ? '发布中...' : '发布' }}
                </a>
              </template>
            </a-space>
          </template>
        </template>
      </a-table>
    </a-card>

    <!-- Report Detail Drawer -->
    <a-drawer v-model:open="showDetail" :title="detail?.title || '报告详情'" :width="drawerWidth" placement="right">
      <a-spin :spinning="detailLoading">
        <!-- 历史版本回溯：选项来自 GET /reports/{id}/versions；当前版本走实时接口。
             原生 <select> 而非 a-select：选项渲染在 DOM 里，验收脚本可稳定定位。 -->
        <div v-if="detail || versionError" class="version-bar">
          <label class="version-label" for="report-version-select">版本</label>
          <select
            id="report-version-select"
            ref="versionSelect"
            class="version-select"
            data-testid="report-version-select"
            :value="String(requestedVersion)"
            :disabled="!detail || versionSwitchLoading || (!versionOptions.length && versionsLoading)"
            @change="onVersionChange"
          >
            <option
              v-for="option in versionOptions"
              :key="option.value"
              data-testid="report-version-option"
              :data-version="option.value"
              :value="String(option.value)"
              :title="option.title"
            >{{ option.label }}</option>
          </select>
          <a-tag data-testid="report-version-current" color="blue">当前显示 v{{ displayedVersion }}</a-tag>
          <span v-if="versionSwitchLoading" class="version-loading" data-testid="report-version-loading">切换中…</span>
          <span v-if="versionsLoading && !versions.length" class="version-hint">历史版本加载中…</span>
          <span v-if="versionsError" class="version-error">{{ versionsError }}</span>
          <template v-if="versionError">
            <span class="version-error">{{ versionError }}</span>
            <a-button
              v-if="versionRetryable"
              size="small"
              data-testid="report-version-retry"
              @click="retryVersion()"
            >重试</a-button>
          </template>
        </div>
        <!-- 验收探针：只读 detail.*，不回落到 selectedVersion/requestedVersion。
             同一课堂的两个版本可能只差 0.2 秒，发布时间只到分钟无法区分，且 detail
             缺失时 displayedVersion 会回落到选中版本；因此需要一个「当前渲染的 payload」
             的无回退标记。hidden 元素仍在 DOM 中，可被验收脚本直接读取。 -->
        <div data-testid="report-detail-version" :data-version="detail?.version ?? ''" hidden></div>
        <div data-testid="report-detail-source" :data-source="detail?.dataSource ?? ''" hidden></div>
        <template v-if="detail">
          <a-descriptions size="small" :column="{ xs: 1, sm: 2 }" style="margin-bottom:12px" bordered>
            <a-descriptions-item label="版本">v{{ displayedVersion }}</a-descriptions-item>
            <a-descriptions-item label="状态">
              {{ detail.status === 'published' ? '已发布' : '草稿' }}
            </a-descriptions-item>
            <a-descriptions-item label="发布时间">
              {{ formatDate(detail.publishedAt) }}
            </a-descriptions-item>
            <a-descriptions-item label="生成任务">
              视频 {{ detail.analysisVideoJobId || '--' }} / 语音 {{ detail.analysisAudioJobId || '--' }}
            </a-descriptions-item>
          </a-descriptions>

          <a-alert
            v-if="detail.dataSource === 'DEMO'"
            type="warning"
            show-icon
            message="当前报告使用演示数据"
            description="分数和建议仅用于界面演示，不代表真实课堂评价。"
            style="margin-bottom:16px"
          />

          <a-card size="small" style="margin-bottom:16px">
            <a-row :gutter="16">
              <a-col :span="8">
                <a-statistic
                  v-if="detail.climateScore != null"
                  title="课堂氛围"
                  :value="detail.climateScore"
                  :precision="1"
                  suffix="分"
                />
                <span v-else class="missing-score">该维度数据不足</span>
                <div style="margin-top:8px">
                  <a-tag :color="validationColor(detail.scoreValidation?.climate)">
                    {{ validationLabel(detail.scoreValidation?.climate) }}
                  </a-tag>
                </div>
              </a-col>
              <a-col :span="8">
                <a-statistic
                  v-if="detail.engagementScore != null"
                  title="参与度"
                  :value="detail.engagementScore"
                  :precision="1"
                  suffix="分"
                />
                <span v-else class="missing-score">该维度数据不足</span>
                <div style="margin-top:8px">
                  <a-tag :color="validationColor(detail.scoreValidation?.engagement)">
                    {{ validationLabel(detail.scoreValidation?.engagement) }}
                  </a-tag>
                </div>
              </a-col>
              <a-col :span="8">
                <a-statistic
                  v-if="detail.teachingScore != null"
                  title="教学质量"
                  :value="detail.teachingScore"
                  :precision="1"
                  suffix="分"
                />
                <span v-else class="missing-score">该维度数据不足</span>
                <div style="margin-top:8px">
                  <a-tag :color="validationColor(detail.scoreValidation?.teaching)">
                    {{ validationLabel(detail.scoreValidation?.teaching) }}
                  </a-tag>
                </div>
              </a-col>
            </a-row>
            <p v-if="detail.climateScore == null" style="margin:12px 0 0;color:#667085;font-size:12px">
              课堂氛围需要至少一个同时包含视频与语音的 60 秒窗口；缺少语音或视频时不会用业务数据兜底。
            </p>
            <a-alert
              v-if="detail.scoreValidation?.windowState === 'windows_pending'"
              data-testid="report-window-pending"
              type="warning"
              show-icon
              style="margin-top:12px"
              message="分析任务已成功，但该版本的统一时间窗尚未生成"
              description="对齐可能仍在进行或结果为空。此状态下不会用旧版本的窗口分数代替，请等待对齐完成后重新生成报告。"
            />
          </a-card>

          <a-alert
            type="info"
            show-icon
            style="margin-bottom:16px"
            message="课堂氛围是唯一纳入效度验证的目标维度；参与度/教学质量为工程映射，未做效度验证。"
            description="公开课堂池的 E3–E6 双人标注尚未完成，当前 REAL 课堂氛围分为规则基线 + 晚期融合输出，标记为“待验证”，不代表最终效度结论。"
          />

          <a-card title="证据面板" size="small" style="margin-bottom:16px">
            <template #extra>
              <a-tag v-if="detail.evidence" :color="evidenceColor(detail.evidence.dataSource)">
                {{ evidenceLabel(detail.evidence.dataSource) }}
              </a-tag>
            </template>
            <div v-if="detail.evidence" style="display:flex;flex-direction:column;gap:12px">
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                <a-tag color="blue">模型 {{ detail.evidence.modelVersion }}</a-tag>
                <a-tag
                  v-if="windowStateLabel(detail.scoreValidation?.windowState)"
                  data-testid="report-window-state"
                  :color="detail.scoreValidation?.windowState === 'windows_ready' ? 'green' : 'orange'"
                >{{ windowStateLabel(detail.scoreValidation?.windowState) }}</a-tag>
                <a-tag color="cyan">窗口 {{ detail.evidence.windowCount }}</a-tag>
                <a-tag color="purple">证据来源 {{ detail.evidence.featureCount }}</a-tag>
              </div>
              <div>
                <div style="display:flex;justify-content:space-between;font-size:12px;color:#667085;margin-bottom:4px">
                  <span>证据完整度（窗口数 + 证据来源数 / 8）</span>
                  <span>{{ coveragePercent(detail.evidence.coverage) }}%</span>
                </div>
                <a-progress :percent="coveragePercent(detail.evidence.coverage)" size="small"
                  :status="coverageStatus(detail.evidence.coverage)" />
                <p style="margin:4px 0 0;color:#98A2B3;font-size:12px">
                  该比例只表示证据来源覆盖情况，不代表检测质量，也不代表效度验证结果。
                </p>
              </div>
              <a-collapse ghost>
                <a-collapse-panel key="basis" header="该指标的计算依据">
                  <p style="margin-bottom:4px">参与度：基于 {{ detail.evidence.windowCount }} 个窗口的视频规则分均值（工程映射，未验证）。</p>
                  <p style="margin-bottom:4px">教学质量：基于 {{ detail.evidence.windowCount }} 个窗口的语音规则分均值（工程映射，未验证）。</p>
                  <p style="margin-bottom:0">课堂氛围：基于 {{ detail.evidence.windowCount }} 个窗口的视频 + 语音晚期融合分均值（规则基线 v1，待验证）。</p>
                </a-collapse-panel>
              </a-collapse>
            </div>
            <div v-else style="color:#9CA3AF;font-size:12px">该报告暂无溯源证据信息。</div>
          </a-card>

          <a-card title="报告摘要" size="small" style="margin-bottom:16px">
            <p>{{ detail.summary || '暂无摘要' }}</p>
          </a-card>

          <a-card title="数据覆盖" size="small" style="margin-bottom:16px">
            <pre style="font-size:12px;color:#667085">{{ dataCoverageText }}</pre>
          </a-card>

          <a-card title="课堂行为" size="small" style="margin-bottom:16px">
            <p>{{ detail.behaviorSummary || '暂无数据' }}</p>
          </a-card>

          <a-card title="课堂行为识别证据" size="small" style="margin-bottom:16px">
            <BehaviorEvidenceGallery :key="`${detail.lessonSessionId}:${displayedVersion}`"
              :lesson-id="detail.lessonSessionId" :version="displayedVersion" />
          </a-card>

          <a-card title="教学建议" size="small" v-if="detail.suggestions && detail.suggestions.length > 0">
            <div v-for="s in detail.suggestions" :key="s.id" style="margin-bottom:12px;padding:8px;border-left:3px solid #315FE8;background:#f8f9ff">
              <div style="display:flex;justify-content:space-between">
                <strong>{{ s.category }}</strong>
                <a-tag :color="s.priority === 'high' ? 'red' : s.priority === 'medium' ? 'orange' : 'green'">
                  {{ s.priority === 'high' ? '重要' : s.priority === 'medium' ? '建议' : '参考' }}
                </a-tag>
              </div>
              <p style="margin:4px 0">{{ s.content }}</p>
              <small style="color:#667085">{{ s.evidence }}</small>
              <div style="margin-top:6px;display:flex;align-items:center;gap:8px">
                <a-tag :color="suggestionStatusColor(s.status)">{{ suggestionStatusLabel(s.status) }}</a-tag>
                <a-button
                  v-if="s.status === 'pending'"
                  size="small"
                  type="primary"
                  :loading="adoptingId === s.id"
                  @click="adoptSuggestion(s)"
                >采纳</a-button>
              </div>
            </div>
          </a-card>
        </template>
        <a-empty v-else-if="!detailLoading">
          <template #description>
            <span>{{ versionError || '报告不存在或加载失败' }}</span>
          </template>
        </a-empty>
      </a-spin>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import dayjs from 'dayjs'
import PageHeader from '@/components/PageHeader.vue'
import BehaviorEvidenceGallery from '@/components/BehaviorEvidenceGallery.vue'
import apiClient from '@/api/client'
import {
  adoptSuggestion as adoptSuggestionApi,
  getReportVersion,
  getReportVersions,
  type ReportVersionSummary,
} from '@/api/endpoints'

const reports = ref<any[]>([])
const loading = ref(false)
const loadError = ref('')
const showDetail = ref(false)
const detail = ref<any>(null)
const dataCoverageText = computed(() => {
  let values: Record<string, unknown> = {}
  try {
    const raw = detail.value?.dataCoverage
    values = typeof raw === 'string' ? JSON.parse(raw) : raw || {}
    if (!values || typeof values !== 'object') values = {}
  } catch {
    return '暂无数据覆盖信息'
  }
  return [['behavior', '行为观察'], ['video', '视频分析'], ['speech', '语音分析']]
    .map(([key, label]) => `${label}：${values[key] === true ? '有' : '无'}`).join('\n')
})
const detailLoading = ref(false)
const drawerWidth = 'min(1040px, 100vw)'
const pagination = ref({ current: 1, pageSize: 10, total: 0, showSizeChanger: true })
const actionBusy = ref<Record<string, boolean>>({})
const adoptingId = ref('')
let detailSeq = 0
let versionsSeq = 0

const route = useRoute()
const router = useRouter()

/* ------------------------------------------------------------------
 * 历史版本回溯（P1-1）
 *   后端接口（ReportController 已实现，本页只读）：
 *     GET /reports/{lessonSessionId}/versions      → 已发布版本摘要列表
 *     GET /reports/{lessonSessionId}/versions/{v}  → 指定版本快照，字段同 GET /reports/{lessonSessionId}
 *   当前版本走实时接口 GET /reports/{lessonSessionId}，历史版本走快照接口（见 endpoints.ts）。
 *   选择器用原生 <select> 而非 a-select：a-select 的选项渲染在 portal 中，验收脚本难以稳定定位；
 *   原生 select 可稳定暴露 data-testid="report-version-option" 与 data-version。
 * ------------------------------------------------------------------ */

const versions = ref<ReportVersionSummary[]>([])
const versionsLoading = ref(false)
const versionsError = ref('')
/** 报告当前（实时）版本号，来自 GET /reports/{id}。 */
const liveVersion = ref(1)
/** 抽屉里实际展示的版本号。 */
const selectedVersion = ref(1)
/** 选择器绑定的版本：正常情况下等于 selectedVersion。 */
const requestedVersion = ref(1)
/** 最近一次加载失败的版本，供「重试」使用（失败后选择器会回到已展示版本）。 */
const failedVersion = ref(1)
/** 原生 select 的 DOM 引用：选项晚于 value 插入时浏览器会回退到第一项，需要事后校正。 */
const versionSelect = ref<HTMLSelectElement | null>(null)
const versionSwitchLoading = ref(false)
const versionError = ref('')
const versionErrorStatus = ref(0)
const detailLessonSessionId = ref('')

/** 当前渲染内容对应的版本：以已展示的 detail 为准，避免与选择器状态错位。 */
const displayedVersion = computed<number>(() => detail.value?.version ?? selectedVersion.value)

/** 数据来源 → 版本选项里的中文说明；映射与报告表格的数据来源列保持一致。 */
const DATA_SOURCE_LABELS: Record<string, string> = {
  REAL: '真实证据',
  DEMO: '演示',
  EMPTY: '无数据',
}

/** 旧快照可能没有 dataSource：必须给出明确说明，不能让选项出现空段。 */
function versionDataSourceLabel(dataSource?: string | null) {
  return DATA_SOURCE_LABELS[dataSource || ''] || '来源未记录'
}

function formatVersionOptionLabel(
  version: number,
  status?: string,
  publishedAt?: string,
  dataSource?: string | null,
) {
  const stateLabel = status === 'published' ? '已发布' : '草稿'
  return `v${version} · ${stateLabel} · ${formatDate(publishedAt)} · ${versionDataSourceLabel(dataSource)}`
}

/** 快照列表 + 实时版本（草稿可能没有发布快照，必须补一项，否则默认值不在选项里）。 */
const versionOptions = computed(() => {
  const entries = new Map<number, { value: number; label: string; title: string }>()
  for (const item of versions.value) {
    const version = Number(item?.version)
    if (!Number.isInteger(version) || version < 1) continue
    entries.set(version, {
      value: version,
      label: formatVersionOptionLabel(
        version, item.status, item.publishedAt ?? undefined, item.dataSource,
      ),
      title: item.publishedBy ? `发布人：${item.publishedBy}` : '发布人未记录',
    })
  }
  const live = liveVersion.value
  if (Number.isInteger(live) && live >= 1 && !entries.has(live)) {
    // 没有发布快照的版本（草稿）也要能选回。只在当前展示的就是该版本时才引用它的
    // status / dataSource；否则不借用其它版本的值，按「草稿 · 来源未记录」如实呈现。
    const shownLive = detail.value?.version === live ? detail.value : null
    entries.set(live, {
      value: live,
      label: formatVersionOptionLabel(
        live, shownLive?.status, shownLive?.publishedAt ?? shownLive?.createdAt, shownLive?.dataSource,
      ),
      title: '当前报告（该版本尚未保存发布快照）',
    })
  }
  return [...entries.values()].sort((a, b) => b.value - a.value)
})

// 原生 select 在选项尚未插入时会丢弃 value，浏览器回退到第一项；DOM 更新后按目标版本校正。
watch(
  [versionOptions, requestedVersion],
  () => {
    const el = versionSelect.value
    if (!el) return
    const next = String(requestedVersion.value)
    if (el.value !== next) el.value = next
  },
  { flush: 'post' },
)

/** 403 不提供重试：属主由后端判定，重试同一请求没有意义。 */
const versionRetryable = computed(() => !!versionError.value
  && versionErrorStatus.value !== 403 && versionErrorStatus.value !== 404)

function errorMessage(err: any, fallback: string, notFound = '该版本不存在（404）') {
  const status = err?.response?.status
  if (status === 403) return '无权查看该报告的历史版本（403）'
  if (status === 404) return notFound
  return err?.response?.data?.message || fallback
}

/** 深链接 ?version=N 解析：非法值忽略，回落到报告当前版本。 */
function parseVersionParam(raw: unknown): number | null {
  const value = Array.isArray(raw) ? raw[0] : raw
  if (typeof value !== 'string' && typeof value !== 'number') return null
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 1 ? parsed : null
}

/** 深链接可恢复：把当前报告/版本写回 URL；刷新或分享后回到同一视图。 */
function syncDetailQuery() {
  if (!showDetail.value || !detailLessonSessionId.value) return
  const currentId = typeof route.query.report === 'string' ? route.query.report : ''
  const currentVersion = String(route.query.version ?? '')
  if (currentId === detailLessonSessionId.value && currentVersion === String(selectedVersion.value)) return
  router.replace({
    query: {
      ...route.query,
      report: detailLessonSessionId.value,
      version: String(selectedVersion.value),
    },
  }).catch(() => { /* 重复导航无需处理 */ })
}

function clearDetailQuery() {
  if (typeof route.query.report !== 'string' && route.query.version === undefined) return
  const rest: Record<string, any> = { ...route.query }
  delete rest.report
  delete rest.version
  router.replace({ query: rest }).catch(() => { /* 重复导航无需处理 */ })
}

/** 抽屉关闭：序号自增让所有在途响应失效，避免关闭后被回写。 */
function onDrawerClose() {
  detailSeq++
  versionsSeq++
  detail.value = null
  detailLoading.value = false
  versionSwitchLoading.value = false
  versionError.value = ''
  versionErrorStatus.value = 0
  versions.value = []
  versionsError.value = ''
  versionsLoading.value = false
  detailLessonSessionId.value = ''
  clearDetailQuery()
}

// 唯一的关闭路径：用户点关闭、遮罩关闭、前进/后退清掉 query 都走这里复位并作废在途请求。
watch(showDetail, (open) => {
  if (!open) onDrawerClose()
})

async function loadVersionList(lessonSessionId: string) {
  const seq = ++versionsSeq
  versionsLoading.value = true
  try {
    const list = await getReportVersions(lessonSessionId)
    if (seq !== versionsSeq) return
    versions.value = Array.isArray(list) ? list : []
    versionsError.value = ''
  } catch (err: any) {
    if (seq !== versionsSeq) return
    versions.value = []
    versionsError.value = errorMessage(err, '历史版本列表加载失败', '课堂不存在或无权访问（404）')
  } finally {
    if (seq === versionsSeq) versionsLoading.value = false
  }
}

/**
 * 请求并展示某个版本。
 * switching=true 表示抽屉里已有数据（切版本）：保留旧内容 + 加载遮罩，失败时保留旧内容并给出重试。
 * 请求序号 detailSeq 单调递增：晚到的旧响应不得覆盖新选择。
 */
async function fetchVersion(version: number, seq: number, switching: boolean): Promise<boolean> {
  if (switching) {
    versionSwitchLoading.value = true
    detailLoading.value = true
  }
  try {
    const lessonSessionId = detailLessonSessionId.value
    const payload = version === liveVersion.value
      ? (await apiClient.get('/reports/' + lessonSessionId)).data.data
      : await getReportVersion(lessonSessionId, version)
    if (seq !== detailSeq) return false
    detail.value = payload
    selectedVersion.value = version
    versionError.value = ''
    versionErrorStatus.value = 0
    syncDetailQuery()
    return true
  } catch (err: any) {
    if (seq !== detailSeq) return false
    // 403/404 不自动重试：错误原样呈现给用户。
    versionErrorStatus.value = err?.response?.status || 0
    versionError.value = errorMessage(err, `版本 v${version} 加载失败`)
    failedVersion.value = version
    // 选择器回到“实际展示”的版本：不能出现选择器 v2、内容仍是 v3 的错位状态。
    requestedVersion.value = selectedVersion.value
    message.error(versionError.value)
    return false
  } finally {
    if (seq === detailSeq) {
      detailLoading.value = false
      versionSwitchLoading.value = false
    }
  }
}

async function showVersion(version: number, force = false) {
  if (!showDetail.value || !detail.value) return
  // 已经显示该版本且没有错误时无需重复请求；force 用于「失败后重试同一版本」。
  if (!force && version === selectedVersion.value && !versionError.value) return
  requestedVersion.value = version
  failedVersion.value = version
  versionError.value = ''
  versionErrorStatus.value = 0
  const seq = ++detailSeq
  await fetchVersion(version, seq, true)
}

function onVersionChange(event: Event) {
  const value = Number((event.target as HTMLSelectElement).value)
  if (!Number.isInteger(value) || value < 1) return
  void showVersion(value)
}

function retryVersion() {
  const target = failedVersion.value
  if (!detail.value) {
    // 首次打开就失败：重新按目标版本打开整份报告。
    void viewReport(detailLessonSessionId.value, target)
    return
  }
  // 失败后重试同一版本必须绕过 showVersion 的短路，否则点了没有反应。
  void showVersion(target, true)
}

function busyKey(id: string, action: string) { return `${action}:${id}` }
function isBusy(id: string, action: string) { return !!actionBusy.value[busyKey(id, action)] }
function setBusy(id: string, action: string, value: boolean) {
  actionBusy.value = { ...actionBusy.value, [busyKey(id, action)]: value }
}
function canPublish(record: any) {
  return record.generated && record.status === 'draft'
    && record.climateScore != null && record.dataSource !== 'EMPTY'
}
function publishBlockedReason(record: any) {
  if (!record.generated) return '尚未生成报告'
  if (record.climateScore == null) return '课堂氛围分不可用，不能发布'
  if (record.dataSource === 'EMPTY') return '无有效数据，不能发布'
  return ''
}
function suggestionStatusLabel(status: string) {
  const map: Record<string, string> = {
    pending: '待处理', adopted: '已采纳', accepted: '已接受', ignored: '已忽略',
  }
  return map[status] || status
}
function suggestionStatusColor(status: string) {
  if (status === 'adopted' || status === 'accepted') return 'green'
  if (status === 'ignored') return 'default'
  return 'blue'
}

const VALIDATION_LABELS: Record<string, string> = {
  pending_validation: '规则基线，待验证',
  validated_on_public_data: '已验证（公开数据）',
  engineering_mapping_not_validated: '工程映射，未验证',
  unavailable: '无数据',
  demo_only: '仅演示',
}

/** R11：窗口状态 → 面向教师的中文说明（后端 scoreValidation.windowState）。 */
const WINDOW_STATE_LABELS: Record<string, string> = {
  windows_ready: '窗口已就绪',
  windows_pending: '窗口未生成（数据不足）',
  legacy_no_job: '历史报告（无分析任务）',
}

function windowStateLabel(state?: string) {
  return WINDOW_STATE_LABELS[state || ''] || ''
}

const VALIDATION_COLORS: Record<string, string> = {
  pending_validation: 'orange',
  validated_on_public_data: 'green',
  engineering_mapping_not_validated: 'blue',
  unavailable: '',
  demo_only: 'orange',
}

function validationLabel(status?: string) {
  return VALIDATION_LABELS[status || ''] || '状态未知'
}

function validationColor(status?: string) {
  return VALIDATION_COLORS[status || ''] ?? ''
}

function formatDate(value?: string) {
  return value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '--'
}

function evidenceColor(source?: string) {
  if (source === 'REAL') return 'green'
  if (source === 'DEMO') return 'orange'
  return 'default'
}

function evidenceLabel(source?: string) {
  if (source === 'REAL') return '真实证据'
  if (source === 'DEMO') return '演示数据'
  return '无数据'
}

function coveragePercent(value?: number) {
  return Math.round(Math.min(1, Math.max(0, value || 0)) * 100)
}

function coverageStatus(value?: number) {
  const p = coveragePercent(value)
  if (p >= 80) return 'success'
  if (p >= 50) return 'normal'
  return 'exception'
}

const columns = [
  { title: '课堂', dataIndex: 'title', width: 220 },
  { title: '课程', dataIndex: 'courseName', width: 160 },
  { title: '班级', dataIndex: 'className', width: 180 },
  { title: '日期', key: 'date', width: 160 },
  { title: '课堂氛围', key: 'climateScore', width: 100 },
  { title: '数据来源', key: 'dataSource', width: 100 },
  { title: '状态', key: 'status', width: 80 },
  { title: '操作', key: 'action', width: 200 },
]

async function refreshReports(page = pagination.value.current) {
  loading.value = true
  loadError.value = ''
  try {
    const res = await apiClient.get('/reports/page', {
      params: { page: Math.max(0, page - 1), size: pagination.value.pageSize },
    })
    reports.value = res.data.data.items || []
    pagination.value = {
      ...pagination.value,
      current: page,
      total: res.data.data.total || 0,
    }
  } catch (err: any) {
    loadError.value = err?.response?.data?.message || '请检查网络或登录状态后重试'
  } finally {
    loading.value = false
  }
}

function handleTableChange(paginationInfo: any) {
  pagination.value = { ...pagination.value, current: paginationInfo.current, pageSize: paginationInfo.pageSize }
  void refreshReports(paginationInfo.current)
}

async function viewReport(lessonSessionId: string, version?: number) {
  const seq = ++detailSeq
  showDetail.value = true
  detail.value = null
  detailLoading.value = true
  detailLessonSessionId.value = lessonSessionId
  liveVersion.value = 1
  selectedVersion.value = version ?? 1
  requestedVersion.value = version ?? 1
  versionSwitchLoading.value = false
  versionError.value = ''
  versionErrorStatus.value = 0
  failedVersion.value = version ?? 1
  versions.value = []
  versionsError.value = ''
  void loadVersionList(lessonSessionId)
  try {
    const live = (await apiClient.get('/reports/' + lessonSessionId)).data.data
    if (seq !== detailSeq) return // 旧报告响应晚到，不能覆盖当前抽屉
    liveVersion.value = live?.version ?? 1
    const target = version ?? liveVersion.value
    requestedVersion.value = target
    if (target === liveVersion.value) {
      detail.value = live
      selectedVersion.value = target
      versionError.value = ''
      versionErrorStatus.value = 0
      syncDetailQuery()
    } else {
      // 深链接直接指向历史版本：不先渲染实时版本，避免「新标题 + 旧分数」混排。
      const ok = await fetchVersion(target, seq, false)
      if (!ok && seq === detailSeq) {
        // 目标版本不存在/无权：回落到实时版本，错误说明仍保留在版本条上；
        // 选择器与 URL 都跟实际展示的版本一致，避免「选择器 v2、内容 v3」的错位。
        detail.value = live
        selectedVersion.value = liveVersion.value
        requestedVersion.value = liveVersion.value
        syncDetailQuery()
      }
    }
  } catch (err: any) {
    if (seq !== detailSeq) return
    detail.value = null
    versionErrorStatus.value = err?.response?.status || 0
    versionError.value = errorMessage(err, '报告不存在或加载失败')
    message.error(versionError.value)
  } finally {
    if (seq === detailSeq) detailLoading.value = false
  }
}

async function generateReport(record: any) {
  const lessonSessionId = record.id
  if (isBusy(lessonSessionId, 'generate')) return
  if (record.status === 'published') {
    try {
      await new Promise<void>((resolve, reject) => {
        Modal.confirm({
          title: '重新生成已发布报告？',
          content: '已发布版本会保留为历史快照，新内容将作为新草稿版本，需要重新发布。',
          okText: '生成新草稿',
          cancelText: '取消',
          onOk: () => resolve(),
          onCancel: () => reject(new Error('cancelled')),
        })
      })
    } catch {
      return
    }
  }
  setBusy(lessonSessionId, 'generate', true)
  try {
    await apiClient.post('/reports/generate/' + lessonSessionId)
    message.success('报告已生成')
    await refreshReports()
  } catch (err: any) {
    message.error(err?.response?.data?.message || '生成失败')
  } finally {
    setBusy(lessonSessionId, 'generate', false)
  }
}

async function publishReport(record: any) {
  const lessonSessionId = record.id
  if (isBusy(lessonSessionId, 'publish')) return
  if (!canPublish(record)) {
    message.warning(publishBlockedReason(record) || '当前报告不能发布')
    return
  }
  setBusy(lessonSessionId, 'publish', true)
  try {
    await apiClient.post('/reports/' + lessonSessionId + '/publish')
    message.success('报告已发布，版本快照已保存')
    await refreshReports()
  } catch (err: any) {
    message.error(err?.response?.data?.message || '发布失败')
  } finally {
    setBusy(lessonSessionId, 'publish', false)
  }
}

async function adoptSuggestion(s: any) {
  if (!s?.id || adoptingId.value) return
  adoptingId.value = s.id
  try {
    await adoptSuggestionApi(s.id)
    s.status = 'adopted'
    message.success('已采纳建议')
  } catch (err: any) {
    message.error(err?.response?.data?.message || '采纳失败')
  } finally {
    adoptingId.value = ''
  }
}

onMounted(async () => {
  await refreshReports()
  // 深链接：?report=<lessonSessionId>&version=<n> 直接恢复上次查看的位置。
  const targetId = typeof route.query.report === 'string' ? route.query.report : ''
  if (targetId) void viewReport(targetId, parseVersionParam(route.query.version) ?? undefined)
})

// 浏览器前进/后退：URL 是唯一真源，但本页自己的 router.replace 不应触发重复加载。
watch(
  () => [route.query.report, route.query.version],
  () => {
    const targetId = typeof route.query.report === 'string' ? route.query.report : ''
    if (!targetId) {
      if (showDetail.value) showDetail.value = false
      return
    }
    const parsed = parseVersionParam(route.query.version)
    if (targetId === detailLessonSessionId.value
      && (parsed === null || parsed === requestedVersion.value)) return
    void viewReport(targetId, parsed ?? undefined)
  },
)
</script>

<style scoped>
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.version-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  padding: 8px 10px;
  background: #f8f9ff;
  border: 1px solid #e6e9f5;
  border-radius: 6px;
}
.version-label {
  font-size: 13px;
  color: #475467;
}
.version-select {
  min-width: 220px;
  height: 28px;
  padding: 0 6px;
  font-size: 13px;
  color: #1d2939;
  background: #fff;
  border: 1px solid #d0d5dd;
  border-radius: 4px;
}
.version-select:disabled {
  color: #98A2B3;
  background: #f2f4f7;
}
.version-loading,
.version-hint {
  font-size: 12px;
  color: #667085;
}
.version-error {
  font-size: 12px;
  color: #B42318;
}
.missing-score {
  color: #98A2B3;
  font-size: 13px;
}
</style>
