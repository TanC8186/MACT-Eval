<template>
  <div class="page analysis-page">
    <page-header title="课堂分析" description="上传课堂素材，回看识别画面，核对分析结果。" eyebrow="CLASSROOM ANALYSIS">
      <template #extra>
        <a-space>
          <a-select v-model:value="selectedSessionId" placeholder="选择课堂，可输入名称搜索" aria-label="选择课堂" show-search option-filter-prop="label" style="width:280px"
            :options="sessionOptions" @change="onSessionChange" />
          <a-tag v-if="sessionStatus" :color="sessionStatus === 'IN_PROGRESS' ? 'processing' : 'default'">
            {{ sessionStatus === 'IN_PROGRESS' ? '进行中' : sessionStatus === 'SCHEDULED' ? '待开始' : sessionStatus === 'COMPLETED' ? '已结束' : sessionStatus === 'CANCELLED' ? '已取消' : sessionStatus }}
          </a-tag>
          <a-button v-if="sessionStatus === 'SCHEDULED'" type="primary" @click="startSession" :loading="startingSession">
            <CaretRightOutlined /> 开始课堂
          </a-button>
        </a-space>
      </template>
    </page-header>

    <a-alert
      v-if="demoMode"
      type="warning"
      show-icon
      message="演示模式"
      description="当前实时指标由本地模拟数据生成，不代表真实课堂分析结果。移除地址中的 demo=1 可关闭演示数据。"
      style="margin-bottom: 16px"
    />

    <a-alert
      v-if="socketError"
      type="warning"
      show-icon
      message="实时连接异常"
      :description="socketError + '。页面仍可通过轮询更新分析结果。'"
      style="margin-bottom: 16px"
    >
      <template #action>
        <a-button size="small" @click="connectWS">重新连接</a-button>
      </template>
    </a-alert>

    <a-alert
      v-if="dataError"
      type="error"
      show-icon
      message="课堂数据加载失败"
      :description="dataError"
      style="margin-bottom: 16px"
    >
      <template #action>
        <a-button size="small" @click="reloadCurrentLesson">重试</a-button>
      </template>
    </a-alert>

    <template v-if="selectedSessionId">
      <div class="analysis-flow" aria-label="分析流程"><span><i>1</i> 选择课堂</span><RightOutlined /><span><i>2</i> 上传素材</span><RightOutlined /><span><i>3</i> 复核结果</span></div>
      <section class="analysis-upload-grid">
      <!-- 课堂媒体上传：上传→异步分析→结果落库 (视频/音频) -->
            <a-card title="上传课堂素材" size="small" class="upload-card">
              <template #extra>
                <span class="upload-header-note">支持视频与音频</span>
              </template>
              <a-space direction="vertical" style="width:100%">
                <a-upload-dragger
                  :before-upload="handleMediaBeforeUpload"
                  :show-upload-list="false"
                  accept=".mp4,.avi,.mov,.mkv,.wav,.mp3,.m4a,.flac"
                  :multiple="false"
                  :disabled="mediaUploading"
                >
                  <p class="ant-upload-drag-icon">
                    <CloudUploadOutlined style="font-size:32px;color:#315FE8" />
                  </p>
                  <p class="ant-upload-text">将文件拖到这里，或点击选择</p>
                  <p class="ant-upload-hint">
                    MP4、MOV 等视频 / WAV、MP3 等音频 · 单个文件最大 2 GB
                  </p>
                </a-upload-dragger>
                <div v-if="mediaFile" class="selected-media">
                  <a-tag :color="mediaType === 'video' ? 'blue' : 'purple'">
                    {{ mediaType === 'video' ? '视频' : '音频' }}
                  </a-tag>
                  <span class="selected-media-name">{{ mediaFile.name }}（{{ formatMediaSize(mediaFile.size) }}）</span>
                  <a-button size="small" type="text" danger @click="clearMediaFile" :disabled="mediaUploading">移除</a-button>
                </div>
                <div class="upload-actions">
                  <a-button
                    type="primary"
                    :loading="mediaUploading"
                    :disabled="!mediaFile || mediaUploading || !selectedSessionId"
                    @click="startMediaUpload"
                  >
                    <template #icon><CloudUploadOutlined /></template>
                    {{ mediaUploading ? '上传分析中...' : '上传并分析' }}
                  </a-button>
                  <a-tag v-if="mediaJobStatus" :color="mediaJobStatusColor(mediaJobStatus)">
                    {{ mediaJobStatusLabel(mediaJobStatus) }}
                  </a-tag>
                  <a-button v-if="mediaUploading" size="small" danger @click="cancelMediaUpload">取消上传</a-button>
                </div>
                <div v-if="mediaJobs.length" class="media-job-list">
                  <div v-for="job in mediaJobs" :key="job.id" class="media-job-row">
                    <a-tag :color="job.mediaType === 'video' ? 'blue' : 'purple'">
                      {{ job.mediaType === 'video' ? '视频' : '音频' }}
                    </a-tag>
                    <span class="media-job-name">{{ job.fileName }}</span>
                    <a-tag :color="mediaJobStatusColor(job.status)">
                      {{ mediaJobStatusLabel(job.status) }}
                    </a-tag>
                    <span v-if="job.status === 'UPLOADING'" class="media-job-hint">{{ job.progress }}%</span>
                    <span v-if="job.error" class="media-job-error" :title="job.error">{{ job.error }}</span>
                    <a v-if="job.status === 'FAILED'" @click="retryMediaJob(job.id)">重试</a>
                  </div>
                </div>
                <a-progress
                  v-if="mediaUploading || mediaProgress > 0"
                  :percent="mediaProgress"
                  :status="mediaProgress >= 100 ? 'active' : 'active'"
                  :stroke-color="{ from: '#315FE8', to: '#87d068' }"
                  style="width:100%"
                />
              </a-space>
            </a-card>


      <aside class="analysis-sidebar">
        <a-card title="本节分析概况" class="analysis-status-card" size="small">
          <div class="status-item"><span>数据质量</span><strong :style="{ color: qualityBorderColor }">{{ dataQualityLabel }}</strong></div>
          <div class="status-item"><span>结果连接</span><strong :class="{ 'connection-live': wsConnected }">{{ socketLabel }}</strong></div>
          <div class="status-item"><span>观测覆盖</span><strong>{{ behaviorWindows.length ? coverage + '%' : '不可用' }}</strong></div>
          <div class="status-item"><span>可观测抬头姿态</span><strong>{{ uprightRate === null ? '不可用' : uprightRate + '%' }}</strong></div>
          <p class="status-note">未获得可靠测量的指标保留为不可用。</p>
        </a-card>
        <div class="review-guide"><span class="guide-icon"><SafetyCertificateOutlined /></span><h3>先看证据，再做判断</h3><p>行为标签是模型给出的候选结果。请结合原始画面检查误检、漏检，再参考分析报告。</p><router-link :to="'/reports?report=' + selectedSessionId">前往评价报告 <ArrowRightOutlined /></router-link></div>
      </aside>
      </section>
      <a-collapse v-if="!demoMode" ghost class="model-information"><a-collapse-panel key="model" header="识别范围与数据说明"><p>当前行为模型提供举手、阅读、书写、讨论、低头和转头的候选识别画面，需结合原画面复核。</p><p>已停用根据人物框位置推断坐姿、站立和离座的规则；缺少可靠测量的姿态指标与视频评分显示为不可用。</p></a-collapse-panel></a-collapse>

      <a-tabs v-model:activeKey="analysisTab" class="analysis-tabs">
        <a-tab-pane key="evidence" tab="识别画面与分析结果">
      <!-- Evidence and aligned score windows -->
      <a-card title="课堂窗口与识别画面" size="small" style="margin-top: 16px">
        <template #extra>
          <a-tag color="blue" v-if="windowFeatures.length > 0">
            {{ windowFeatures.length }} 个窗口
          </a-tag>
        </template>
        <v-chart
          v-if="windowFeatures.length > 0"
          :option="windowChartOption"
          :autoresize="true"
          style="width:100%;height:200px"
        />
        <div v-if="windowFeatures.length === 0" style="text-align:center;color:#9CA3AF;padding:20px">暂无窗口融合数据</div>
        <div v-for="win in windowFeatures" :key="win.windowIndex" class="window-quality-row">
          <span>窗口 {{ win.windowIndex }}</span>
          <a-tag :color="win.videoFeatures?.data_quality === 'usable' ? 'green' : win.videoFeatures?.data_quality === 'limited' ? 'orange' : 'red'">
            {{ win.videoFeatures?.data_quality === 'usable' ? '视频可用' : win.videoFeatures?.data_quality === 'limited' ? '视频受限' : '视频不可用' }}
          </a-tag>
          <span v-if="win.videoFeatures?.dominant_scene">场景：{{ win.videoFeatures.dominant_scene }}</span>
          <span v-if="win.videoScore === null" class="window-quality-reason">该窗口未生成视频评分</span>
          <div v-if="win.evidenceFrames?.length" class="window-evidence-frames">
            <div v-for="frame in win.evidenceFrames" :key="frame.id" class="evidence-frame">
              <a-image
                v-if="frame.url"
                :src="frame.url"
                :width="112"
                :height="72"
                :preview="{ src: frame.url }"
                class="evidence-frame-image"
              />
              <div v-else class="evidence-frame-missing">预览不可用</div>
              <span class="evidence-frame-label">{{ frame.label }}</span>
            </div>
          </div>
        </div>
        <div v-if="hasEvidenceFrames" class="evidence-frame-note">
          以上画面由自动抽帧生成，仅用于教师核对识别是否准确；画面含课堂学生影像，请勿外传。
        </div>
      </a-card>

        </a-tab-pane>
        <a-tab-pane key="observations" tab="姿态观测">            <!-- Behavior Trend -->
            <a-card title="行为观察趋势" size="small" style="margin-bottom:16px">
        <div v-if="!hasPostureHistory" style="text-align:center;color:#9CA3AF;padding:20px">暂无可靠姿态测量，可切换到“识别画面与分析结果”复核。</div>
        <div v-else class="trend-container">
          <div v-for="(b, i) in behaviorHistory.slice(-12)" :key="i" class="trend-bar-wrapper">
            <div style="font-size:10px;color:#9CA3AF;text-align:center">{{ b.time }}</div>
            <div class="trend-bar-bg">
              <div class="trend-bar" :style="{ height: (b.upright ?? 0) + '%', background: barColor(b.upright ?? 0) }">
                <span v-if="b.upright !== null && b.upright > 20" style="font-size:9px;color:#fff;writing-mode:vertical-lr">{{ b.upright }}%</span>
              </div>
            </div>
          </div>
        </div>
        <div style="font-size:11px;color:#667085;margin-top:8px;text-align:center">
          缺失姿态测量保留为不可用，行为标签请结合“识别画面与分析结果”复核。
        </div>
      </a-card>

      <!-- Behavior observation timeline -->
      <a-card title="行为观察窗口" size="small" style="margin-bottom:16px">
        <template #extra>
          <a-tag color="blue">最近 {{ behaviorWindows.length }} 条记录</a-tag>
        </template>
        <a-timeline v-if="behaviorWindows.length > 0">
          <a-timeline-item v-for="(win, i) in behaviorWindows" :key="i" :color="win.upright === null ? 'gray' : win.upright >= 70 ? 'green' : win.upright >= 50 ? 'orange' : 'red'">
            <template #dot>
              <span style="font-size:12px;font-weight:600">{{ win.time }}</span>
            </template>
            <div style="display:flex;gap:16px;align-items:center">
              <span>抬头姿态: <strong>{{ win.upright === null ? '不可用' : win.upright + '%' }}</strong></span>
              <span>覆盖: <strong>{{ win.coverage }}%</strong></span>
              <a-tag :color="win.quality === 'usable' ? 'green' : win.quality === 'limited' ? 'orange' : 'red'">
                {{ win.quality === 'usable' ? '良好' : win.quality === 'limited' ? '受限' : '不足' }}
              </a-tag>
            </div>
          </a-timeline-item>
        </a-timeline>
        <div v-else style="text-align:center;color:#9CA3AF;padding:20px">暂无观测数据</div>
      </a-card>

</a-tab-pane>
        <a-tab-pane key="speech" tab="语音指标">      <!-- Speech metrics (aligned window features summary) -->
      <a-card title="语音话语指标" size="small" style="margin-top: 16px">
        <a-descriptions :column="{ xs: 1, sm: 3 }" size="small">
          <a-descriptions-item label="话语覆盖率">
            {{ speechMetrics ? (speechMetrics.speechCoverageAvg * 100).toFixed(1) + '%' : '-' }}
          </a-descriptions-item>
          <a-descriptions-item label="提问句数">
            {{ speechMetrics ? speechMetrics.totalQuestionCount : '-' }}
          </a-descriptions-item>
          <a-descriptions-item label="总话语时长">
            {{ speechMetrics ? speechMetrics.totalSpeechSeconds.toFixed(0) + ' 秒' : '-' }}
          </a-descriptions-item>
        </a-descriptions>
      </a-card>

</a-tab-pane>
      </a-tabs>
    </template>

    <section v-else class="choose-classroom"><span class="choose-icon"><VideoCameraOutlined /></span><h2>选择一节课堂，开始分析</h2><p>从右上方选择已有课堂，或先在“课程与课堂”中新建课堂。<br />视频与音频的分析结果会保存在对应课堂中。</p><a-button type="primary" @click="router.push('/classroom')">前往课程与课堂 <ArrowRightOutlined /></a-button></section>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { CaretRightOutlined, CloudUploadOutlined, RightOutlined, ArrowRightOutlined, SafetyCertificateOutlined, VideoCameraOutlined } from '@ant-design/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import apiClient from '@/api/client'
import VChart from 'vue-echarts/csp'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { ComposeOption } from 'echarts/core'
import type { LineSeriesOption } from 'echarts/charts'
import type { GridComponentOption, TooltipComponentOption, LegendComponentOption } from 'echarts/components'
import { colors } from '@mact-eval/design-tokens'
import { useClassroomSocket } from '@/composables/useClassroomSocket'
import { useMediaUpload } from '@/composables/useMediaUpload'
import { useClassroomStore } from '@/stores/classroom'

use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

type ECOption = ComposeOption<
  LineSeriesOption | GridComponentOption | TooltipComponentOption | LegendComponentOption
>

/** Fallback classroom size when class config / live totals are unavailable */
const DEFAULT_TOTAL_SEATS = 45

const route = useRoute()
const router = useRouter()
const analysisTab = ref('evidence')
const classroomStore = useClassroomStore()

// 请求序号：切换课堂后，旧请求晚到也不得覆盖当前课堂数据。
let loadSeq = 0
const dataError = ref('')

// Session management
const selectedSessionId = ref(route.query.sessionId as string || '')
const sessionStatus = ref('')
const sessions = ref<any[]>([])
const sessionOptions = computed(() => sessions.value.map((s: any) => ({ label: s.title, value: s.id })))
const startingSession = ref(false)

let sessionStatusTimer: ReturnType<typeof setInterval> | null = null
let demoTimer: ReturnType<typeof setInterval> | null = null

async function refreshSelectedSessionStatus(
  lessonId = selectedSessionId.value,
  seq = loadSeq,
) {
  if (!lessonId) return
  try {
    const r = await apiClient.get('/sessions/' + lessonId)
    if (seq !== loadSeq || lessonId !== selectedSessionId.value) return
    const latest = r.data.data
    if (!latest) return
    sessionStatus.value = latest.status || ''
    const idx = sessions.value.findIndex((s: any) => s.id === latest.id)
    if (idx >= 0) sessions.value[idx] = { ...sessions.value[idx], status: latest.status }
  } catch {
    // 保留当前状态，避免短暂网络错误造成按钮闪烁
  }
}

function startSessionStatusPolling() {
  if (sessionStatusTimer) clearInterval(sessionStatusTimer)
  sessionStatusTimer = setInterval(refreshSelectedSessionStatus, 5000)
}
const demoMode = computed(() => route.query.demo === '1')

// Speech metrics (from aligned window features)
const speechMetrics = ref<{ speechCoverageAvg: number; totalQuestionCount: number; totalSpeechSeconds: number } | null>(null)

// Window features (video/speech/fused score trend)
interface EvidenceFrame {
  id: string
  windowIndex: number
  timestampMs?: number
  code: string
  label: string
  url: string | null
}

const windowFeatures = ref<Array<{
  windowIndex: number
  videoScore: number | null
  speechScore: number | null
  fusedScore: number | null
  videoFeatures?: { data_quality?: string; dominant_scene?: string }
  evidenceFrames?: EvidenceFrame[]
}>>([])

const hasEvidenceFrames = computed(() =>
  windowFeatures.value.some((w) => (w.evidenceFrames?.length ?? 0) > 0),
)

const windowChartOption = computed<ECOption>(() => {
  const list = windowFeatures.value
  return {
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#fff',
      borderColor: colors.neutral.border,
      borderWidth: 1,
      textStyle: { color: colors.text.primary, fontSize: 12 },
      // Plain text formatter — no HTML tags, escapes residual '<' to avoid HTML tooltip injection
      formatter: (params: any) => {
        if (!Array.isArray(params)) return ''
        const lines = [`窗口 ${params[0].axisValue}`]
        for (const p of params) {
          const v = p.value ?? '-'
          const vs = typeof v === 'number' ? v.toFixed(1) : v
          lines.push(` ${p.seriesName}:${vs}分`)
        }
        return lines.join('\n').replace(/</g, '&lt;')
      },
    },
    legend: { bottom: 0, data: ['视频', '语音', '融合'], textStyle: { color: colors.text.secondary, fontSize: 11 } },
    grid: { left: 12, right: 24, top: 24, bottom: 36, containLabel: true },
    xAxis: {
      type: 'category',
      data: list.map((w) => String(w.windowIndex)),
      axisLine: { lineStyle: { color: colors.neutral.border } },
      axisTick: { show: false },
      axisLabel: { color: colors.text.secondary, fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      min: 0, max: 100, interval: 20,
      splitLine: { lineStyle: { color: colors.neutral.divider, type: 'dashed' } },
      axisLabel: { color: colors.text.secondary, fontSize: 10 },
    },
    series: [
      { name: '视频', type: 'line', data: list.map((w) => w.videoScore), smooth: true, symbol: 'circle', symbolSize: 4, lineStyle: { color: colors.chart.blue, width: 2 }, itemStyle: { color: colors.chart.blue } },
      { name: '语音', type: 'line', data: list.map((w) => w.speechScore), smooth: true, symbol: 'circle', symbolSize: 4, lineStyle: { color: colors.chart.purple, width: 2 }, itemStyle: { color: colors.chart.purple } },
      { name: '融合', type: 'line', data: list.map((w) => w.fusedScore), smooth: true, symbol: 'circle', symbolSize: 4, lineStyle: { color: colors.chart.cyan, width: 2 }, itemStyle: { color: colors.chart.cyan } },
    ],
  }
})

// ---- Classroom socket (WS + live lesson-room state) ----
const {
  wsConnected,
  socketState,
  socketError,
  lastSyncedAt,
  uprightRate,
  coverage,
  dataQuality,
  behaviorHistory,
  behaviorWindows,
  connect: connectWS,
  resetLessonState,
  startPolling,
} = useClassroomSocket({
  lessonId: () => selectedSessionId.value,
  pollHandler: () => {
    void fetchSpeechMetrics()
  },
})

const hasPostureHistory = computed(() => behaviorHistory.value.some(b => b.upright !== null))

const socketLabel = computed(() => {
  if (socketState.value === 'connected') return '● 实时'
  if (socketState.value === 'connecting') return '◌ 连接中'
  if (socketState.value === 'auth-required') return '连接受限'
  return '○ 离线'
})

const qualityBorderColor = computed(() => {
  if (dataQuality.value === 'usable') return '#43836b'
  if (dataQuality.value === 'limited') return '#b67a1d'
  if (dataQuality.value === 'insufficient') return '#d04b4b'
  return '#68758a'
})
const dataQualityLabel = computed(() => {
  if (dataQuality.value === 'usable') return '良好'
  if (dataQuality.value === 'limited') return '受限'
  if (dataQuality.value === 'insufficient') return '不足'
  return '待分析'
})

/** Seats for analysis come from class configuration, independently of check-in records. */
const totalSeats = computed(() => {
  const session = sessions.value.find((x: any) => x.id === selectedSessionId.value)
  if (session?.classId) {
    const cls = classroomStore.classes.find((c) => c.id === session.classId)
    const n = cls?.studentCount
    if (typeof n === 'number' && n > 0) return n
  }
  return DEFAULT_TOTAL_SEATS
})

// ---- Media upload (video/audio → async analysis) ----
const {
  mediaFile,
  mediaType,
  mediaUploading,
  mediaProgress,
  mediaJobStatus,
  mediaJobs,
  setActiveLesson,
  handleMediaBeforeUpload,
  clearMediaFile,
  formatMediaSize,
  startMediaUpload,
  cancelMediaUpload,
  retryMediaJob,
  restoreMediaJobs,
  mediaJobStatusColor,
  mediaJobStatusLabel,
} = useMediaUpload({
  lessonId: () => selectedSessionId.value,
  totalSeats: () => totalSeats.value,
  onJobFinished: (job, lessonId) => {
    // 只刷新任务所属课堂；即使教师已切到其他课堂也不会污染当前页面。
    if (lessonId !== selectedSessionId.value) return
    const seq = loadSeq
    void fetchSpeechMetrics(lessonId, seq)
  },
})

function barColor(v: number) {
  return v >= 70 ? '#22C55E' : v >= 50 ? '#F59E0B' : '#EF4444'
}

// ---- Session management ----
async function fetchSessions() {
  try {
    const r = await apiClient.get('/sessions')
    sessions.value = r.data.data || []
  } catch (err: any) {
    dataError.value = err?.response?.data?.message || '课堂列表加载失败，请稍后重试'
  }
}

async function onSessionChange() {
  const nextId = selectedSessionId.value
  if (!nextId) return
  const seq = ++loadSeq
  dataError.value = ''
  // 先清空课堂级数据，避免切换瞬间仍显示上一课堂的分析结果。
  speechMetrics.value = null
  windowFeatures.value = []
  resetLessonState()
  setActiveLesson(nextId)

  const s = sessions.value.find((x: any) => x.id === nextId)
  sessionStatus.value = s?.status || ''
  connectWS()
  startSessionStatusPolling()
  startPolling()
  await fetchSpeechMetrics(nextId, seq)
  refreshSelectedSessionStatus(nextId, seq)
}

function reloadCurrentLesson() {
  if (!selectedSessionId.value) return
  const seq = ++loadSeq
  dataError.value = ''
  void fetchSpeechMetrics(selectedSessionId.value, seq)
  refreshSelectedSessionStatus(selectedSessionId.value, seq)
}

async function startSession() {
  startingSession.value = true
  try {
    await apiClient.patch('/sessions/' + selectedSessionId.value + '/status', { status: 'IN_PROGRESS' })
    sessionStatus.value = 'IN_PROGRESS'
    const idx = sessions.value.findIndex((x: any) => x.id === selectedSessionId.value)
    if (idx >= 0) sessions.value[idx] = { ...sessions.value[idx], status: 'IN_PROGRESS' }
    message.success('课堂已开始')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    startingSession.value = false
  }
}

// ---- Speech metrics ----
async function fetchSpeechMetrics(lessonId = selectedSessionId.value, seq = loadSeq) {
  try {
    const res = await apiClient.get(`/window-features/${lessonId}`)
    if (seq !== loadSeq || lessonId !== selectedSessionId.value) return
    if (res.data?.code === 200) {
      speechMetrics.value = res.data.data?.speechMetrics ?? null
      windowFeatures.value = res.data.data?.windows ?? []
    }
  } catch (err: any) {
    if (seq !== loadSeq || lessonId !== selectedSessionId.value) return
    speechMetrics.value = null
    windowFeatures.value = []
    dataError.value = err?.response?.data?.message || '语音/窗口特征加载失败'
  }
}

// ---- Demo mode fallback when no real WebSocket data ----
function startDemoMode() {
  if (demoTimer) clearInterval(demoTimer)
  let m = 0
  demoTimer = setInterval(() => {
    m++
    uprightRate.value = 55 + Math.round(Math.sin(m / 8) * 20 + (Math.random() - 0.5) * 12)
    coverage.value = 70 + Math.round(Math.random() * 20)
    dataQuality.value = coverage.value > 60 ? 'usable' : 'limited'
    const t = new Date()
    const timeStr = t.getHours().toString().padStart(2, '0') + ':' + t.getMinutes().toString().padStart(2, '0')
    behaviorHistory.value.push({ time: timeStr, upright: uprightRate.value })
    if (behaviorHistory.value.length > 24) behaviorHistory.value.shift()
    behaviorWindows.value.push({
      time: timeStr,
      upright: uprightRate.value,
      coverage: coverage.value,
      quality: dataQuality.value,
    })
    if (behaviorWindows.value.length > 20) behaviorWindows.value.shift()
  }, 5000)
}

onMounted(async () => {
  restoreMediaJobs()
  await Promise.all([fetchSessions(), classroomStore.fetchClasses()])
  if (selectedSessionId.value) await onSessionChange()
  setActiveLesson(selectedSessionId.value)
  if (demoMode.value) startDemoMode()
})

onUnmounted(() => {
  if (sessionStatusTimer) clearInterval(sessionStatusTimer)
  if (demoTimer) clearInterval(demoTimer)
})
</script>

<style scoped>
.media-job-list { display: flex; flex-direction: column; gap: 6px; width: 100%; }
.selected-media { display: flex; align-items: center; gap: 8px; width: 100%; min-width: 0; }
.selected-media-name { flex: 1; min-width: 0; font-size: 13px; overflow-wrap: anywhere; }
.media-job-row { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.media-job-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.media-job-hint { color: #667085; }
.media-job-error { color: #EF4444; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.window-evidence-frames { display: flex; flex-wrap: wrap; gap: 10px; width: 100%; margin-top: 8px; }
.evidence-frame { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.evidence-frame-image { border-radius: 6px; overflow: hidden; border: 1px solid #E4E7EC; }
.evidence-frame-label { font-size: 12px; color: #475467; }
.evidence-frame-missing {
  width: 112px; height: 72px; display: grid; place-items: center;
  background: #F2F4F7; color: #98A2B3; font-size: 12px; border-radius: 6px;
}
.evidence-frame-note { margin-top: 8px; font-size: 12px; color: #98A2B3; }
.trend-container {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 140px;
  padding: 4px 0;
}
.trend-bar-wrapper {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  height: 100%;
}
.trend-bar-bg {
  flex: 1;
  width: 100%;
  background: #f0f0f0;
  border-radius: 4px 4px 0 0;
  position: relative;
  overflow: hidden;
}
.trend-bar {
  position: absolute;
  bottom: 0;
  width: 100%;
  border-radius: 4px 4px 0 0;
  transition: height 1.5s ease;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 2px;
}

.analysis-flow { display: flex; align-items: center; gap: 22px; padding: 14px 20px; margin-bottom: 20px; background: #fff; border: 1px solid var(--color-border); border-radius: 9px; }
.analysis-flow > span { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--color-text-secondary); }
.analysis-flow i { font-style: normal; width: 22px; height: 22px; display: grid; place-items: center; background: #eef2fb; color: var(--color-primary); border-radius: 50%; font-size: 12px; }
.analysis-flow > .anticon { font-size: 9px; color: #b7c2d3; }
.analysis-upload-grid { display: grid; grid-template-columns: minmax(0,1fr) 280px; gap: 20px; align-items: start; }
.upload-card { min-width: 0; }
.upload-card :deep(.ant-upload-drag) { border: 1px dashed #b8c8e6; background: #f9fbff; border-radius: 9px; }
.upload-card :deep(.ant-upload-btn) { padding: 32px 18px; }
.upload-card :deep(.ant-upload-text) { font-size: 14px; font-weight: 550; color: var(--color-ink); }
.upload-card :deep(.ant-upload-hint) { font-size: 12px; line-height: 1.8; padding: 0 15px; }
.upload-header-note { font-size: 12px; color: var(--color-text-secondary); }
.upload-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin-top: 10px; }
.analysis-sidebar { display: grid; gap: 16px; }
.status-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 9px 0; font-size: 12px; }
.status-item > span { color: var(--color-text-secondary); }
.status-item strong { font-weight: 550; }
.connection-live { color: #43836b; }
.status-note { border-top: 1px solid var(--color-divider); padding-top: 13px; margin: 9px 0 0; color: var(--color-text-secondary); font-size: 12px; line-height: 1.7; }
.review-guide { padding: 20px; border-radius: 9px; background: #edf3f2; border: 1px solid #dfeae7; }
.guide-icon { color: #598573; font-size: 20px; }
.review-guide h3 { font-size: 12px; font-weight: 600; margin: 10px 0 7px; color: #3e6257; }
.review-guide p { color: #6a827b; font-size: 12px; line-height: 1.9; margin-bottom: 12px; }
.review-guide a { font-size: 12px; color: #46755f; }
.model-information { font-size: 12px; margin: 10px 0 15px; color: var(--color-text-secondary); }
.model-information p { font-size: 12px; line-height: 1.8; margin: 0 0 7px; }
.analysis-tabs :deep(.ant-tabs-tab) { font-size: 13px; }
.window-quality-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 18px 0; border-top: 1px solid var(--color-divider); font-size: 12px; }
.window-quality-reason { color: var(--color-text-secondary); }
.choose-classroom { display: flex; flex-direction: column; align-items: center; text-align: center; border: 1px solid var(--color-border); border-radius: 12px; background: #fff; padding: 72px 20px; }
.choose-icon { width: 68px; height: 68px; display: grid; place-items: center; border-radius: 18px; background: #eef2fb; color: #778fb8; font-size: 28px; }
.choose-classroom h2 { font-size: 19px; font-weight: 600; margin: 22px 0 10px; }
.choose-classroom p { font-size: 12px; line-height: 1.9; color: var(--color-text-secondary); margin-bottom: 24px; }
@media (max-width: 1100px) { .analysis-upload-grid { grid-template-columns: minmax(0,1fr) 240px; gap: 16px; } }
@media (max-width: 700px) { .analysis-upload-grid { grid-template-columns: 1fr; } .analysis-sidebar { grid-template-columns: 1fr; } .analysis-flow { padding: 12px; gap: 10px; justify-content: space-between; } .analysis-flow > span { font-size: 12px; gap: 5px; } .media-job-row { flex-wrap: wrap; } .analysis-tabs :deep(.ant-tabs-tab) { font-size: 12px; } }

</style>
