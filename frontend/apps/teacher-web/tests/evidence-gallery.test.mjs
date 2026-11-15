/**
 * 报告交互回归（任务书 R08 / 台账 A8）。
 *
 * 运行（仓库根目录）：
 *   node --experimental-strip-types --no-warnings frontend/apps/teacher-web/tests/evidence-gallery.test.mjs
 *
 * 覆盖三部分：
 *   1. 真实执行 src/utils/duration.ts：时间格式化与分页 clamp；
 *   2. 真实执行 src/utils/evidenceMedia.ts：检测框可见性（解码前后/失效后）与重试后的画面状态；
 *   3. 对 BehaviorEvidenceGallery.vue **源码**做静态契约断言（下称“源码契约”）。
 *      这一部分不是 DOM 测试，不渲染组件；浏览器行为由 A10 的 Playwright 用例验证。
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const { formatDuration, paginate } = await import(new URL('../src/utils/duration.ts', import.meta.url).href)
const { ZOOM_MAX, ZOOM_MIN, ZOOM_STEP, clampZoom, mediaBoxVisible, mediaPending, mediaStateAfterRetry } =
  await import(new URL('../src/utils/evidenceMedia.ts', import.meta.url).href)
const gallerySource = readFileSync(new URL('../src/components/BehaviorEvidenceGallery.vue', import.meta.url), 'utf8')

// ---------------------------------------------------------------- 纯函数
function testFormatDuration() {
  assert.equal(formatDuration(0), '00:00:00')
  assert.equal(formatDuration(1000), '00:00:01')
  assert.equal(formatDuration(59000), '00:00:59')
  assert.equal(formatDuration(3599000), '00:59:59')
  assert.equal(formatDuration(3600000), '01:00:00')
  assert.equal(formatDuration(7199000), '01:59:59')
  assert.equal(formatDuration(7200000), '02:00:00')
  // 缺失时间不得伪装成 00:00
  assert.equal(formatDuration(null), '时间未知')
  assert.equal(formatDuration(undefined), '时间未知')
  assert.equal(formatDuration(Number.NaN), '时间未知')
  assert.equal(formatDuration(-1), '时间未知')
  assert.equal(formatDuration(Number.POSITIVE_INFINITY), '时间未知')
}

function testPaginate() {
  // 329 张、24/页 → 14 页，末页 17 张
  assert.deepEqual(paginate(329, 1, 24), { page: 1, pageCount: 14, start: 0, end: 24 })
  assert.deepEqual(paginate(329, 14, 24), { page: 14, pageCount: 14, start: 312, end: 329 })
  // 越界 clamp
  assert.deepEqual(paginate(329, 99, 24), { page: 14, pageCount: 14, start: 312, end: 329 })
  assert.deepEqual(paginate(329, 0, 24), { page: 1, pageCount: 14, start: 0, end: 24 })
  assert.deepEqual(paginate(329, -5, 24), { page: 1, pageCount: 14, start: 0, end: 24 })
  // total=0 时仍为 1 页
  assert.deepEqual(paginate(0, 1, 24), { page: 1, pageCount: 1, start: 0, end: 0 })
  assert.deepEqual(paginate(0, 7, 24), { page: 1, pageCount: 1, start: 0, end: 0 })
  assert.equal(paginate(0, 1, 24).pageCount, 1)
  // 正好整除与非整除边界
  assert.deepEqual(paginate(48, 2, 24), { page: 2, pageCount: 2, start: 24, end: 48 })
  assert.deepEqual(paginate(25, 2, 24), { page: 2, pageCount: 2, start: 24, end: 25 })
  // 数据量变小后原页码 clamp 到最后一页
  assert.equal(paginate(30, 5, 24).page, 2)
  // 非法输入不产生 NaN 页
  assert.equal(paginate(Number.NaN, 1, 24).pageCount, 1)
  assert.equal(paginate(10, Number.NaN, 24).page, 1)
}

// ------------------------------------------------- 源码契约（非 DOM 测试）
function assertSource(label, pattern) {
  assert.ok(pattern.test(gallerySource), `源码契约未满足：${label}`)
}

function testGallerySourceContract() {
  assertSource('每页 24 张', /const PAGE_SIZE = 24\b/)
  assertSource('渲染只取当前页切片', /visibleFrames = computed\(\(\) => filteredFrames\.value\.slice\(pageInfo\.value\.start, pageInfo\.value\.end\)\)/)
  assertSource('分页来自 paginate()', /paginate\(filteredFrames\.value\.length, page\.value, PAGE_SIZE\)/)
  assertSource('使用统一时间格式化函数', /formatDuration\(frame\.timestampMs\)/)
  assertSource('缺失时间文案', /时间未知/)
  assert.ok(!/function time\(/.test(gallerySource), '源码契约未满足：旧的 mm:ss 内联 time() 应已移除')
  assert.ok(/from '@\/utils\/duration'/.test(gallerySource), '源码契约未满足：应引用 utils/duration')
  assertSource('分页文案含页数/总数', /第 \{\{ pageInfo\.page \}\} \/ \{\{ pageInfo\.pageCount \}\} 页，共 \{\{ filteredFrames\.length \}\} 张/)
  assertSource('筛选改变后回到第 1 页', /function selectFilter\(code: string\) \{\s*filterCode\.value = code\s*page\.value = 1/)
  assertSource('刷新与重置分离：reload(resetState)', /async function reload\(resetState: boolean\)/)
  assertSource('切换报告/版本触发 reload(true)', /watch\(\(\) => \[props\.lessonId, props\.version\], \(\) => \{ void reload\(true\) \}/)
  assertSource('刷新按钮触发 reload(false)', /void reload\(false\)/)
  assertSource('关闭预览后焦点返回触发元素', /function onPreviewClosed\(\)[\s\S]{0,400}?\.focus\(\{ preventScroll: true \}\)/)
  assertSource('Modal 关闭走 after-close', /:after-close="onPreviewClosed"/)
  assertSource('图片加载失败文案', /画面已过期或不可用，请刷新/)
  assertSource('单帧重试入口', /async function retryFrame\(frameId: string\)/)
  assertSource('预览 img 处理加载失败', /@error="previewBroken = true"/)
  assertSource('浏览报告总数基于全量 items', /全部 \{\{ items\.length \}\}/)
  assertSource('工具栏可换行', /\.evidence-toolbar \{[^}]*flex-wrap:wrap/)
  assertSource('窄屏工具栏纵向', /@media \(max-width: 640px\)/)
  assertSource('保留代表帧含义说明', /截图数量不代表动作发生次数/)
  assertSource('保留目标编号含义说明', /目标编号只在当前画面内有效/)
  assertSource('保留人工复核说明', /需结合画面人工复核/)
  assertSource('采样间隔按课堂时长给出区间，不写死 10 秒', /采样间隔随课堂时长约 2–10 秒/)
  assert.ok(!/约每 10 秒采样一帧/.test(gallerySource), '源码契约未满足：「约每 10 秒采样一帧」对 20–40 分钟课堂不成立')
  assertSource('保留 AbortController', /new AbortController\(\)/)
  assertSource('保留请求序号绑定', /requestSeq/)
  // 浏览器验收（A10）依赖的稳定选择器
  assertSource('分页容器 testid', /data-testid="evidence-pager"/)
  assertSource('上一页 testid', /data-testid="evidence-prev"/)
  assertSource('下一页 testid', /data-testid="evidence-next"/)
  assertSource('页码状态 testid', /data-testid="evidence-page-status"/)
  assertSource('卡片 testid', /data-testid="evidence-card"/)
  assertSource('筛选按钮 testid', /data-testid="evidence-filter"/)
  assertSource('错误横幅 testid', /data-testid="evidence-error"/)
  assertSource('卡片重试 testid', /data-testid="evidence-retry"/)
  assertSource('卡片媒体按钮 testid', /data-testid="evidence-card-media"/)
  assertSource('刷新按钮 testid', /data-testid="evidence-refresh"/)
  assertSource('预览容器 testid', /data-testid="evidence-preview"/)
  assertSource('预览图片 testid', /data-testid="evidence-preview-image"/)
  assertSource('预览重试 testid', /data-testid="evidence-preview-retry"/)
  assertSource('预览时间 testid', /data-testid="evidence-preview-time"/)
  assertSource('目标切换 testid', /data-testid="evidence-target"/)
  // A15：契约对齐新增的属性（只加属性，不改行为）
  assertSource('卡片带 data-code', /data-testid="evidence-card"\s*\n\s*:data-frame-id="frame\.id" :data-window-index="frame\.windowIndex" :data-code="frame\.code"/)
  assertSource('媒体按钮带 data-frame-id', /data-testid="evidence-card-media"\s*\n\s*:data-frame-id="frame\.id" :data-window-index="frame\.windowIndex"/)
  assertSource('卡片时间节点 testid', /class="frame-time" data-testid="evidence-frame-time"/)
  assert.equal((gallerySource.match(/data-testid="evidence-detection-box"/g) || []).length, 2,
    '源码契约未满足：卡片与预览内都要有 evidence-detection-box')
  assertSource('分页容器可编程聚焦', /aria-label="证据画面分页" tabindex="-1" data-testid="evidence-pager"/)
  // F02–F06：审查 B 的语义/健壮性修复
  assertSource('主检测框兜底取置信度最高', /detections\.reduce<ActionDetection \| undefined>/)
  assertSource('窗口序号缺值不渲染 NaN', /Number\.isFinite\(frame\.windowIndex\) && frame\.windowIndex >= 0/)
  assertSource('预览渲染 identityScope', /identityScopeLabel\(activeFrame\.metadata\.identityScope\)/)
  assertSource('焦点回退到卡片或分页容器', /data-testid="evidence-pager"\]'\)\s*\n\s*fallback\?\.focus/)
  assertSource('重试进行中统一禁用其它重试', /const retryBusy = computed\(\(\) => !!retryingId\.value\)/)
  assert.ok(!/className\?:/.test(readFileSync(new URL('../src/types/evidence.ts', import.meta.url), 'utf8')),
    '源码契约未满足：未使用的 ActionDetection.className 不应保留声明')
  assertSource('卡片 aria-label 使用格式化时间', /查看 \$\{formatDuration\(frame\.timestampMs\)\} \$\{frame\.label\} 检测画面/)
}

// ------------------------------------------- 检测框可见性（R08，纯函数真实执行）
function testMediaBoxVisibility() {
  const url = 'https://minio.local/bucket/frame-1.jpg?X-Amz-Signature=abc'
  // 加载中：有 URL、未失败、未解码 → 不画框（否则框盖在空白上）
  assert.equal(mediaBoxVisible({ url, broken: false, decodedSrc: null }), false,
    'R08：图片加载中不得渲染检测框')
  assert.equal(mediaPending({ url, broken: false, decodedSrc: null }), true)
  // 解码成功 → 画框
  assert.equal(mediaBoxVisible({ url, broken: false, decodedSrc: url }), true,
    'R08：解码成功后应渲染检测框')
  assert.equal(mediaPending({ url, broken: false, decodedSrc: url }), false)
  // 失效（403 / 对象删除）→ 不画框，即使之前解码成功过
  assert.equal(mediaBoxVisible({ url, broken: true, decodedSrc: url }), false,
    'R08：图片失效后不得在错误占位图上叠加检测框')
  // 后端未给 URL → 不画框
  assert.equal(mediaBoxVisible({ url: null, broken: false, decodedSrc: null }), false)
  assert.equal(mediaBoxVisible({ url: '', broken: false, decodedSrc: '' }), false)
  // 解码成功但 URL 已换（新签名）→ 需要重新解码
  assert.equal(mediaBoxVisible({ url: `${url}&v=2`, broken: false, decodedSrc: url }), false,
    'R08：换新签名 URL 后必须重新解码才画框')
}

function testMediaStateAfterRetry() {
  const url = 'https://minio.local/bucket/frame-1.jpg?X-Amz-Signature=abc'
  // 重试拿到新签名：清掉失效标记，但不预置解码成功
  const retried = mediaStateAfterRetry(`${url}&v=2`, null)
  assert.deepEqual(retried, { url: `${url}&v=2`, broken: false, decodedSrc: null })
  assert.equal(mediaBoxVisible(retried), false, 'R08：重试成功后仍需等解码，不能立即画框')
  // 同一 URL（对象恢复了访问）：沿用上次解码结果即可显示
  assert.equal(mediaBoxVisible(mediaStateAfterRetry(url, url)), true,
    'R08：同一 URL 已解码过时，重试后无需等待即可显示框')
  // 服务端已无该帧 URL：标记失效并清掉解码记账
  assert.deepEqual(mediaStateAfterRetry(null, url), { url: null, broken: true, decodedSrc: null })
  assert.deepEqual(mediaStateAfterRetry(undefined, url), { url: null, broken: true, decodedSrc: null })
}

// --------------------------------- 源码契约：检测框受解码/可用状态守卫（R08）
function testDetectionBoxGuardedByDecodeContract() {
  // （a）卡片检测框的渲染必须由「可用 + 已解码」判定守卫，不能无条件 v-for。
  assert.match(gallerySource,
    /<template v-if="cardBoxesVisible\(frame\)">\s*\n\s*<span v-for="detection in highlighted\(frame\)"[^>]*data-testid="evidence-detection-box"/,
    '源码契约未满足（R08a）：卡片检测框必须包在 cardBoxesVisible(frame) 守卫内')
  const highlightedLoops = gallerySource.match(/<span v-for="detection in highlighted\(frame\)"/g) || []
  assert.equal(highlightedLoops.length, 1,
    '源码契约未满足（R08a）：卡片检测框只应有一处渲染点，且必须被守卫包住')
  assert.match(gallerySource, /function cardBoxesVisible\(frame: EvidenceFrame\) \{\s*\n\s*return mediaBoxVisible\(frameMedia\(frame\)\)/,
    '源码契约未满足（R08a）：cardBoxesVisible 必须走 mediaBoxVisible 判定')
  // 解码标志由 img 的 load 事件设置，且要求 naturalWidth > 0
  assert.match(gallerySource, /@load="markDecoded\(frame, \$event\)"/,
    '源码契约未满足（R08a）：卡片 img 必须监听 load 记录解码成功')
  assert.match(gallerySource, /function markDecoded\(frame: EvidenceFrame, event\?: Event\) \{\s*\n\s*const img = event\?\.target as HTMLImageElement \| null\s*\n\s*const width = img\?\.naturalWidth \?\? 0/,
    '源码契约未满足（R08a）：解码成功必须以 naturalWidth > 0 为准')
  // 预览同样受守卫（原 previewUnavailable 之外，还要等解码）
  assert.match(gallerySource, /<template v-if="previewBoxesVisible">/,
    '源码契约未满足（R08a）：预览检测框必须由 previewBoxesVisible 守卫')
  assert.doesNotMatch(gallerySource, /<template v-if="!previewUnavailable">/,
    '源码契约未满足（R08a）：预览不得只判断「不可用」，否则加载中仍会画框')
  assert.match(gallerySource, /@load="markPreviewDecoded"/,
    '源码契约未满足（R08a）：预览 img 必须监听 load')
  // 失效时同时清掉解码记账，避免残留成功标志
  assert.match(gallerySource, /function markBroken\(frame: EvidenceFrame\) \{\s*\n\s*brokenImages\.value\[frame\.id\] = true\s*\n\s*delete decodedImages\.value\[frame\.id\]/,
    '源码契约未满足（R08a）：markBroken 必须清除该帧的解码成功标志')

  // （b）失效 → 重试成功后 brokenImages 被清除（retryFrame 源码契约 + 纯函数已验证）
  assert.match(gallerySource, /const previous = frameMedia\(fresh\)\s*\n\s*const next = mediaStateAfterRetry\(fresh\.url, previous\.decodedSrc\)/,
    '源码契约未满足（R08b）：retryFrame 必须用 mediaStateAfterRetry 计算重试后的画面状态')
  assert.match(gallerySource, /if \(next\.broken\) \{\s*\n\s*brokenImages\.value\[frameId\] = true[\s\S]{0,400}?\} else \{\s*\n\s*delete brokenImages\.value\[frameId\]/,
    '源码契约未满足（R08b）：重试成功后必须清除 brokenImages 标记')
  assert.match(gallerySource, /if \(next\.decodedSrc\) decodedImages\.value\[frameId\] = next\.decodedSrc\s*\n\s*else delete decodedImages\.value\[frameId\]/,
    '源码契约未满足（R08b）：重试后不得预置解码成功')
  // 帧消失（对象真实删除）时同时清理两种记账
  assert.match(gallerySource, /items\.value = items\.value\.filter\(f => f\.id !== frameId\)\s*\n\s*delete brokenImages\.value\[frameId\]/,
    '源码契约未满足（R08b）：帧被删除时须清除失效标记')
  assert.match(gallerySource, /if \(!frame \|\| frame\.url !== decodedImages\.value\[id\]\) delete decodedImages\.value\[id\]/,
    '源码契约未满足（R08b）：数据替换后须按 URL 收敛解码记账')
}

testFormatDuration()
testPaginate()
testMediaBoxVisibility()
testMediaStateAfterRetry()
testGallerySourceContract()
testDetectionBoxGuardedByDecodeContract()
console.log('evidence-gallery regression ok: duration/paginate + media box visibility + gallery source contract')


function testSamplingLabelContract() {
  // 报告必须显示本次任务实际生效的采样间隔，而不是写死 10 秒。
  assert.ok(gallerySource.includes('samplingLabel'),
    '组件应通过 samplingLabel 渲染实际采样间隔')
  assert.ok(gallerySource.includes('effectiveSampleIntervalSec'),
    '应读取后端下发的 effectiveSampleIntervalSec')
  assert.ok(!/约每 10 秒采样一帧/.test(gallerySource),
    '不得再写死「约每 10 秒采样一帧」')
}

testSamplingLabelContract()

// ------------------------------------- 预览缩放（纯函数真实执行）
function testClampZoom() {
  // 边界常量本身就是合法倍数，且 min < max、步长为 0.25
  assert.equal(ZOOM_MIN, 1)
  assert.equal(ZOOM_MAX, 4)
  assert.equal(ZOOM_STEP, 0.25)
  // 合法值原样通过
  assert.equal(clampZoom(1), 1)
  assert.equal(clampZoom(2), 2)
  assert.equal(clampZoom(4), 4)
  assert.equal(clampZoom(1.25), 1.25)
  assert.equal(clampZoom(3.75), 3.75)
  // 上下界 clamp：连续点击不会越界
  assert.equal(clampZoom(0.5), ZOOM_MIN)
  assert.equal(clampZoom(0), ZOOM_MIN)
  assert.equal(clampZoom(-3), ZOOM_MIN)
  assert.equal(clampZoom(4.25), ZOOM_MAX)
  assert.equal(clampZoom(99), ZOOM_MAX)
  assert.equal(clampZoom(Number.POSITIVE_INFINITY), ZOOM_MIN)
  assert.equal(clampZoom(Number.NEGATIVE_INFINITY), ZOOM_MIN)
  // 非数字输入不得把样式写成 calc(... * NaN)
  assert.equal(clampZoom(Number.NaN), ZOOM_MIN)
  assert.equal(clampZoom(undefined), ZOOM_MIN)
  assert.equal(clampZoom(null), ZOOM_MIN)
  assert.equal(clampZoom('2'), ZOOM_MIN)
  // 步长吸附：去掉浮点残差，避免出现 110.00000000000001%
  assert.equal(clampZoom(1.1), 1)
  assert.equal(clampZoom(1.13), 1.25)
  assert.equal(clampZoom(1 + 0.25 + 0.25 + 0.25), 1.75)
  // 结果一定落在区间内且是步长整数倍
  for (const raw of [-1, 0, 0.4, 1.02, 2.37, 3.99, 4.5, 12, Number.NaN]) {
    const value = clampZoom(raw)
    assert.ok(value >= ZOOM_MIN && value <= ZOOM_MAX, `clampZoom(${raw}) 越界：${value}`)
    assert.equal(Math.round((value * 100) / 25) * 25 / 100, value, `clampZoom(${raw}) 未吸附到 25% 步长：${value}`)
  }
}

testClampZoom()

// --------------------------------- 源码契约：预览缩放/切换/关闭/原视频入口
function testPreviewZoomSourceContract() {
  const testids = [
    'evidence-preview-zoom-in', 'evidence-preview-zoom-out', 'evidence-preview-zoom-reset',
    'evidence-preview-close', 'evidence-preview-zoom-level', 'evidence-jump-to-video',
    'evidence-preview-nav-prev', 'evidence-preview-nav-next',
  ]
  for (const id of testids) {
    assert.ok(gallerySource.includes(`data-testid="${id}"`), `源码契约未满足：缺少 ${id}`)
  }
  // 原视频回放未接入：必须渲染成明确禁用、且有解释，不能装成可用按钮
  assert.match(gallerySource,
    /data-testid="evidence-jump-to-video"\s+disabled/,
    '源码契约未满足：evidence-jump-to-video 必须以 disabled 渲染')
  assert.match(gallerySource,
    /data-testid="evidence-jump-to-video"[\s\S]{0,240}?aria-describedby="evidence-jump-to-video-hint"/,
    '源码契约未满足：原视频入口须通过 aria-describedby 说明为何不可用')
  assert.match(gallerySource, /原视频回放尚未接入/, '源码契约未满足：须写明原视频回放尚未接入')
  // 缩放换算走纯函数，且实际参与预览宽度
  assert.match(gallerySource, /const previewZoom = ref\(ZOOM_MIN\)/, '缩放初值应为 ZOOM_MIN（100%）')
  assert.match(gallerySource, /const zoomPercent = computed\(\(\) => `\$\{Math\.round\(clampZoom\(previewZoom\.value\) \* 100\)\}%`\)/,
    '缩放倍数文案应经 clampZoom 换算成百分比')
  assert.match(gallerySource, /const previewWidth = computed\([\s\S]{0,700}?clampZoom\(previewZoom\.value\)/,
    '预览宽度必须由 previewZoom 决定')
  assert.match(gallerySource, /function changeZoom\(delta: number\) \{ previewZoom\.value = clampZoom\(previewZoom\.value \+ delta\) \}/,
    '缩放按钮应通过 clampZoom 收敛')
  assert.match(gallerySource, /function resetZoom\(\) \{ previewZoom\.value = ZOOM_MIN \}/, '重置应回到 ZOOM_MIN')
  // 边界禁用，便于 Playwright 直接断言 disabled
  assert.match(gallerySource, /data-testid="evidence-preview-zoom-out"[\s\S]{0,160}?:disabled="previewZoom <= ZOOM_MIN"/,
    '缩小按钮在最小倍数时应 disabled')
  assert.match(gallerySource, /data-testid="evidence-preview-zoom-in"[\s\S]{0,160}?:disabled="previewZoom >= ZOOM_MAX"/,
    '放大按钮在最大倍数时应 disabled')
  // 关闭按钮走与 antd 相同的关闭路径，焦点仍由 after-close 归还
  assert.match(gallerySource, /data-testid="evidence-preview-close"[\s\S]{0,200}?@click="closePreview"/,
    '关闭按钮应调用 closePreview')
  assert.match(gallerySource, /function closePreview\(\) \{ previewOpen\.value = false \}/,
    'closePreview 必须置 previewOpen=false，复用 after-close 的焦点归还')
  assertSource('Modal 关闭仍走 after-close', /:after-close="onPreviewClosed"/)
  // 方向键：仅左右切帧，到边界不拦截默认行为，组件卸载时解绑
  assert.match(gallerySource, /window\.addEventListener\('keydown', onPreviewKeydown\)/, '键盘切帧需绑定 keydown')
  assert.match(gallerySource, /window\.removeEventListener\('keydown', onPreviewKeydown\)/, '组件卸载须解绑 keydown，避免监听泄漏')
  assert.match(gallerySource, /event\.key === 'ArrowLeft'/, '← 应切到上一张')
  assert.match(gallerySource, /event\.key === 'ArrowRight'/, '→ 应切到下一张')
  assert.match(gallerySource, /function stepPreview\(offset: number\)[\s\S]{0,400}?openFrame\(target\)/,
    '切换帧必须复用 openFrame，保持选中目标与解码记账一致')
  // 时间播报：aria-live 让读屏在切帧后读出当前帧时间
  assert.match(gallerySource, /data-testid="evidence-preview-time" aria-live="polite"/,
    'evidence-preview-time 需要 aria-live 播报当前帧时间')
  // 面板内不得注入 HTML
  assert.ok(!/v-html/.test(gallerySource), '源码契约未满足：组件不得使用 v-html')
  // 缩放不得打断「解码后才画框」的守卫
  assert.match(gallerySource, /<template v-if="previewBoxesVisible">/,
    '源码契约未满足：缩放后预览检测框仍须由 previewBoxesVisible 守卫')
  assert.match(gallerySource, /function cardBoxesVisible\(frame: EvidenceFrame\)/,
    '源码契约未满足：卡片检测框守卫不得被改动')
  assert.match(gallerySource, /function onPreviewClosed\(\)/, '焦点归还函数须保留')
  assert.match(gallerySource, /async function retryFrame\(frameId: string\)/, '单帧重试须保留')
  // 横向溢出只在预览滚动容器内部，不推到页面
  assert.match(gallerySource, /class="preview-scroll" :style="\{ overflowX: 'auto', overflowY: 'hidden', maxWidth: '100%' \}"/,
    '预览需在自身滚动容器内横向滚动')
  assert.match(gallerySource, /\.preview-media \{[^}]*max-width:100%/, '高倍缩放时 .preview-media 仍需 maxWidth 守卫')
  assert.match(gallerySource, /\.zoom-button:focus-visible \{ outline:3px solid #84ADFF;outline-offset:2px; \}/,
    '缩放按钮需有与分页按钮一致的可见焦点样式')
  // 不引入任何指向学生身份的措辞：「姓名/学号」只允许出现在「不对应姓名或学号」这条否定说明里。
  assert.match(gallerySource, /不对应姓名或学号/, '源码契约未满足：目标编号不指代身份的说明须保留')
  const withoutIdentityNote = gallerySource.replace(/不对应姓名或学号/g, '')
  assert.ok(!/姓名|学号|学生画像/.test(withoutIdentityNote),
    '源码契约未满足：除「不对应姓名或学号」外，不得出现学生身份相关措辞')
  assert.match(gallerySource, /function targetName\(value: string\) \{ return `目标 \$\{/, '目标编号措辞须保留')
  // 采样间隔 / identityScope 说明不得被缩放改动删掉
  assert.match(gallerySource, /identityScopeLabel\(activeFrame\.metadata\.identityScope\)/, '模型面板须保留 identityScope')
  assert.match(gallerySource, /samplingLabel\(activeFrame\)/, '预览摘要须保留采样间隔')
}

testPreviewZoomSourceContract()
