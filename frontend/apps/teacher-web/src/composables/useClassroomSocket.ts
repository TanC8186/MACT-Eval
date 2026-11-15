import { ref, onUnmounted } from 'vue'
import apiClient from '@/api/client'
import { ensureAccessToken, getAccessToken, isTokenUsable } from '@/api/authSession'

export interface BehaviorPoint {
  time: string
  upright: number | null
}

export interface BehaviorWindow {
  time: string
  upright: number | null
  coverage: number
  quality: string
}

export type SocketState = 'idle' | 'connecting' | 'connected' | 'offline' | 'auth-required'

export interface ClassroomSocketOptions {
  /** Current lesson / session id; evaluated when connect() is called */
  lessonId: () => string
  /** Background REST poll handler for analysis results */
  pollHandler?: () => void
  pollIntervalMs?: number
  /** Override token accessor (defaults to localStorage session) */
  getToken?: () => string | null
}

function formatTime(d: Date): string {
  return (
    d.getHours().toString().padStart(2, '0') +
    ':' +
    d.getMinutes().toString().padStart(2, '0')
  )
}

/**
 * 申请一次性 WebSocket 票据（SEC-02，2026-10-28）。
 *
 * 为什么不再把 JWT 放查询串：浏览器只能给 WebSocket 传 URL，因此旧实现写的是
 * `/ws/classroom?lessonId=..&token=<access JWT>`。nginx 默认访问日志记录完整
 * `$request`（含 query string），于是 24 小时有效的教师凭据被写进 access.log，
 * 并被 `docker compose logs teacher-web` 带出来。
 *
 * 现在每次建连前换一张 60 秒、单次消费、绑定课堂的票据。票据即使落进日志，
 * 也只能开一个已经用过的连接（第二次必然 401）。
 *
 * 失败一律返回 null，由调用方转成 `auth-required`：**不要**在这里回退到
 * `?token=`，那会把刚修好的泄漏重新打开。
 */
async function fetchWsTicket(lessonId: string): Promise<string | null> {
  try {
    // 必须用 apiClient 而不是裸 axios：Authorization 头由 apiClient 的请求
    // 拦截器注入，裸 axios 拿不到 access token，换票必然 401。这个错误不会
    // 在类型检查或构建时暴露——只会在浏览器里表现成"实时面板永远离线"。
    const { data } = await apiClient.post('/auth/ws-ticket', { lessonSessionId: lessonId })
    const ticket = data?.data?.ticket
    return typeof ticket === 'string' && ticket ? ticket : null
  } catch {
    return null
  }
}

/**
 * Classroom WebSocket lifecycle + lesson-room live state.
 *
 * - Handshake carries a one-time ticket (backend requires it); see fetchWsTicket.
 * - On an expired token the composable refreshes once, then reconnects.
 * - Reconnect uses exponential backoff and stops when the page/lesson changes.
 * - `generation` guards every async step so a late close/open from the previous
 *   lesson can never write into the new lesson's live state.
 */
export function useClassroomSocket(options: ClassroomSocketOptions) {
  const wsConnected = ref(false)
  const socketState = ref<SocketState>('idle')
  const socketError = ref('')
  const lastSyncedAt = ref<Date | null>(null)
  const uprightRate = ref<number | null>(null)
  const coverage = ref(0)
  const dataQuality = ref('')
  const behaviorHistory = ref<BehaviorPoint[]>([])
  const behaviorWindows = ref<BehaviorWindow[]>([])

  let ws: WebSocket | null = null
  let wsReconnectTimer: ReturnType<typeof setTimeout> | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let generation = 0
  let reconnectAttempt = 0
  let authRetried = false
  let disposed = false

  function applyBehaviorUpdate(d: Record<string, any>) {
    uprightRate.value = typeof d.upright_proportion === 'number' && Number.isFinite(d.upright_proportion)
      ? Math.round(d.upright_proportion * 100) : null
    coverage.value = Math.round((d.coverage || 0) * 100)
    dataQuality.value = d.data_quality || ''
    lastSyncedAt.value = new Date()
    const timeStr = formatTime(new Date())
    behaviorHistory.value.push({ time: timeStr, upright: uprightRate.value })
    if (behaviorHistory.value.length > 24) behaviorHistory.value.shift()
    behaviorWindows.value.push({
      time: timeStr,
      upright: uprightRate.value,
      coverage: coverage.value,
      quality: dataQuality.value,
    })
    if (behaviorWindows.value.length > 20) behaviorWindows.value.shift()
  }

  /** Clear lesson-scoped live state when switching classroom. */
  function resetLessonState() {
    // 新课允许一次认证重试；否则上一次课堂的失败会永久禁用刷新重连。
    authRetried = false
    uprightRate.value = null
    coverage.value = 0
    dataQuality.value = ''
    behaviorHistory.value = []
    behaviorWindows.value = []
    lastSyncedAt.value = null
    socketError.value = ''
  }

  function closeSocket() {
    if (!ws) return
    ws.onopen = null
    ws.onclose = null
    ws.onerror = null
    ws.onmessage = null
    try {
      ws.close(1000, 'client-switch')
    } catch {
      // already closed
    }
    ws = null
  }

  function scheduleReconnect(gen: number, reason: string) {
    if (disposed || gen !== generation) return
    if (wsReconnectTimer) return
    socketState.value = 'offline'
    socketError.value = reason
    const delay = Math.min(30_000, 1000 * Math.pow(2, Math.min(reconnectAttempt, 5)))
    reconnectAttempt++
    wsReconnectTimer = setTimeout(() => {
      wsReconnectTimer = null
      if (gen !== generation) return
      connect()
    }, delay)
  }

  async function connect() {
    const lessonId = options.lessonId()
    if (!lessonId || disposed) return

    const gen = ++generation
    if (wsReconnectTimer) {
      clearTimeout(wsReconnectTimer)
      wsReconnectTimer = null
    }
    closeSocket()
    socketState.value = 'connecting'
    socketError.value = ''

    // 先确保 access token 可用：票据接口本身要认证，token 过期时换票会 401。
    // （票据不等于令牌：它只用于建连，不能拿它调其它 API。）
    let token = options.getToken ? options.getToken() : getAccessToken()
    if (!isTokenUsable(token)) {
      token = await ensureAccessToken()
    }
    if (gen !== generation || disposed) return
    if (!token) {
      socketState.value = 'auth-required'
      socketError.value = '登录已过期，请重新登录'
      return
    }

    // 票据必须"换来即用"：60 秒 TTL、单次消费。此处不做缓存，
    // 重连走同一条路径重新换票。
    const ticket = await fetchWsTicket(lessonId)
    if (gen !== generation || disposed) return
    if (!ticket) {
      // 换票失败（课堂无权/课堂不存在/服务端异常）与"登录过期"分开表达，
      // 否则教师会以为重新登录就能连上，而真正原因是无权访问该课堂。
      socketState.value = 'auth-required'
      socketError.value = '无法获取课堂实时连接凭证，请确认课堂权限后重试'
      return
    }

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(
      protocol + '//' + location.host + '/ws/classroom?lessonId=' +
        encodeURIComponent(lessonId) + '&ticket=' + encodeURIComponent(ticket),
    )
    ws = socket

    socket.onopen = () => {
      if (gen !== generation) {
        try { socket.close(1000, 'stale') } catch { /* noop */ }
        return
      }
      wsConnected.value = true
      socketState.value = 'connected'
      socketError.value = ''
      reconnectAttempt = 0
      authRetried = false
      lastSyncedAt.value = new Date()
    }

    socket.onclose = (event) => {
      if (gen !== generation) return
      wsConnected.value = false
      if (event.code === 1000 || disposed) {
        socketState.value = 'idle'
        return
      }
      // 浏览器不会暴露 WebSocket 握手的状态码；第一次异常关闭时先尝试续期，
      // 续期成功则立即重连，避免 access token 过期导致永久离线。
      if (!authRetried && !isTokenUsable(getAccessToken())) {
        authRetried = true
        void (async () => {
          const fresh = await ensureAccessToken()
          if (gen !== generation) return
          if (fresh) {
            connect()
            return
          }
          socketState.value = 'auth-required'
          socketError.value = '登录已过期，请重新登录'
        })()
        return
      }
      scheduleReconnect(gen, '实时连接已断开，正在重连')
    }

    socket.onerror = () => {
      // onclose follows; keep the reason generic to avoid false specifics.
    }

    socket.onmessage = (e) => {
      if (gen !== generation || socket !== ws) return
      try {
        const msg = JSON.parse(e.data)
        const d = msg.data || {}
        if (msg.type === 'behavior:update') {
          applyBehaviorUpdate(d)

        }
      } catch {
        // ignore malformed messages
      }
    }
  }

  function startPolling() {
    stopPolling()
    if (!options.pollHandler) return
    const ms = options.pollIntervalMs ?? 10000
    pollTimer = setInterval(() => {
      options.pollHandler?.()
    }, ms)
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  function disconnect() {
    generation++
    disposed = true
    closeSocket()
    if (wsReconnectTimer) {
      clearTimeout(wsReconnectTimer)
      wsReconnectTimer = null
    }
    stopPolling()
    wsConnected.value = false
    socketState.value = 'idle'
  }

  onUnmounted(disconnect)

  return {
    wsConnected,
    socketState,
    socketError,
    lastSyncedAt,
    uprightRate,
    coverage,
    dataQuality,
    behaviorHistory,
    behaviorWindows,
    connect,
    disconnect,
    resetLessonState,
    startPolling,
    stopPolling,
  }
}
