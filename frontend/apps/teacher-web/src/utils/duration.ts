/**
 * 报告交互用的纯函数：时间格式化与分页计算。
 *
 * 说明：证据帧的 timestampMs 可能为 null（历史抽帧没有精确时间），
 * 这里必须显式返回「时间未知」，不能把缺失时间伪装成 00:00。
 */

export interface PageInfo {
  page: number
  pageCount: number
  start: number
  end: number
}

function pad2(value: number): string {
  return value.toString().padStart(2, '0')
}

/** 毫秒 → HH:MM:SS；null/undefined/NaN/负数 → 「时间未知」。 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '时间未知'
  const totalSeconds = Math.floor(ms / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return `${pad2(hours)}:${pad2(minutes)}:${pad2(seconds)}`
}

/**
 * 计算分页区间（start/end 为数组 slice 下标）。
 * page 越界时 clamp 到 [1, pageCount]；total=0 时 pageCount=1。
 */
export function paginate(total: number, page: number, pageSize: number): PageInfo {
  const safeTotal = Number.isFinite(total) ? Math.max(0, Math.floor(total)) : 0
  const size = Number.isFinite(pageSize) && pageSize > 0 ? Math.floor(pageSize) : 1
  const pageCount = Math.max(1, Math.ceil(safeTotal / size))
  const requested = Number.isFinite(page) ? Math.floor(page) : 1
  const current = Math.min(Math.max(1, requested), pageCount)
  const start = (current - 1) * size
  return { page: current, pageCount, start, end: Math.min(start + size, safeTotal) }
}
