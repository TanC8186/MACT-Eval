<template>
  <section ref="rootEl" class="behavior-evidence" aria-label="课堂行为识别证据" data-testid="behavior-evidence-gallery">
    <div class="evidence-toolbar">
      <div>
        <strong>行为识别画面</strong>
        <p>对照检测框与原始画面，复核模型识别到的课堂动作。</p>
      </div>
      <a-button size="small" data-testid="evidence-refresh" :loading="loading" @click="refresh">刷新画面</a-button>
    </div>
    <a-alert v-if="error" type="error" show-icon :message="error" data-testid="evidence-error" style="margin-bottom:12px" />
    <a-spin :spinning="loading">
      <template v-if="items.length">
        <div class="evidence-filters" aria-label="按动作筛选">
          <button type="button" data-testid="evidence-filter" data-code=""
            :class="{ active: !filterCode }" @click="selectFilter('')">全部 {{ items.length }}</button>
          <button v-for="option in filters" :key="option.code" type="button" data-testid="evidence-filter"
            :data-code="option.code" :class="{ active: filterCode === option.code }" @click="selectFilter(option.code)">
            {{ option.label }} {{ option.count }}
          </button>
        </div>
        <nav v-if="filteredFrames.length" class="evidence-pager" aria-label="证据画面分页" tabindex="-1" data-testid="evidence-pager">
          <button type="button" class="pager-button" data-testid="evidence-prev"
            aria-label="上一页证据画面" :disabled="pageInfo.page <= 1" @click="goToPage(pageInfo.page - 1)">上一页</button>
          <span class="pager-status" data-testid="evidence-page-status" aria-live="polite">
            第 {{ pageInfo.page }} / {{ pageInfo.pageCount }} 页，共 {{ filteredFrames.length }} 张
          </span>
          <button type="button" class="pager-button" data-testid="evidence-next"
            aria-label="下一页证据画面" :disabled="pageInfo.page >= pageInfo.pageCount" @click="goToPage(pageInfo.page + 1)">下一页</button>
        </nav>
        <div class="evidence-grid">
          <article v-for="frame in visibleFrames" :key="frame.id" class="evidence-card" data-testid="evidence-card"
            :data-frame-id="frame.id" :data-window-index="frame.windowIndex" :data-code="frame.code">
            <button class="evidence-media" data-testid="evidence-card-media"
              :data-frame-id="frame.id" :data-window-index="frame.windowIndex"
              :aria-label="`查看 ${formatDuration(frame.timestampMs)} ${frame.label} 检测画面`"
              :disabled="isUnavailable(frame)" @click="openFrame(frame, $event)">
              <img v-if="frame.url && !brokenImages[frame.id]" :src="frame.url" loading="lazy"
                data-testid="evidence-card-image"
                :alt="`${formatDuration(frame.timestampMs)}的${frame.label}证据帧`"
                @load="markDecoded(frame, $event)" @error="markBroken(frame)" />
              <span v-else class="image-missing">画面已过期或不可用，请刷新</span>
              <!-- 检测框只在图片真正解码成功后叠加：加载中会盖在空白上，失效占位（黑底）后继续画框会把不可复核的画面伪装成有效识别结果。 -->
              <template v-if="cardBoxesVisible(frame)">
                <span v-for="detection in highlighted(frame)" :key="detection.subjectId" class="detection-box"
                  data-testid="evidence-detection-box" :style="boxStyle(detection)">
                  <span class="box-caption">{{ targetName(detection.subjectId) }} · {{ detection.label }} {{ percent(detection.confidence) }}</span>
                </span>
              </template>
              <span class="frame-time" data-testid="evidence-frame-time">{{ formatDuration(frame.timestampMs) }}</span>
            </button>
            <div class="evidence-caption">
              <div class="evidence-title"><strong>{{ frame.label }}</strong><a-tag :color="sourceColor(frame)">{{ sourceLabel(frame) }}</a-tag></div>
              <p v-if="primary(frame)">
                {{ targetName(primary(frame)!.subjectId) }} · 模型置信度 {{ percent(primary(frame)!.confidence) }}<span
                  v-if="frame.code === 'overview'">（同帧行为模型框，全景视图）</span>
              </p>
              <p v-else>{{ frame.code === 'overview' ? '课堂全景，供对照复核' : '未提供目标级检测框，不能据此确认个人动作' }}</p>
              <div class="evidence-actions">
                <button class="inspect-button" :disabled="isUnavailable(frame)" @click="openFrame(frame, $event)">查看检测详情 →</button>
                <button v-if="isUnavailable(frame)" type="button" class="retry-button" data-testid="evidence-retry"
                  :aria-label="`重试加载 ${formatDuration(frame.timestampMs)} 的检测画面`"
                  :aria-disabled="retryBusy && retryingId !== frame.id"
                  :title="retryBusy && retryingId !== frame.id ? '另一张画面正在重试，请稍后' : ''"
                  :disabled="retryBusy" @click="retryFrame(frame.id)">
                  {{ retryingId === frame.id ? '重试中…' : '重试' }}
                </button>
              </div>
            </div>
          </article>
        </div>
        <p class="evidence-note">这里展示经过筛选的代表帧，截图数量不代表动作发生次数。目标编号只在当前画面内有效，不对应姓名或学号，也不表示跨帧身份追踪。置信度是模型预测分数，需结合画面人工复核。缺少精确时间的记录显示为「时间未知」，不会被当作课堂零点。采样间隔随课堂时长约 2–10 秒，持续很短的动作可能没有被截取。</p>
      </template>
      <div v-else-if="!loading && !error" class="evidence-empty">
        <strong>本报告暂无行为识别画面</strong>
        <p>使用已配置的课堂行为模型重新分析视频，再生成报告。历史任务不会自动补造截图或检测结果。</p>
      </div>
    </a-spin>

    <a-modal v-model:open="previewOpen" :title="activeFrame ? `${formatDuration(activeFrame.timestampMs)} · ${activeFrame.label}` : '识别画面'"
      width="min(1080px, 96vw)" :style="{ top: '24px' }" :after-close="onPreviewClosed"
      :body-style="{ maxHeight: 'calc(100vh - 130px)', overflowY: 'auto' }" :footer="null" :destroy-on-close="true">
      <div v-if="activeFrame" data-testid="evidence-preview">
        <div class="preview-toolbar" role="toolbar" aria-label="证据画面缩放与切换">
          <button type="button" class="zoom-button" data-testid="evidence-preview-nav-prev"
            aria-label="上一张证据画面" :disabled="!canStepPrev" @click="stepPreview(-1)">← 上一张</button>
          <button type="button" class="zoom-button" data-testid="evidence-preview-nav-next"
            aria-label="下一张证据画面" :disabled="!canStepNext" @click="stepPreview(1)">下一张 →</button>
          <span class="zoom-level" data-testid="evidence-preview-zoom-level" aria-live="polite">缩放 {{ zoomPercent }}</span>
          <button type="button" class="zoom-button" data-testid="evidence-preview-zoom-out"
            aria-label="缩小证据画面" :disabled="previewZoom <= ZOOM_MIN" @click="changeZoom(-ZOOM_STEP)">缩小</button>
          <button type="button" class="zoom-button" data-testid="evidence-preview-zoom-in"
            aria-label="放大证据画面" :disabled="previewZoom >= ZOOM_MAX" @click="changeZoom(ZOOM_STEP)">放大</button>
          <button type="button" class="zoom-button" data-testid="evidence-preview-zoom-reset"
            aria-label="恢复为原始大小" @click="resetZoom">重置</button>
          <button type="button" class="zoom-button preview-close" data-testid="evidence-preview-close"
            aria-label="关闭识别画面预览" @click="closePreview">关闭</button>
        </div>
        <!-- 缩放只改这个滚动容器内画面的宽度：页面本身不得出现横向滚动条。 -->
        <div class="preview-scroll" :style="{ overflowX: 'auto', overflowY: 'hidden', maxWidth: '100%' }">
          <div class="evidence-media preview-media" :style="previewWidth">
            <img v-if="activeFrame.url && !previewBroken" :src="activeFrame.url" alt="包含模型检测框的课堂证据画面"
              data-testid="evidence-preview-image" @load="markPreviewDecoded" @error="previewBroken = true" />
            <span v-if="!activeFrame.url || previewBroken" class="image-missing">画面不可用</span>
            <template v-if="previewBoxesVisible">
              <span v-for="detection in previewDetections" :key="detection.subjectId" class="detection-box"
                data-testid="evidence-detection-box"
                :class="{ selected: detection.subjectId === selectedSubject }" :style="boxStyle(detection)">
                <span class="box-caption">{{ targetName(detection.subjectId) }} · {{ detection.label }} {{ percent(detection.confidence) }}</span>
              </span>
            </template>
          </div>
        </div>
        <div v-if="previewUnavailable" class="preview-error">
          <span>画面已过期或不可用，请刷新</span>
          <button type="button" class="retry-button" data-testid="evidence-preview-retry"
            :aria-disabled="retryBusy && retryingId !== activeFrame.id"
            :disabled="retryBusy" @click="retryFrame(activeFrame.id)">
            {{ retryingId === activeFrame.id ? '重试中…' : '重试' }}
          </button>
        </div>
        <div class="preview-summary"><a-tag :color="sourceColor(activeFrame)">{{ sourceLabel(activeFrame) }}</a-tag>
          <span data-testid="evidence-preview-time" aria-live="polite">视频时间 {{ formatDuration(activeFrame.timestampMs) }}<template
            v-if="windowLabel(activeFrame)"> · {{ windowLabel(activeFrame) }}</template><template
            v-if="samplingLabel(activeFrame)"> · {{ samplingLabel(activeFrame) }}</template></span>
        </div>
        <p v-if="selectedDetection">模型在此画面中将 <strong>{{ targetName(selectedDetection.subjectId) }}</strong>
          预测为 <strong>{{ selectedDetection.label }}</strong>，置信度 {{ percent(selectedDetection.confidence) }}。</p>
        <div v-if="safeDetections(activeFrame).length" class="target-list" aria-label="选择画面中的检测目标">
          <button v-for="detection in safeDetections(activeFrame)" :key="detection.subjectId" type="button"
            data-testid="evidence-target" :data-subject-id="detection.subjectId"
            :class="{ active: selectedSubject === detection.subjectId }" @click="selectedSubject = detection.subjectId">
            {{ targetName(detection.subjectId) }} · {{ detection.label }} {{ percent(detection.confidence) }}
          </button>
        </div>
        <p v-else class="evidence-note">此记录只包含抽帧或规则线索，没有可核验的行为模型检测框。</p>
        <details v-if="activeFrame.metadata?.modelVersion" class="model-details">
          <summary>模型与证据来源</summary>
          <p>模型：{{ activeFrame.metadata.modelVersion }}</p>
          <p v-if="activeFrame.metadata.modelDigest">权重 SHA-256：{{ activeFrame.metadata.modelDigest }}</p>
          <p>动作类别：{{ activeFrame.metadata.supportedActions?.join('、') || '--' }}</p>
          <p v-if="activeFrame.metadata.identityScope">目标身份范围：{{ identityScopeLabel(activeFrame.metadata.identityScope) }}</p>
        </details>
        <!--
          原视频回放：后端目前没有受控的媒体播放地址（无流式/预签名视频接口），因此这里只放一个
          明确禁用的入口并说明原因，而不是伪造一个点了没反应、或直接暴露原始对象链接的按钮。
          等后端提供受控播放地址后再接上 openFrame 同类逻辑。
        -->
        <div class="preview-actions">
          <button type="button" class="zoom-button" data-testid="evidence-jump-to-video" disabled
            aria-describedby="evidence-jump-to-video-hint"
            title="原视频回放尚未接入：后端未提供受控媒体播放地址">定位到原视频</button>
          <span id="evidence-jump-to-video-hint" class="evidence-note">原视频回放尚未接入（后端未提供受控媒体播放地址），请以上方证据画面为准。</span>
        </div>
        <p class="evidence-note">识别结果用于教师复核；低头、转头等动作本身不等于不专注或违纪。课堂画面通过短期授权链接访问。</p>
      </div>
    </a-modal>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import apiClient from '@/api/client'
import type { ActionDetection, EvidenceFrame } from '@/types/evidence'
import { formatDuration, paginate } from '@/utils/duration'
import { ZOOM_MAX, ZOOM_MIN, ZOOM_STEP, clampZoom, mediaBoxVisible, mediaStateAfterRetry } from '@/utils/evidenceMedia'
import type { FrameMediaState } from '@/utils/evidenceMedia'

const PAGE_SIZE = 24

const props = defineProps<{ lessonId: string; version?: number; frames?: EvidenceFrame[] }>()
const emit = defineEmits<{ refresh: [] }>()
const rootEl = ref<HTMLElement | null>(null)
const items = ref<EvidenceFrame[]>([])
const loading = ref(false)
const error = ref('')
const filterCode = ref('')
const page = ref(1)
const brokenImages = ref<Record<string, boolean>>({})
/**
 * 已成功解码的帧：frameId -> 解码成功时的 URL。检测框只在解码后叠加；
 * 换到新签名 URL 时必须重新解码，否则又会把框画在尚未显示的画面上。
 */
const decodedImages = ref<Record<string, string>>({})
const previewOpen = ref(false)
const activeFrameId = ref('')
const selectedSubject = ref('')
const previewBroken = ref(false)
/** 预览图解码成功时的 URL；与当前 URL 不一致即视为尚未解码。 */
const previewDecodedSrc = ref('')
const retryingId = ref('')
/** 预览缩放倍数：1 = 适配容器宽度，最大 ZOOM_MAX（400%）。只用按钮和方向键改变，不做拖拽平移。 */
const previewZoom = ref(ZOOM_MIN)
let triggerEl: HTMLElement | null = null
let requestSeq = 0
let abort: AbortController | null = null
let retrySeq = 0
let retryAbort: AbortController | null = null

const filters = computed(() => {
  const groups = new Map<string, { code: string; label: string; count: number }>()
  for (const frame of items.value) {
    const group = groups.get(frame.code) || { code: frame.code, label: frame.label, count: 0 }
    group.count++
    groups.set(frame.code, group)
  }
  return [...groups.values()]
})
/** 全量数据按动作筛选；分页只影响渲染，不影响总数与计数。 */
const filteredFrames = computed(() => items.value.filter(f => !filterCode.value || f.code === filterCode.value))
const pageInfo = computed(() => paginate(filteredFrames.value.length, page.value, PAGE_SIZE))
const visibleFrames = computed(() => filteredFrames.value.slice(pageInfo.value.start, pageInfo.value.end))
const activeFrame = computed(() => items.value.find(f => f.id === activeFrameId.value) || null)
const previewUnavailable = computed(() => !activeFrame.value?.url || previewBroken.value)
/** 预览内的检测框同样要等解码成功：加载中提前画框会盖在空白上。 */
const previewBoxesVisible = computed(() => mediaBoxVisible({
  url: activeFrame.value?.url ?? null,
  broken: previewBroken.value,
  decodedSrc: !!activeFrame.value?.url && previewDecodedSrc.value === activeFrame.value?.url
    ? activeFrame.value.url : null,
}))
/** 任一帧重试进行中：其它重试按钮统一禁用并给出说明，而不是静默忽略点击。 */
const retryBusy = computed(() => !!retryingId.value)

// 数据变少时 page 已在 afterDataChanged 中收敛；这里兜底防止外部直接改页导致越界。
watch(pageInfo, info => { if (info.page !== page.value) page.value = info.page })
watch(activeFrame, frame => { if (previewOpen.value && !frame) previewOpen.value = false })

function resetInteractionState() {
  filterCode.value = ''
  page.value = 1
  brokenImages.value = {}
  decodedImages.value = {}
  previewOpen.value = false
  activeFrameId.value = ''
  selectedSubject.value = ''
  previewBroken.value = false
  previewDecodedSrc.value = ''
  previewZoom.value = ZOOM_MIN
  triggerEl = null
  retrySeq++
  retryAbort?.abort()
  retryAbort = null
  retryingId.value = ''
}

/**
 * 刷新数据。
 * resetState=true：切换报告/版本，清空筛选、页码与预览；
 * resetState=false：仅刷新签名 URL，保留筛选、页码、预览与选中目标（帧仍存在时）。
 */
async function reload(resetState: boolean) {
  if (resetState) {
    resetInteractionState()
    // 切换报告/版本时先清空，避免新版本标题下短暂渲染上一版本的可访问链接。
    items.value = []
  } else {
    // 整份刷新会带回新签名，进行中的单帧重试响应已无意义。
    retrySeq++
    retryAbort?.abort()
    retryAbort = null
    retryingId.value = ''
  }
  error.value = ''
  if (props.frames !== undefined) {
    requestSeq++
    abort?.abort()
    abort = null
    items.value = props.frames
    loading.value = false
    afterDataChanged()
    return
  }
  if (!props.lessonId) {
    requestSeq++
    abort?.abort()
    abort = null
    items.value = []
    loading.value = false
    return
  }
  const seq = ++requestSeq
  abort?.abort()
  abort = new AbortController()
  loading.value = true
  try {
    const response = await apiClient.get(`/reports/${encodeURIComponent(props.lessonId)}/behavior-evidence`, {
      params: { version: props.version }, signal: abort.signal,
    })
    if (seq !== requestSeq) return
    items.value = response.data.data || []
    afterDataChanged()
  } catch (err: any) {
    if (seq === requestSeq && err?.code !== 'ERR_CANCELED') {
      error.value = err?.response?.data?.message || '识别画面加载失败，请重试'
    }
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

/** 数据替换后的状态收敛：清理失效标记、越界页码、已消失的预览帧与目标。 */
function afterDataChanged() {
  for (const id of Object.keys(brokenImages.value)) {
    const frame = items.value.find(f => f.id === id)
    if (!frame || frame.url) delete brokenImages.value[id]
  }
  // 解码结果按 URL 记账：帧消失或换了新签名 URL 后，旧 URL 的解码结果不再成立。
  for (const id of Object.keys(decodedImages.value)) {
    const frame = items.value.find(f => f.id === id)
    if (!frame || frame.url !== decodedImages.value[id]) delete decodedImages.value[id]
  }
  if (filterCode.value && !items.value.some(f => f.code === filterCode.value)) filterCode.value = ''
  page.value = pageInfo.value.page
  const frame = activeFrame.value
  if (!frame) {
    previewOpen.value = false
    activeFrameId.value = ''
    selectedSubject.value = ''
    previewBroken.value = false
    previewDecodedSrc.value = ''
    return
  }
  const detections = safeDetections(frame)
  if (selectedSubject.value && !detections.some(d => d.subjectId === selectedSubject.value)) {
    selectedSubject.value = primary(frame)?.subjectId || ''
  }
  // 刷新拿到的可能是新签名 URL，清掉旧的加载失败状态；若对象确实被删除，img 会再次报错且不会自动重试。
  previewBroken.value = !frame.url
  if (previewDecodedSrc.value !== frame.url) previewDecodedSrc.value = ''
}

function refresh() {
  if (props.frames !== undefined) { emit('refresh'); return }
  void reload(false)
}

watch(() => [props.lessonId, props.version], () => { void reload(true) }, { immediate: true })
watch(() => props.frames, () => { if (props.frames !== undefined) void reload(false) })
onBeforeUnmount(() => {
  requestSeq++
  abort?.abort()
  retrySeq++
  retryAbort?.abort()
  window.removeEventListener('keydown', onPreviewKeydown)
})

function selectFilter(code: string) {
  filterCode.value = code
  page.value = 1
}
function goToPage(value: number) {
  page.value = Math.min(Math.max(1, value), pageInfo.value.pageCount)
}
function isUnavailable(frame: EvidenceFrame) { return !frame.url || !!brokenImages.value[frame.id] }
function markBroken(frame: EvidenceFrame) {
  brokenImages.value[frame.id] = true
  delete decodedImages.value[frame.id]
}
/**
 * img 的 load 事件：只有真正完成解码（naturalWidth > 0）才记为可用，
 * 并记下当时的 URL——换到新签名 URL 后必须重新解码，否则框会画在新画面上。
 */
function markDecoded(frame: EvidenceFrame, event?: Event) {
  const img = event?.target as HTMLImageElement | null
  const width = img?.naturalWidth ?? 0
  if (!frame.url || !width) {
    markBroken(frame)
    return
  }
  decodedImages.value[frame.id] = frame.url
}
function markPreviewDecoded(event?: Event) {
  const img = event?.target as HTMLImageElement | null
  const url = activeFrame.value?.url
  if (!url || !(img?.naturalWidth)) { previewBroken.value = true; return }
  previewDecodedSrc.value = url
}
/**
 * 卡片检测框的可见性：URL 存在、未失效、且当前 URL 已解码成功。
 * 失败占位（黑底）或加载中都不渲染，避免把不可复核的画面伪装成有效识别结果。
 */
function cardBoxesVisible(frame: EvidenceFrame) {
  return mediaBoxVisible(frameMedia(frame))
}
/** 卡片当前的画面状态：URL、是否失效、以及「解码成功时的 URL」。 */
function frameMedia(frame: EvidenceFrame): FrameMediaState {
  return {
    url: frame.url ?? null,
    broken: !!brokenImages.value[frame.id],
    decodedSrc: decodedImages.value[frame.id] ?? null,
  }
}

/**
 * 只对单帧重新取签名 URL：后端只有整份清单接口，因此发一次请求，
 * 但只替换该帧的 URL，不动其它帧、不自动重试，且期间若切换报告/版本则丢弃响应。
 */
async function retryFrame(frameId: string) {
  // 按钮在重试期间统一 disabled；这里的守卫只防程序化并发调用。
  if (retryingId.value) return
  if (props.frames !== undefined) { emit('refresh'); return }
  if (!props.lessonId) return
  const lessonId = props.lessonId
  const version = props.version
  retryingId.value = frameId
  retrySeq++
  retryAbort?.abort()
  retryAbort = new AbortController()
  const seq = retrySeq
  try {
    const response = await apiClient.get(`/reports/${encodeURIComponent(lessonId)}/behavior-evidence`, {
      params: { version }, signal: retryAbort.signal,
    })
    if (seq !== retrySeq || props.lessonId !== lessonId || props.version !== version) return
    const frames: EvidenceFrame[] = response.data.data || []
    const fresh = frames.find(f => f.id === frameId)
    if (!fresh) {
      items.value = items.value.filter(f => f.id !== frameId)
      delete brokenImages.value[frameId]
      if (activeFrameId.value === frameId) previewOpen.value = false
      afterDataChanged()
      return
    }
    const index = items.value.findIndex(f => f.id === frameId)
    if (index >= 0) items.value[index] = fresh
    // 重试成功后清掉失效标记，但**不**预置解码成功：新签名 URL 要重新解码后才画框，
    // 否则框会先盖在尚未显示的画面上（R08 要求的语义）。
    const previous = frameMedia(fresh)
    const next = mediaStateAfterRetry(fresh.url, previous.decodedSrc)
    if (next.broken) {
      brokenImages.value[frameId] = true
      delete decodedImages.value[frameId]
      if (activeFrameId.value === frameId) previewBroken.value = true
    } else {
      delete brokenImages.value[frameId]
      if (next.decodedSrc) decodedImages.value[frameId] = next.decodedSrc
      else delete decodedImages.value[frameId]
      if (activeFrameId.value === frameId) {
        previewBroken.value = false
        if (previewDecodedSrc.value !== next.url) previewDecodedSrc.value = ''
      }
      // 成功拿回签名后，上一次重试留下的全局错误已不成立，避免横幅永久停留。
      error.value = ''
    }
  } catch (err: any) {
    if (seq === retrySeq && err?.code !== 'ERR_CANCELED') {
      error.value = err?.response?.data?.message || '重新获取画面失败，请稍后再试'
    }
  } finally {
    if (seq === retrySeq) retryingId.value = ''
  }
}

function safeDetections(frame: EvidenceFrame): ActionDetection[] {
  const detections = frame.metadata?.detections
  return (Array.isArray(detections) ? detections : []).filter(d => d && typeof d.subjectId === 'string'
    && typeof d.label === 'string' && Array.isArray(d.bbox) && d.bbox.length === 4
    && d.bbox.every(n => Number.isFinite(n) && n >= 0 && n <= 1)
    && d.bbox[2] > 0 && d.bbox[3] > 0 && d.bbox[0] + d.bbox[2] <= 1.001 && d.bbox[1] + d.bbox[3] <= 1.001
    && Number.isFinite(d.confidence) && d.confidence >= 0 && d.confidence <= 1)
}
/**
 * 卡片/预览的主检测框：优先后端标注的 primarySubjectId，其次与帧动作 code 一致者，
 * 最后退回置信度最高者（并列时取先出现的一个），避免「第一个框」这种与置信度无关的兜底。
 */
function primary(frame: EvidenceFrame) {
  const detections = safeDetections(frame)
  return detections.find(d => d.subjectId === frame.metadata?.primarySubjectId)
    || detections.find(d => d.code === frame.code)
    || detections.reduce<ActionDetection | undefined>(
      (best, d) => (!best || d.confidence > best.confidence ? d : best), undefined)
}
function highlighted(frame: EvidenceFrame) { const detection = primary(frame); return detection ? [detection] : [] }
const previewDetections = computed(() => activeFrame.value
  ? safeDetections(activeFrame.value).filter(d => !selectedSubject.value || d.subjectId === selectedSubject.value) : [])
const selectedDetection = computed(() => previewDetections.value[0])
/**
 * 预览画面宽度。1 倍时保持原行为：撑满容器、以「按宽高比换算的 54vh」为上限；
 * 放大后在基准宽度上乘以倍数，由 .preview-scroll 自己在内部横向滚动，页面不出现横向滚动条。
 */
const previewWidth = computed(() => {
  const metadata = activeFrame.value?.metadata
  const ratio = Math.max(0.25, Math.min(4, (metadata?.imageWidth || 16) / (metadata?.imageHeight || 9)))
  const fit = `54vh * ${ratio}`
  const zoom = clampZoom(previewZoom.value)
  if (zoom <= ZOOM_MIN) return { width: '100%', maxWidth: `calc(${fit})` }
  return { width: `calc(min(100%, ${fit}) * ${zoom})`, maxWidth: 'none' }
})
/** 当前放大倍数文案，例如 100% / 175%。 */
const zoomPercent = computed(() => `${Math.round(clampZoom(previewZoom.value) * 100)}%`)
/** 当前预览帧在筛选结果中的序号；-1 表示该帧已不在当前筛选内（例如刷新后换码）。 */
const previewIndex = computed(() => filteredFrames.value.findIndex(f => f.id === activeFrameId.value))
/** 方向键/按钮切换的边界：到头即禁用，不回环，避免误以为画面在循环播放。 */
const canStepPrev = computed(() => previewOpen.value && previewIndex.value > 0)
const canStepNext = computed(() => previewIndex.value >= 0 && previewIndex.value < filteredFrames.value.length - 1)
/** 窗口序号缺值时不要渲染「第 NaN 个分析窗口」。 */
function windowLabel(frame: EvidenceFrame) {
  return Number.isFinite(frame.windowIndex) && frame.windowIndex >= 0
    ? `第 ${frame.windowIndex + 1} 个分析窗口` : ''
}
/** 后端目前固定下发 frame_local；出现其它取值时不暗示「画面内同一目标」成立。 */
function identityScopeLabel(scope: string) {
  return scope === 'frame_local' ? '仅当前画面内有效（frame_local）' : `${scope}（跨画面身份范围待确认）`
}
function openFrame(frame: EvidenceFrame, event?: MouseEvent) {
  // 方向键切帧没有触发元素：保留原触发卡片，关闭后焦点仍回到用户出发的位置（已被移除则由 onPreviewClosed 兜底）。
  if (event) triggerEl = (event.currentTarget as HTMLElement | null) || null
  // 换帧即回到适配大小，否则上一帧的放大倍数会带到下一帧，看起来像画面突然「跑偏」。
  if (frame.id !== activeFrameId.value) previewZoom.value = ZOOM_MIN
  activeFrameId.value = frame.id
  selectedSubject.value = primary(frame)?.subjectId || ''
  previewBroken.value = !frame.url
  // 卡片已解码同一 URL 时可直接沿用；否则等预览图自己 load 完再画框。
  previewDecodedSrc.value = frame.url && decodedImages.value[frame.id] === frame.url ? frame.url : ''
  previewOpen.value = true
}
/**
 * 预览内前后切换：在**当前筛选结果**的顺序里移动（跨页也可），到边界就停住不回环。
 * 走同一个 openFrame 路径，因此选中目标、解码状态与焦点返回逻辑都保持一致。
 */
function stepPreview(offset: number) {
  const index = previewIndex.value
  if (index < 0) return
  const target = filteredFrames.value[index + offset]
  if (!target) return
  // 目标可能在别的分页上：先把所在页翻过去，关闭预览后焦点回退才能落到它附近的分页容器。
  const targetPage = Math.floor((index + offset) / PAGE_SIZE) + 1
  if (targetPage !== page.value) page.value = targetPage
  openFrame(target)
}
/** 缩放按钮：以 ZOOM_STEP 为步长增减，clampZoom 保证不越界也不产生浮点残差。 */
function changeZoom(delta: number) { previewZoom.value = clampZoom(previewZoom.value + delta) }
function resetZoom() { previewZoom.value = ZOOM_MIN }
/**
 * 显式关闭按钮走与 antd 内部关闭完全相同的路径：置 previewOpen=false，
 * 由 :after-close 触发 onPreviewClosed，焦点返回触发它的卡片。
 */
function closePreview() { previewOpen.value = false }
/** 预览内的方向键：←/→ 切帧；其余按键一律不拦截，保持 Esc/Tab 等默认行为。 */
function onPreviewKeydown(event: KeyboardEvent) {
  if (!previewOpen.value) return
  const target = event.target as HTMLElement | null
  // 输入类控件内的方向键属于控件自己，不切帧。
  if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return
  if (event.altKey || event.ctrlKey || event.metaKey) return
  if (event.key === 'ArrowLeft') {
    if (!canStepPrev.value) return
    event.preventDefault()
    stepPreview(-1)
  } else if (event.key === 'ArrowRight') {
    if (!canStepNext.value) return
    event.preventDefault()
    stepPreview(1)
  }
}
window.addEventListener('keydown', onPreviewKeydown)
/**
 * Modal 关闭（含 Esc / 遮罩 / 关闭按钮）后把焦点还给触发卡片。
 * 触发卡片可能已被刷新移除（服务端删除该帧），此时退到当前页第一张可用卡片，
 * 再退到分页容器，避免焦点掉到 <body> 让键盘用户丢失位置。
 */
function onPreviewClosed() {
  const element = triggerEl
  triggerEl = null
  activeFrameId.value = ''
  selectedSubject.value = ''
  previewBroken.value = false
  previewDecodedSrc.value = ''
  // 关闭即回到适配大小：下次打开的是另一帧，带着上一次的放大倍数会让人误以为画面没切。
  previewZoom.value = ZOOM_MIN
  if (element?.isConnected) {
    element.focus({ preventScroll: true })
    return
  }
  const root = rootEl.value
  if (!root) return
  const fallback = root.querySelector<HTMLElement>('[data-testid="evidence-card-media"]:not([disabled])')
    || root.querySelector<HTMLElement>('[data-testid="evidence-card-media"]')
    || root.querySelector<HTMLElement>('[data-testid="evidence-pager"]')
  fallback?.focus({ preventScroll: true })
}
function boxStyle(d: ActionDetection) {
  return { left: `${d.bbox[0] * 100}%`, top: `${d.bbox[1] * 100}%`, width: `${d.bbox[2] * 100}%`, height: `${d.bbox[3] * 100}%` }
}
/**
 * 本次任务的实际采样间隔（后端在证据帧 metadata 中下发）。
 *
 * 只在拿到有效值时才显示：请求值是 2 秒，但长视频会按窗口覆盖率放大到 4–10 秒，
 * 写死一个数字会误导复核者对「可能漏掉短动作」的判断。
 */
function samplingLabel(frame: EvidenceFrame | null) {
  const value = frame?.metadata?.effectiveSampleIntervalSec
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? `本次采样约每 ${value} 秒一帧`
    : ''
}

function targetName(value: string) { return `目标 ${value.replace(/^target-/, '')}` }
function percent(value: number) { return `${(value * 100).toFixed(1)}%` }
function sourceLabel(frame: EvidenceFrame) {
  const source = frame.metadata?.detectionSource
  return source ? ({ behavior_model: '行为模型', position_proxy: '规则线索', overview: '全景' })[source] || '历史抽帧' : '历史抽帧'
}
function sourceColor(frame: EvidenceFrame) { return frame.metadata?.detectionSource === 'behavior_model' ? 'blue' : 'default' }
</script>

<style scoped>
.evidence-toolbar { display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:14px; }
.evidence-toolbar p { font-size:12px;color:#667085;margin:5px 0 0; }
.evidence-filters,.target-list { display:flex;flex-wrap:wrap;gap:7px;margin:12px 0 18px; }
.evidence-filters button,.target-list button { border:1px solid #D0D5DD;background:#fff;border-radius:7px;padding:5px 10px;color:#475467;font-size:12px;cursor:pointer; }
.evidence-filters button.active,.target-list button.active { color:#1849A9;border-color:#84ADFF;background:#EFF4FF; }
.evidence-pager { display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:10px;margin:0 0 16px;font-size:12px;color:#475467; }
.pager-button { border:1px solid #D0D5DD;background:#fff;border-radius:7px;padding:5px 12px;color:#344054;font-size:12px;cursor:pointer; }
.pager-button:disabled { color:#98A2B3;background:#F8FAFC;cursor:not-allowed; }
.pager-button:focus-visible,.retry-button:focus-visible { outline:3px solid #84ADFF;outline-offset:2px; }
.evidence-grid { display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,270px),1fr));gap:16px; }
.evidence-card { overflow:hidden;border:1px solid #E4E7EC;border-radius:10px;background:#fff;min-width:0; }
.evidence-media { position:relative;display:block;width:100%;padding:0;border:0;background:#101828;overflow:hidden;line-height:0;cursor:zoom-in; }
.evidence-media img { display:block;width:100%;height:auto; }
.evidence-media:focus-visible,.inspect-button:focus-visible { outline:3px solid #84ADFF;outline-offset:-3px; }
.image-missing { display:grid;place-items:center;min-height:160px;color:#D0D5DD;font-size:12px;line-height:1.6; }
.detection-box { position:absolute;border:2px solid #36D6A8;pointer-events:none;box-shadow:0 0 0 1px #10182866; }
.box-caption { position:absolute;left:-2px;top:0;white-space:nowrap;background:#087F63;color:#fff;padding:3px 5px;font-size:10px;line-height:14px;border-radius:3px; }
.frame-time { position:absolute;bottom:8px;left:8px;padding:4px 7px;border-radius:4px;background:#101828CC;color:#fff;font:11px/1.3 monospace; }
.evidence-caption { padding:12px; }.evidence-title { display:flex;justify-content:space-between;align-items:center;gap:6px; }
.evidence-caption p { color:#667085;font-size:12px;margin:8px 0; }.evidence-title :deep(.ant-tag) { margin:0; }
.evidence-actions { display:flex;flex-wrap:wrap;align-items:center;gap:10px; }
.inspect-button { background:none;border:0;padding:0;color:#2457C5;font-size:12px;cursor:pointer; }.inspect-button:disabled { color:#98A2B3;cursor:default; }
.retry-button { border:1px solid #D0D5DD;background:#fff;border-radius:6px;padding:3px 10px;color:#344054;font-size:12px;cursor:pointer; }
.retry-button:disabled { color:#98A2B3;background:#F8FAFC;cursor:not-allowed; }
.evidence-note { color:#667085;font-size:12px;line-height:1.7;margin:14px 0 0; }.evidence-empty { text-align:center;background:#F8FAFC;border:1px dashed #D0D5DD;padding:26px 18px;color:#475467;border-radius:8px; }.evidence-empty p { color:#667085;font-size:12px; }
.preview-media { cursor:default;border-radius:8px;margin-inline:auto;max-width:100%; }.preview-scroll { max-width:100%; }.preview-summary { display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:16px 0;color:#475467;font-size:12px; }
.preview-error { display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:12px;color:#B42318;font-size:12px; }
.preview-media .box-caption { font-size:12px; }.model-details { font-size:12px;color:#667085;overflow-wrap:anywhere; }.model-details summary { cursor:pointer; }.model-details p { margin:7px 0; }
/* 缩放工具条：按钮沿用分页/重试按钮的视觉，焦点环与 .pager-button:focus-visible 保持一致。 */
.preview-toolbar { display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:12px; }
.zoom-button { border:1px solid #D0D5DD;background:#fff;border-radius:7px;padding:4px 10px;color:#344054;font-size:12px;line-height:1.6;cursor:pointer; }
.zoom-button:hover:not(:disabled) { border-color:#84ADFF;color:#1849A9; }
.zoom-button:disabled { color:#98A2B3;background:#F8FAFC;cursor:not-allowed; }
.zoom-button:focus-visible { outline:3px solid #84ADFF;outline-offset:2px; }
.zoom-level { font:12px/1.6 monospace;color:#475467;min-width:64px; }
.preview-close { margin-left:auto; }
.preview-actions { display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:12px 0 0; }
.preview-actions .evidence-note { margin:0; }

@media (max-width: 640px) {
  .evidence-toolbar { flex-direction:column;align-items:stretch; }
  .evidence-pager { justify-content:flex-start; }
  .preview-close { margin-left:0; }
}
</style>
