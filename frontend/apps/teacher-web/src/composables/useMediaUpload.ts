import { ref, computed, onUnmounted } from 'vue'
import axios from 'axios'
import { message } from 'ant-design-vue'
import apiClient from '@/api/client'

const VIDEO_EXTS = ['mp4', 'avi', 'mov', 'mkv']
const AUDIO_EXTS = ['wav', 'mp3', 'm4a', 'flac']

/**
 * 扩展名 → 上传时声明的 MIME（SEC-03，2026-10-28）。
 *
 * 为什么要这张表：服务端要求 init 声明 contentType 并与扩展名匹配。
 * 浏览器的 file.type 有时是空串（取决于操作系统是否注册了该扩展名），
 * 此时若发 application/octet-stream，服务端会按"我不知道"拒绝——那会把一个
 * 合法文件挡在外面。按扩展名映射比"猜一个通用类型"更准确，也不会放宽校验。
 */
const EXT_MIME: Record<string, string> = {
  mp4: 'video/mp4',
  avi: 'video/x-msvideo',
  mov: 'video/quicktime',
  mkv: 'video/x-matroska',
  wav: 'audio/wav',
  mp3: 'audio/mpeg',
  m4a: 'audio/mp4',
  flac: 'audio/flac',
}

function contentTypeFor(file: File): string {
  if (file.type) return file.type
  const ext = (file.name.split('.').pop() || '').toLowerCase()
  return EXT_MIME[ext] || 'application/octet-stream'
}
const DEFAULT_SAMPLE_INTERVAL = 2
const DEFAULT_WINDOW_DURATION = 60
const MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
const JOB_STORAGE_KEY = 'mact.mediaJobs.v1'
const MAX_STORED_JOBS = 20

export interface MediaUploadOptions {
  /** Current lesson session id */
  lessonId: () => string
  /** Seat count for the analysis job */
  totalSeats: () => number
  /** Called after a media analysis job reaches a terminal state */
  onJobFinished?: (job: MediaJob, lessonId: string) => void | Promise<void>
}

export type MediaJobStatus =
  | 'UPLOADING'
  | 'QUEUED'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'PARTIAL'
  | 'FAILED'
  | 'CANCELLED'

export interface MediaJob {
  id: string
  jobId: string
  lessonId: string
  fileName: string
  mediaType: 'video' | 'audio'
  objectName?: string
  assetId?: string
  totalSeats: number
  sampleInterval: number
  windowDuration: number
  status: MediaJobStatus
  progress: number
  error?: string
  createdAt: number
  updatedAt: number
}

function newId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function loadStoredJobs(): MediaJob[] {
  try {
    const raw = localStorage.getItem(JOB_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed as MediaJob[] : []
  } catch {
    localStorage.removeItem(JOB_STORAGE_KEY)
    return []
  }
}

/**
 * Classroom media upload → MinIO presigned PUT → async analysis job.
 *
 * 关键不变量（FE04/FE05）：
 *  - 启动上传时固定 file/type/lesson/seats 快照，后续 await 不再读取可变 UI 状态；
 *  - 每个任务独立记录 jobId、课堂、状态与失败原因，支持并行与刷新恢复；
 *  - 取消上传使用 AbortController，取消后不会再发送 confirm；
 *  - 轮询使用递归 setTimeout，避免异步 setInterval 重叠；
 *  - 403/404 停止无效轮询并保留可读原因。
 */
export function useMediaUpload(options: MediaUploadOptions) {
  const mediaFile = ref<File | null>(null)
  const mediaType = ref<'video' | 'audio'>('video')
  const mediaUploading = ref(false)
  const mediaProgress = ref(0)
  const jobs = ref<MediaJob[]>(loadStoredJobs())
  const activeLessonId = ref('')

  const timers = new Map<string, ReturnType<typeof setTimeout>>()
  let abortController: AbortController | null = null

  // 未选择课堂时不展示任何历史任务，避免同一浏览器切换账号后泄露他人文件名。
  const lessonJobs = computed(() =>
    activeLessonId.value
      ? jobs.value.filter(job => job.lessonId === activeLessonId.value)
      : [],
  )
  const mediaJobStatus = computed(() => lessonJobs.value.at(-1)?.status ?? '')

  function persist() {
    try {
      const trimmed = jobs.value.slice(-MAX_STORED_JOBS)
      jobs.value = trimmed
      localStorage.setItem(JOB_STORAGE_KEY, JSON.stringify(trimmed))
    } catch {
      // localStorage 不可用时仅保留内存状态
    }
  }

  function setActiveLesson(lessonId: string) {
    activeLessonId.value = lessonId
  }

  function updateJob(id: string, patch: Partial<MediaJob>) {
    const index = jobs.value.findIndex(job => job.id === id)
    if (index < 0) return
    jobs.value[index] = { ...jobs.value[index], ...patch, updatedAt: Date.now() }
    persist()
  }

  function handleMediaBeforeUpload(file: File): false {
    if (mediaUploading.value) {
      message.warning('当前有文件正在上传，请等待完成或取消')
      return false
    }
    const ext = (file.name.split('.').pop() || '').toLowerCase()
    if (VIDEO_EXTS.includes(ext)) mediaType.value = 'video'
    else if (AUDIO_EXTS.includes(ext)) mediaType.value = 'audio'
    else {
      message.error('仅支持视频（MP4/AVI/MOV/MKV）或音频（WAV/MP3/M4A/FLAC）文件')
      return false
    }
    if (file.size > MAX_FILE_BYTES) {
      message.error('单个文件不能超过 2GB，请压缩或分段后再上传')
      return false
    }
    mediaFile.value = file
    return false
  }

  function clearMediaFile() {
    if (mediaUploading.value) return
    mediaFile.value = null
  }

  function formatMediaSize(bytes: number) {
    if (bytes >= 1024 * 1024 * 1024) return (bytes / 1024 / 1024 / 1024).toFixed(1) + ' GB'
    if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB'
    return Math.round(bytes / 1024) + ' KB'
  }

  async function startMediaUpload() {
    // 快照：从这里开始只使用局部变量，后续 UI 变更不得影响本任务。
    const file = mediaFile.value
    const type = mediaType.value
    const lessonId = options.lessonId()
    const seats = options.totalSeats()
    if (!file || !lessonId) {
      message.warning('请先选择文件并选择课堂')
      return
    }
    if (mediaUploading.value) return

    const job: MediaJob = {
      id: newId(),
      jobId: '',
      lessonId,
      fileName: file.name,
      mediaType: type,
      totalSeats: seats,
      sampleInterval: DEFAULT_SAMPLE_INTERVAL,
      windowDuration: DEFAULT_WINDOW_DURATION,
      status: 'UPLOADING',
      progress: 0,
      createdAt: Date.now(),
      updatedAt: Date.now(),
    }
    jobs.value.push(job)
    persist()
    mediaUploading.value = true
    mediaProgress.value = 0
    abortController = new AbortController()

    try {
      // SEC-03（2026-10-28）：init 必须声明大小与 MIME。服务端据此在 confirm
      // 用 statObject 的实测值对照——声明值与实际上传字节数不一致会被 409 拒绝，
      // 因此这里传入的就是即将上传的 File 自身的值，不能估算。
      const initRes = await apiClient.post('/upload/media', {
        mediaType: type,
        fileName: file.name,
        lessonSessionId: lessonId,
        sizeBytes: file.size,
        contentType: contentTypeFor(file),
      })
      const { uploadUrl, objectName, assetId } = initRes.data.data
      updateJob(job.id, { objectName, assetId })

      await axios.put(uploadUrl, file, {
        headers: { 'Content-Type': file.type || 'application/octet-stream' },
        signal: abortController.signal,
        onUploadProgress: (e: any) => {
          if (!e.total) return
          const percent = Math.round((e.loaded * 100) / e.total)
          mediaProgress.value = percent
          updateJob(job.id, { progress: percent })
        },
      })
      mediaProgress.value = 100
      updateJob(job.id, { progress: 100 })

      if (abortController.signal.aborted) {
        updateJob(job.id, { status: 'CANCELLED', error: '用户取消上传' })
        return
      }

      const confirmRes = await apiClient.post('/upload/media/confirm', {
        assetId,
        objectName,
        lessonSessionId: lessonId,
        totalSeats: seats,
        sampleInterval: DEFAULT_SAMPLE_INTERVAL,
        windowDuration: DEFAULT_WINDOW_DURATION,
      })
      const jobId = confirmRes.data.data.jobId
      updateJob(job.id, { jobId, status: 'QUEUED', error: undefined })
      message.success('上传成功，异步分析任务已进入队列')
      pollJob(job.id)
    } catch (err: any) {
      if (err?.code === 'ERR_CANCELED' || abortController?.signal.aborted) {
        updateJob(job.id, { status: 'CANCELLED', error: '用户取消上传' })
        return
      }
      const status = err?.response?.status
      const reason = status === 413
        ? '文件超过 2GB 限制'
        : err?.response?.data?.message || err?.message || '上传失败'
      updateJob(job.id, { status: 'FAILED', error: reason })
      message.error(reason)
    } finally {
      mediaUploading.value = false
      abortController = null
      if (mediaFile.value === file) mediaFile.value = null
    }
  }

  function pollJob(internalId: string) {
    const job = jobs.value.find(j => j.id === internalId)
    if (!job?.jobId) return
    if (timers.has(internalId)) return

    const tick = async () => {
      timers.delete(internalId)
      const current = jobs.value.find(j => j.id === internalId)
      if (!current || !current.jobId) return
      try {
        const r = await apiClient.get(`/jobs/${current.jobId}`)
        const st: MediaJobStatus = r.data?.data?.status ?? 'RUNNING'
        updateJob(internalId, { status: st, error: r.data?.data?.errorMessage || undefined })
        if (st === 'SUCCEEDED' || st === 'PARTIAL' || st === 'FAILED') {
          if (st === 'SUCCEEDED') message.success('异步分析完成，结果已落库')
          else message.error(st === 'FAILED' ? '异步分析失败，请查看任务详情' : '异步分析部分完成')
          await options.onJobFinished?.(current, current.lessonId)
          return
        }
      } catch (err: any) {
        const status = err?.response?.status
        if (status === 403 || status === 404) {
          updateJob(internalId, {
            status: 'FAILED',
            error: status === 403 ? '无权查看该任务' : '任务不存在或已被清理',
          })
          return
        }
        // 网络抖动：保留 QUEUED/RUNNING，由下一次 tick 重试。
      }
      timers.set(internalId, setTimeout(tick, 3000))
    }

    timers.set(internalId, setTimeout(tick, 1000))
  }

  function cancelMediaUpload() {
    abortController?.abort()
    // 上传阶段的任务立即标记取消；已进入分析队列的任务仅停止本地跟踪。
    for (const job of jobs.value) {
      if (job.status === 'UPLOADING') {
        updateJob(job.id, { status: 'CANCELLED', error: '用户取消上传' })
      }
    }
  }

  async function retryMediaJob(internalId: string) {
    const job = jobs.value.find(j => j.id === internalId)
    if (!job || !job.objectName || !job.assetId || job.status !== 'FAILED') return
    updateJob(internalId, { status: 'QUEUED', error: undefined })
    try {
      const confirmRes = await apiClient.post('/upload/media/confirm', {
        assetId: job.assetId,
        objectName: job.objectName,
        lessonSessionId: job.lessonId,
        totalSeats: job.totalSeats,
        sampleInterval: job.sampleInterval,
        windowDuration: job.windowDuration,
      })
      updateJob(internalId, { jobId: confirmRes.data.data.jobId, status: 'QUEUED' })
      pollJob(internalId)
    } catch (err: any) {
      updateJob(internalId, {
        status: 'FAILED',
        error: err?.response?.data?.message || err?.message || '重试失败',
      })
    }
  }

  /** 页面刷新后恢复未完成任务并继续轮询。 */
  function restoreMediaJobs() {
    jobs.value = loadStoredJobs()
    for (const job of jobs.value) {
      if ((job.status === 'QUEUED' || job.status === 'RUNNING') && job.jobId) {
        pollJob(job.id)
      }
    }
  }

  function stopMediaPoll() {
    for (const timer of timers.values()) clearTimeout(timer)
    timers.clear()
  }

  function mediaJobStatusColor(s: string) {
    if (s === 'SUCCEEDED') return 'green'
    if (s === 'FAILED') return 'red'
    if (s === 'PARTIAL' || s === 'CANCELLED') return 'orange'
    return 'processing'
  }

  function mediaJobStatusLabel(s: string) {
    const map: Record<string, string> = {
      UPLOADING: '上传中',
      QUEUED: '排队中',
      RUNNING: '分析中',
      SUCCEEDED: '已完成',
      PARTIAL: '部分完成',
      FAILED: '失败',
      CANCELLED: '已取消',
    }
    return map[s] || s
  }

  onUnmounted(stopMediaPoll)

  return {
    mediaFile,
    mediaType,
    mediaUploading,
    mediaProgress,
    mediaJobStatus,
    mediaJobs: lessonJobs,
    setActiveLesson,
    handleMediaBeforeUpload,
    clearMediaFile,
    formatMediaSize,
    startMediaUpload,
    cancelMediaUpload,
    retryMediaJob,
    restoreMediaJobs,
    stopMediaPoll,
    mediaJobStatusColor,
    mediaJobStatusLabel,
  }
}
