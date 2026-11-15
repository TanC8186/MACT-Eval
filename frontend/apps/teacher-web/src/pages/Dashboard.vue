<template>
  <div class="dashboard-page">
    <PageHeader title="教学工作台" description="从课堂记录到教学反思，在这里开始。" eyebrow="WORKSPACE">
      <template #extra>
        <span class="dashboard-date"><CalendarOutlined /> {{ todayLabel }}</span>
      </template>
    </PageHeader>

    <section class="start-panel" aria-labelledby="start-title">
      <div class="start-copy">
        <span class="section-kicker">课堂分析</span>
        <h2 id="start-title">让课堂表现，有据可循。</h2>
        <p>上传课堂视频或音频，结合原始画面复核，整理可追溯的教学反馈。</p>
        <a-button type="primary" size="large" @click="router.push('/evaluation')">
          <PlusOutlined /> 开始课堂分析 <ArrowRightOutlined />
        </a-button>
      </div>
      <div class="workflow-guide" aria-label="课堂分析流程">
        <div><span>01</span><strong>选择课堂</strong><small>关联课程与班级</small></div>
        <i aria-hidden="true" />
        <div><span>02</span><strong>上传素材</strong><small>视频或音频文件</small></div>
        <i aria-hidden="true" />
        <div><span>03</span><strong>复核与报告</strong><small>回看证据，辅助反思</small></div>
      </div>
    </section>

    <div class="overview-heading">
      <div><h3>教学数据概览</h3><p>全部课堂的累计记录</p></div>
      <span class="source-label" v-if="pageState === 'ready'">{{ dataSourceLabel }}</span>
    </div>
    <DataState v-if="pageState === 'loading'" state="loading" variant="card" />
    <DataState v-else-if="pageState === 'error'" state="error" :error-message="errorMessage" @retry="fetchAll" retryable />
    <div v-else-if="pageState === 'empty'" class="dashboard-empty">
      <span class="empty-symbol"><FileSearchOutlined /></span>
      <div>
        <h3>{{ emptyStateText }}</h3>
        <p>先选择一节课堂上传素材。分析后的行为画面可在课堂分析中复核。</p>
      </div>
      <a-button @click="router.push('/classroom')">查看课堂 <ArrowRightOutlined /></a-button>
    </div>
    <template v-else>
      <a-alert v-if="summary?.dataSource === 'DEMO'" type="warning" show-icon message="当前为演示数据，不代表真实课堂分析结论。" class="demo-notice" />
      <section class="stats-grid" aria-label="累计课堂指标">
        <a-card class="lesson-stat" :bordered="false">
          <div class="lesson-stat-label">累计课堂记录 <BookOutlined /></div>
          <div class="lesson-stat-value numeric">{{ summary?.totalLessons ?? '—' }}<span>次</span></div>
          <div class="lesson-stat-note">已纳入汇总的课堂</div>
        </a-card>
        <MetricCard title="课堂氛围" :value="summary?.metrics?.climate?.score ?? '—'" suffix="分" subtitle="规则基线 · 尚待验证" highlight />
        <MetricCard title="学生参与度" :value="summary?.metrics?.engagement?.score ?? '—'" suffix="分" subtitle="辅助指标 · 尚待验证" />
        <MetricCard title="教学质量" :value="summary?.metrics?.teaching?.score ?? '—'" suffix="分" subtitle="辅助指标 · 尚待验证" />
      </section>
      <a-card class="suggestions-card" title="教学建议" :bordered="false">
        <template #extra><span class="suggestion-count">{{ visibleSuggestions.length }} 项待处理</span></template>
        <div v-if="visibleSuggestions.length" class="suggestion-list">
          <div v-for="s in visibleSuggestions" :key="s.id" class="suggestion-item">
            <span class="priority-dot" :class="'priority-' + s.priority" />
            <div class="suggestion-body">
              <div class="suggestion-category">{{ s.category }}</div>
              <strong>{{ s.title }}</strong>
              <p>{{ s.description }}</p>
              <div class="suggestion-actions">
                <a-button size="small" @click="handleAccept(s.id)" :loading="actionLoadingId === s.id && actionType === 'accept'">采纳建议</a-button>
                <a-button size="small" type="text" @click="handleIgnore(s.id)" :loading="actionLoadingId === s.id && actionType === 'ignore'">忽略</a-button>
              </div>
            </div>
          </div>
        </div>
        <a-alert v-if="suggestionsError" type="warning" show-icon :message="suggestionsError">
          <template #action><a-button size="small" @click="fetchSuggestions">重试</a-button></template>
        </a-alert>
        <div v-else-if="!visibleSuggestions.length" class="suggestion-empty">
          <CheckCircleOutlined />
          <h4>暂无待处理建议</h4>
          <p>完成课堂分析后，在这里查看值得关注的教学反馈。</p>
        </div>
      </a-card>
      <p class="metric-note"><InfoCircleOutlined /> 评分用于辅助教学反思，当前为待验证的规则结果，不替代人工判断。</p>
    </template>
    <section class="quick-links" aria-label="常用入口">
      <router-link to="/classroom">
        <span class="quick-icon"><BookOutlined /></span>
        <div><strong>课程与课堂</strong><span>管理课程，安排课堂</span></div><ArrowRightOutlined />
      </router-link>
      <router-link to="/reports">
        <span class="quick-icon"><FileTextOutlined /></span>
        <div><strong>评价报告</strong><span>回顾分析结果与证据</span></div><ArrowRightOutlined />
      </router-link>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  BookOutlined,
  PlusOutlined, ArrowRightOutlined, FileSearchOutlined, FileTextOutlined, InfoCircleOutlined,
  CalendarOutlined,
  CheckCircleOutlined,
} from '@ant-design/icons-vue'
import { useRouter } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'
import MetricCard from '@/components/MetricCard.vue'
import DataState from '@/components/DataState.vue'

import {
  getDashboardSummary,
  getSuggestions,
  adoptSuggestion,
  ignoreSuggestion,
  type DashboardSummaryDTO,
  type SuggestionDTO,
} from '@/api/endpoints'

const router = useRouter()
const todayLabel = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: 'long', day: 'numeric',
}).format(new Date())

// ---- 类型定义 ----

interface MetricSnapshot {
  score: number
}

interface DashboardSummary {
  totalLessons: number
  dataSource: string
  metrics: {
    engagement: MetricSnapshot
    teaching: MetricSnapshot
    climate: MetricSnapshot
  }
}

interface SuggestionItem {
  id: string
  priority: number
  title: string
  description: string
  category: string
}

// ---- Backend DTO → Frontend Type 映射函数 ----

function mapSummaryDTO(dto: DashboardSummaryDTO): DashboardSummary {
  return {
    totalLessons: dto.totalLessons,
    dataSource: dto.dataSource,
    metrics: {
      engagement: {
        score: Math.round(dto.avgEngagement),
      },
      teaching: {
        score: Math.round(dto.avgTeaching),
      },
      climate: {
        score: Math.round(dto.avgClimate),
      },
    },
  }
}

const PRIORITY_MAP: Record<string, number> = {
  HIGH: 1,
  MEDIUM: 2,
  LOW: 3,
}

function mapSuggestionDTO(s: SuggestionDTO): SuggestionItem {
  const key = String(s.priority || '').toUpperCase()
  return {
    id: s.id,
    priority: PRIORITY_MAP[key] ?? 3,
    title: s.content,
    description: s.evidence || s.content,
    category: s.category || '教学建议',
  }
}

// ---- Reactive State ----

type PageState = 'loading' | 'error' | 'empty' | 'ready'
const pageState = ref<PageState>('loading')
const errorMessage = ref('')

const summary = ref<DashboardSummary | null>(null)
const suggestions = ref<SuggestionItem[]>([])
const suggestionsError = ref('')

// Track dismissed / accepted / ignored suggestions
const dismissedIds = ref<Set<string>>(new Set())
const actionLoadingId = ref<string | null>(null)
const actionType = ref<'accept' | 'ignore' | null>(null)

// ---- Computed ----

const dataSourceLabel = computed(() => {
  if (summary.value?.dataSource === 'DEMO') return '演示数据'
  if (summary.value?.dataSource === 'REAL') return '真实评分记录'
  return '无可用评分'
})

const emptyStateText = computed(() =>
  summary.value?.totalLessons
    ? '已有课堂证据，但尚无经过验证的评价分数'
    : '暂无仪表盘数据',
)

const visibleSuggestions = computed(() =>
  suggestions.value.filter((s) => !dismissedIds.value.has(s.id)).slice(0, 3),
)

// ---- Data Fetching with Typed API Functions ----

async function fetchSummary() {
  try {
    const dto = await getDashboardSummary()
    summary.value = mapSummaryDTO(dto)
    return summary.value
  } catch (err: any) {
    throw new Error(err?.response?.data?.message || err.message || '获取概览数据失败')
  }
}

async function fetchSuggestions() {
  suggestionsError.value = ''
  try {
    const dtos = await getSuggestions()
    suggestions.value = dtos.map(mapSuggestionDTO).sort((a, b) => a.priority - b.priority)
  } catch (err: any) {
    // 加载失败与“确实没有待办”必须区分，不能把异常显示成空列表。
    suggestionsError.value = err?.response?.data?.message || '待处理建议加载失败'
  }
}

async function fetchAll() {
  pageState.value = 'loading'
  errorMessage.value = ''
  try {
    const [summaryResult] = await Promise.all([
      fetchSummary(),
      fetchSuggestions(),
    ])
    pageState.value =
      summaryResult && summaryResult.dataSource !== 'EMPTY' ? 'ready' : 'empty'
  } catch (err: any) {
    pageState.value = 'error'
    errorMessage.value = err.message || '加载仪表盘数据失败'
  }
}

// ---- Suggestion Actions ----

async function handleAccept(id: string) {
  actionLoadingId.value = id
  actionType.value = 'accept'
  try {
    await adoptSuggestion(id)
    dismissedIds.value = new Set([...dismissedIds.value, id])
  } catch {
    // Keep visible on error — user can retry
  } finally {
    actionLoadingId.value = null
    actionType.value = null
  }
}

async function handleIgnore(id: string) {
  actionLoadingId.value = id
  actionType.value = 'ignore'
  try {
    await ignoreSuggestion(id)
    dismissedIds.value = new Set([...dismissedIds.value, id])
  } catch {
    // Keep visible on error — user can retry
  } finally {
    actionLoadingId.value = null
    actionType.value = null
  }
}

onMounted(() => {
  fetchAll()
})
</script>

<style scoped>
.dashboard-date { display: inline-flex; align-items: center; gap: 7px; color: var(--color-text-secondary); font-size: 12px; }
.start-panel { display: flex; align-items: center; justify-content: space-between; gap: 30px; padding: 30px 32px; background: #edf2fb; border: 1px solid #dee6f5; border-radius: 12px; margin-bottom: 30px; }
.section-kicker { color: var(--color-primary); font-size: 12px; font-weight: 650; }
.start-copy h2 { color: #263b66; font-size: 26px; font-weight: 650; margin: 9px 0 10px; letter-spacing: -.5px; }
.start-copy p { max-width: 420px; color: #6a7a95; font-size: 12px; line-height: 1.9; margin: 0 0 21px; }
.start-copy :deep(.ant-btn) { font-size: 12px; height: 38px; padding: 0 16px; }
.start-copy :deep(.ant-btn .anticon:last-child) { margin-left: 15px; }
.workflow-guide { display: flex; align-items: flex-start; gap: 14px; flex-shrink: 0; }
.workflow-guide > div { display: flex; flex-direction: column; align-items: flex-start; }
.workflow-guide div > span { display: grid; place-items: center; width: 32px; height: 32px; background: #fff; border: 1px solid #d9e3f5; color: #526d9d; font-family: var(--font-numeric); border-radius: 50%; font-size: 12px; margin-bottom: 12px; }
.workflow-guide strong { font-size: 12px; color: #334968; font-weight: 600; }
.workflow-guide small { font-size: 12px; color: #76869e; margin-top: 5px; }
.workflow-guide i { width: 25px; height: 1px; background: #cbd6e9; margin-top: 16px; }
.overview-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.overview-heading h3 { margin: 0; font-size: 15px; font-weight: 650; }
.overview-heading p { margin: 5px 0 0; color: var(--color-text-secondary); font-size: 12px; }
.source-label { color: #587967; font-size: 12px; display: flex; align-items: center; gap: 6px; }
.source-label::before { content: ''; width: 5px; height: 5px; border-radius: 50%; background: #739a84; }
.stats-grid { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap: 16px; margin-bottom: 24px; }
.lesson-stat-label { display: flex; justify-content: space-between; color: var(--color-text-secondary); font-size: 12px; }
.lesson-stat-value { font-size: 34px; color: var(--color-ink); font-weight: 650; line-height: 1.1; margin-top: 17px; }
.lesson-stat-value span { font-size: 12px; color: var(--color-text-secondary); margin-left: 8px; font-weight: 400; }
.lesson-stat-note { font-size: 12px; color: var(--color-text-secondary); margin-top: 17px; }
.suggestion-count { font-size: 12px; color: var(--color-text-secondary); }
.suggestion-item { display: flex; align-items: flex-start; gap: 10px; padding: 14px 0; }
.suggestion-item + .suggestion-item { border-top: 1px solid var(--color-divider); }
.priority-dot { width: 6px; height: 6px; margin-top: 6px; border-radius: 50%; flex: none; background: #6c8acd; }
.priority-1 { background: #d99375; }
.priority-2 { background: #d5b06c; }
.suggestion-body { min-width: 0; }
.suggestion-category { font-size: 12px; color: var(--color-text-secondary); margin-bottom: 5px; }
.suggestion-body strong { font-size: 12px; font-weight: 550; }
.suggestion-body p { font-size: 12px; color: var(--color-text-secondary); line-height: 1.7; margin: 7px 0 10px; }
.suggestion-actions { display: flex; gap: 6px; }
.suggestion-empty { text-align: center; padding: 44px 16px; }
.suggestion-empty > .anticon { color: #8aa595; font-size: 25px; }
.suggestion-empty h4 { font-size: 13px; margin: 14px 0 7px; font-weight: 550; }
.suggestion-empty p { font-size: 12px; color: var(--color-text-secondary); max-width: 230px; margin: auto; line-height: 1.8; }
.metric-note { display: flex; gap: 6px; align-items: center; margin: 14px 0 26px; font-size: 12px; color: var(--color-text-secondary); }
.quick-links { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 16px; margin-top: 25px; }
.quick-links > a { display: flex; align-items: center; gap: 13px; padding: 18px; border: 1px solid var(--color-border); border-radius: 9px; background: #fff; color: var(--color-ink); transition: border-color .15s; }
.quick-links > a:hover { border-color: #a7b8e5; }
.quick-icon { width: 34px; height: 36px; background: #f3f6fb; color: #637ea9; border-radius: 7px; display: grid; place-items: center; font-size: 17px; flex: none; }
.quick-links div { flex: 1; min-width: 0; }
.quick-links strong, .quick-links div > span { display: block; }
.quick-links strong { font-size: 12px; font-weight: 550; }
.quick-links div > span { font-size: 12px; color: var(--color-text-secondary); margin-top: 4px; }
.quick-links > a > .anticon { color: var(--color-text-tertiary); font-size: 12px; }
.dashboard-empty { display: flex; align-items: center; gap: 18px; background: #fff; padding: 38px 30px; border: 1px solid var(--color-border); border-radius: 10px; }
.empty-symbol { width: 54px; height: 54px; background: #f1f5fb; color: #8196b7; display: grid; place-items: center; border-radius: 12px; font-size: 24px; flex: none; }
.dashboard-empty > div { flex: 1; }
.dashboard-empty h3 { font-size: 14px; margin: 0 0 8px; }
.dashboard-empty p { font-size: 12px; line-height: 1.8; color: var(--color-text-secondary); margin: 0; }
.demo-notice { margin-bottom: 16px; }
@media (min-width: 1600px) { .start-panel { padding: 36px 40px; } }
@media (max-width: 1200px) { .workflow-guide { gap: 10px; } .workflow-guide i { width: 12px; } .workflow-guide small { display: none; } }
@media (max-width: 1050px) { .start-panel { padding: 25px; } .start-copy h2 { font-size: 23px; } .stats-grid { grid-template-columns: repeat(2,minmax(0,1fr)); } }
@media (max-width: 650px) { .start-panel { align-items: flex-start; flex-direction: column; gap: 26px; padding: 24px; } .workflow-guide { width: 100%; justify-content: space-between; } .workflow-guide small { display: block; } .quick-links { grid-template-columns: 1fr; } .stats-grid { gap: 10px; } .dashboard-empty { flex-wrap: wrap; padding: 24px; } .dashboard-empty > div { min-width: 180px; } .dashboard-empty > .ant-btn { margin-left: 72px; } .metric-note { align-items: flex-start; line-height: 1.8; } }
</style>
