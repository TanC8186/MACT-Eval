/**
 * 证据画面与检测框的状态判定（纯函数，便于离线单测）。
 *
 * 只有「有 URL、未加载失败、且图片已成功解码」三者同时成立，才允许叠加检测框：
 * 加载中提前画框会盖在空白上，失效占位（黑底）后继续画框则会像一份有效识别结果，
 * 而画面本身已不可复核（2026-10-27 复核 R08）。
 *
 * 解码结果按 URL 记账：重试拿到新签名 URL 后必须重新解码，不能沿用旧 URL 的成功标志，
 * 否则框又会在新画面尚未显示时先画出来。
 */

export interface FrameMediaState {
  /** 当前可用的签名 URL；null 表示后端没有给出可用链接。 */
  url: string | null
  /** 图片已触发 error（或后端已明确该帧无 URL）。 */
  broken: boolean
  /** 解码成功时的 URL；与 url 不一致表示「需要重新解码」。 */
  decodedSrc: string | null
}

/** 检测框是否允许渲染：三者同时满足才为 true。 */
export function mediaBoxVisible(state: FrameMediaState): boolean {
  return typeof state.url === 'string' && state.url.length > 0
    && !state.broken && state.decodedSrc === state.url
}

/** 画面是否处于「正在加载、既未成功也未失败」的中间态。 */
export function mediaPending(state: FrameMediaState): boolean {
  return typeof state.url === 'string' && state.url.length > 0
    && !state.broken && state.decodedSrc !== state.url
}

/**
 * 重试（或整份刷新带回新签名）后该帧应有的画面状态。
 *
 * - `freshUrl` 为空：服务端已无可用链接，标记失效，清掉解码成功的记账；
 * - `freshUrl` 与上次解码成功的 URL 相同：解码结果仍然有效，直接恢复显示；
 * - 否则：清掉失效标记，但必须先重新解码才允许画框。
 */
export function mediaStateAfterRetry(
  freshUrl: string | null | undefined,
  previousDecodedSrc: string | null,
): FrameMediaState {
  if (!freshUrl) return { url: null, broken: true, decodedSrc: null }
  return {
    url: freshUrl,
    broken: false,
    decodedSrc: previousDecodedSrc === freshUrl ? freshUrl : null,
  }
}

/** 预览缩放倍数的下界（1 = 适配容器宽度）、上界（400%）与步长。 */
export const ZOOM_MIN = 1
export const ZOOM_MAX = 4
export const ZOOM_STEP = 0.25

/**
 * 把任意输入收敛成合法的缩放倍数。
 *
 * - 非数字 / NaN / ±Infinity：回到最小值，避免 `width: calc(... * NaN)` 把画面渲染成空白；
 * - 越界：clamp 到 [ZOOM_MIN, ZOOM_MAX]，连续点击放大/缩小不会溢出；
 * - 吸附到 ZOOM_STEP 的整数倍：防止 1.1000000000000001 这类浮点残差进入样式与百分比文案。
 */
export function clampZoom(value: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return ZOOM_MIN
  const clamped = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, value))
  const snapped = Math.round(clamped / ZOOM_STEP) * ZOOM_STEP
  const bounded = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, snapped))
  return Number(bounded.toFixed(2))
}
