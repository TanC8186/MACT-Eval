/**
 * 报告中历史版本选择器（P1-1）的静态契约回归。
 *
 * 运行（仓库根目录）：
 *   node --experimental-strip-types --no-warnings frontend/apps/teacher-web/tests/report-versions.test.mjs
 *
 * 三层覆盖：
 *   (a) Reports.vue 源码契约：版本接口、请求序号守卫、data-testid、XSS 与既有契约不回归；
 *   (a2) 版本选项文案的真实执行：把 Reports.vue 里的 DATA_SOURCE_LABELS /
 *        versionDataSourceLabel / formatVersionOptionLabel 原文抽出来执行，
 *        覆盖 dataSource 的四种取值（REAL/DEMO/EMPTY/缺失）并确认不产生空段；
 *   (b) endpoints.ts 提供者契约：两个版本接口必须导出且不得吞掉异常（403/404 要能传到调用方）。
 *
 * 说明：这不是 DOM 测试，不渲染组件；真实浏览器行为（抽屉打开、切换版本、深链接刷新）
 * 由 Playwright 验收脚本负责，本文件不声称验证过浏览器行为。
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const reportsSource = readFileSync(new URL('../src/pages/Reports.vue', import.meta.url), 'utf8')
const endpointsSource = readFileSync(new URL('../src/api/endpoints.ts', import.meta.url), 'utf8')

// ------------------------------------------------------------ (a) 源码契约
function assertReports(label, pattern) {
  assert.ok(pattern.test(reportsSource), `Reports.vue 源码契约未满足：${label}`)
}

/** 从 Reports.vue 截取一段源码：从 startMarker 起，到其后第一次出现的 endMarker（含）。 */
function extractBlock(startMarker, endMarker) {
  const start = reportsSource.indexOf(startMarker)
  assert.ok(start >= 0, `Reports.vue 源码契约未满足：找不到 ${startMarker}`)
  const end = reportsSource.indexOf(endMarker, start + startMarker.length)
  assert.ok(end > start, `Reports.vue 源码契约未满足：找不到 ${startMarker} 的结束标记`)
  return reportsSource.slice(start, end + endMarker.length)
}

/**
 * 去掉测试所需片段里的 TypeScript 注解，让它在 new Function 里可以直接执行。
 * 只做显式、窄范围的替换：一旦源码签名改了，这里会留下语法错误并让测试失败，而不是静默通过。
 */
function stripTypeAnnotations(code) {
  return code
    .replace(/:\s*Record<string, string>/g, '')
    .replace(/(\w+)\?:\s*string\s*\|\s*null/g, '$1')
    .replace(/(\w+)\?:\s*string/g, '$1')
    .replace(/(\w+):\s*number/g, '$1')
    .replace(/\)\s*:\s*string\s*\{/g, ') {')
}

/**
 * 真实执行 Reports.vue 里的版本选项文案代码（不是重写一份）。
 * formatDate 由测试注入固定实现，避免为了格式化日期把 dayjs 拉进测试进程。
 */
function loadOptionLabelBuilder() {
  const labelsBlock = stripTypeAnnotations(
    extractBlock('const DATA_SOURCE_LABELS: Record<string, string> = {', '\n}'))
  const dataSourceFn = stripTypeAnnotations(
    extractBlock('function versionDataSourceLabel(', '\n}'))
  const labelFn = stripTypeAnnotations(
    extractBlock('function formatVersionOptionLabel(', '\n}'))
  const factory = new Function(
    'formatDate',
    `${labelsBlock}\n${dataSourceFn}\n${labelFn}\nreturn { DATA_SOURCE_LABELS, versionDataSourceLabel, formatVersionOptionLabel }`,
  )
  return factory((value) => (value ? '2026-10-01 09:30' : '--'))
}

/** 抽出 `export async function <name>` 到下一个顶层 export 之间的函数体。 */
function functionBody(source, name) {
  const start = source.indexOf(`export async function ${name}(`)
  assert.ok(start >= 0, `endpoints.ts 未导出 ${name}`)
  const rest = source.slice(start + 1)
  const nextExport = rest.indexOf('\nexport ')
  return nextExport >= 0 ? rest.slice(0, nextExport) : rest
}

function testVersionEndpointsUsed() {
  // 版本接口路径：列表 /reports/{id}/versions 与快照 /reports/{id}/versions/{v}
  assertReports('引用版本列表接口', /\/reports\/\{lessonSessionId\}\/versions/)
  assertReports('引用版本快照接口', /\/reports\/\{lessonSessionId\}\/versions\/\{v\}/)
  // 两个接口通过 endpoints.ts 的封装调用，不在页面里重复拼 axios
  assertReports('导入 getReportVersions', /\bgetReportVersions\b/)
  assertReports('导入 getReportVersion', /\bgetReportVersion\b/)
  assertReports('从 @/api/endpoints 导入版本接口',
    /import \{[\s\S]{0,300}?getReportVersions,[\s\S]{0,300}?\} from '@\/api\/endpoints'/)
  // 当前版本仍走实时接口（避免历史快照冒充最新状态）
  assertReports('当前版本走实时接口', /apiClient\.get\('\/reports\/' \+ lessonSessionId\)/)
  assert.ok(!/apiClient\.get\('\/reports\/' \+ lessonSessionId \+ '\/versions' \+/.test(reportsSource),
    'Reports.vue 源码契约未满足：版本快照请求应走 endpoints.ts 封装')
}

function testRequestSequenceGuard() {
  // 单调请求序号既用于打开报告，也用于版本切换；晚到的旧响应必须被丢弃。
  assertReports('detailSeq 存在', /let detailSeq = 0\b/)
  assertReports('版本列表独立序号', /let versionsSeq = 0\b/)
  assertReports('序号自增强化', /const seq = \+\+detailSeq/)
  assertReports('晚到响应被丢弃', /if \(seq !== detailSeq\) return/)
  // 版本切换路径（fetchVersion）必须自己带守卫，而不是只靠调用方
  const fetchStart = reportsSource.indexOf('async function fetchVersion(')
  assert.ok(fetchStart >= 0, 'Reports.vue 源码契约未满足：缺少 fetchVersion')
  const fetchEnd = reportsSource.indexOf('\nasync function showVersion(', fetchStart)
  assert.ok(fetchEnd > fetchStart, 'Reports.vue 源码契约未满足：fetchVersion 作用域不完整')
  const fetchBody = reportsSource.slice(fetchStart, fetchEnd)
  assert.ok(/if \(seq !== detailSeq\) return false/.test(fetchBody),
    'Reports.vue 源码契约未满足：fetchVersion 内必须有请求序号守卫')
  assert.ok(fetchBody.indexOf('await') < fetchBody.indexOf('if (seq !== detailSeq)'),
    'Reports.vue 源码契约未满足：守卫必须出现在 await 之后（响应回来后校验）')
  // 抽屉关闭时让在途请求失效
  const closeStart = reportsSource.indexOf('function onDrawerClose()')
  assert.ok(closeStart >= 0, 'Reports.vue 源码契约未满足：缺少 onDrawerClose')
  const closeBody = reportsSource.slice(closeStart, reportsSource.indexOf('async function loadVersionList(', closeStart))
  assert.ok(/detailSeq\+\+/.test(closeBody) && /versionsSeq\+\+/.test(closeBody),
    'Reports.vue 源码契约未满足：关闭抽屉必须让 detailSeq 与 versionsSeq 自增作废在途响应')
}

function testDefaultAndLiveSelection() {
  // 默认选择 = 报告当前版本
  assertReports('默认版本取实时版本', /const target = version \?\? liveVersion\.value/)
  assertReports('实时版本来自接口', /liveVersion\.value = live\?\.version \?\? 1/)
  // 非当前版本才走快照接口
  assertReports('快照接口分支', /version === liveVersion\.value[\s\S]{0,200}?getReportVersion\(/)
  // 展示版本以已渲染的 detail 为准，避免标题与分数错位
  assertReports('展示版本取自 detail', /const displayedVersion = computed<number>\(\(\) => detail\.value\?\.version \?\? selectedVersion\.value\)/)
}

function testLoadingAndFailureState() {
  // 切换版本时保留旧内容 + 加载遮罩（detailLoading 驱动 a-spin）
  assertReports('切换中显示遮罩', /versionSwitchLoading\.value = true[\s\S]{0,120}?detailLoading\.value = true/)
  // 失败保留已展示版本并记录状态码
  assertReports('失败记录状态码', /versionErrorStatus\.value = err\?\.response\?\.status \|\| 0/)
  assertReports('失败时选择器回到已展示版本', /requestedVersion\.value = selectedVersion\.value/)
  // 403/404 不给重试
  assertReports('403/404 不提供重试',
    /versionRetryable = computed\(\(\) => !!versionError\.value\s*\n\s*&& versionErrorStatus\.value !== 403 && versionErrorStatus\.value !== 404\)/)
  assertReports('重试使用失败的目标版本', /const target = failedVersion\.value/)
  // 重试必须绕过「已选中直接返回」的短路，否则点击无反应
  assertReports('重试绕过短路', /void showVersion\(target, true\)/)
  assertReports('showVersion 支持 force', /async function showVersion\(version: number, force = false\)/)
  // 首次打开就失败时，版本条仍需渲染（否则没有重试入口）
  assertReports('失败时版本条可见', /<div v-if="detail \|\| versionError" class="version-bar">/)
  // 关闭抽屉：用户点关闭与程序化关闭走同一条复位路径
  assertReports('关闭后复位在途状态', /watch\(showDetail, \(open\) => \{\s*\n\s*if \(!open\) onDrawerClose\(\)/)
}

function testDeepLink() {
  assertReports('读取 query.report', /route\.query\.report/)
  assertReports('读取 query.version', /route\.query\.version/)
  assertReports('版本参数解析', /function parseVersionParam\(raw: unknown\)/)
  assertReports('写回 query 使用 router.replace', /router\.replace\(\{\s*\n\s*query: \{/)
  assertReports('打开报告时同步 query', /syncDetailQuery\(\)/)
  assertReports('关闭抽屉清除 query', /function clearDetailQuery\(\)[\s\S]{0,300}?delete rest\.report\s*\n\s*delete rest\.version/)
  assertReports('挂载时处理深链接', /onMounted\(async \(\) => \{[\s\S]{0,300}?parseVersionParam\(route\.query\.version\)/)
  assertReports('前进/后退同步', /watch\(\s*\n\s*\(\) => \[route\.query\.report, route\.query\.version\]/)
}

function testGalleryFollowsDisplayedVersion() {
  // 证据画廊的 key 与 version 必须都来自“实际展示”的 detail 版本
  assertReports('画廊 key 跟随展示版本', /:key="`\$\{detail\.lessonSessionId\}:\$\{displayedVersion\}`"/)
  assertReports('画廊 version 跟随展示版本', /:version="displayedVersion"/)
  assert.ok(!/:version="detail\.version"/.test(reportsSource),
    'Reports.vue 源码契约未满足：画廊不得直接绑定 detail.version（切换失败时会与展示版本错位）')
}

function testTestIds() {
  assertReports('选择器 testid', /data-testid="report-version-select"/)
  assertReports('选项 testid', /data-testid="report-version-option"/)
  assertReports('选项带 data-version', /:data-version="option\.value"/)
  assertReports('当前版本 testid', /data-testid="report-version-current"/)
  assertReports('切换中 testid', /data-testid="report-version-loading"/)
  assertReports('重试 testid', /data-testid="report-version-retry"/)
  // 选择器用原生 select：选项真实存在于 DOM，验收脚本可以直接定位
  assertReports('原生 select 选择器', /<select[\s\S]{0,200}?data-testid="report-version-select"/)
  // 既有契约不回归
  assertReports('保留 report-window-pending', /data-testid="report-window-pending"/)
  assertReports('保留 report-window-state', /data-testid="report-window-state"/)
  assertReports('保留证据画廊', /<BehaviorEvidenceGallery /)
}

/** 取出包含指定 data-testid 的那个完整开始标签（从最近的 '<' 到其后第一个 '>'）。 */
function elementWithTestId(testId) {
  const marker = `data-testid="${testId}"`
  const at = reportsSource.indexOf(marker)
  assert.ok(at >= 0, `Reports.vue 源码契约未满足：找不到 ${testId}`)
  const tagStart = reportsSource.lastIndexOf('<', at)
  const tagEnd = reportsSource.indexOf('>', at)
  assert.ok(tagStart >= 0 && tagEnd > at, `Reports.vue 源码契约未满足：${testId} 所在标签不完整`)
  return reportsSource.slice(tagStart, tagEnd + 1)
}

/**
 * 「当前渲染的是哪份 detail」探针的契约：
 *   - testid 恰好出现一次；
 *   - 绑定值只读 detail，绝不回落到 selectedVersion / requestedVersion。
 * 后者是反回退契约：同一课堂的两个版本可能只差 0.2 秒，发布时间只到分钟无法区分，
 * 而 displayedVersion 在 detail 缺失时会回落到选中版本，不能用来证明抽屉里的 payload。
 */
function assertDetailProbe(testId, attribute) {
  const occurrences = reportsSource.split(`data-testid="${testId}"`).length - 1
  assert.equal(occurrences, 1, `Reports.vue 源码契约未满足：${testId} 应恰好出现一次，实际 ${occurrences} 次`)

  const tag = elementWithTestId(testId)
  assert.ok(tag.includes(`data-testid="${testId}"`), `${testId} 标签内应带 testid`)
  assert.ok(tag.includes(`:${attribute}="detail?.`),
    `Reports.vue 源码契约未满足：${testId} 的 :${attribute} 必须只读 detail.*（实际：${tag}）`)
  assert.ok(/hidden/.test(tag), `Reports.vue 源码契约未满足：${testId} 探针应为 hidden 元素`)

  for (const fallback of ['selectedVersion', 'requestedVersion', 'displayedVersion']) {
    assert.ok(!tag.includes(fallback),
      `Reports.vue 源码契约未满足：${testId} 不得引用 ${fallback}（反回退契约：探针必须报告真实渲染的 payload）`)
  }

  // 兜底为空串，而不是 undefined 或上一个版本的值
  assert.match(tag, new RegExp(`:${attribute}="detail\\?\\.\\w+ \\?\\? ''"`),
    `Reports.vue 源码契约未满足：${testId} 的 :${attribute} 应在 detail 缺失时为空串`)
}

function testDetailPayloadProbes() {
  assertDetailProbe('report-detail-version', 'data-version')
  assertDetailProbe('report-detail-source', 'data-source')
}

function testOptionLabelAndPermissions() {
  // 选项文案：v{n} · 已发布|草稿 · YYYY-MM-DD HH:mm · 数据来源
  assertReports('选项文案四段式', /`v\$\{version\} · \$\{stateLabel\} · \$\{formatDate\(publishedAt\)\} · \$\{versionDataSourceLabel\(dataSource\)\}`/)
  assertReports('状态文案', /status === 'published' \? '已发布' : '草稿'/)
  assertReports('发布人放进 title', /item\.publishedBy \? `发布人：\$\{item\.publishedBy\}`/)
  // 快照条目把 dataSource 传进文案
  assertReports('快照条目传入 dataSource', /formatVersionOptionLabel\(\s*\n\s*version, item\.status, item\.publishedAt \?\? undefined, item\.dataSource,/)
  // 不在前端做权限绕过：403 只提示，不重试、不改请求
  assertReports('403 文案', /无权查看该报告的历史版本（403）/)
  assert.ok(!/roles?\s*[:=]\s*\[?\s*'admin'/.test(reportsSource),
    'Reports.vue 源码契约未满足：不应在前端按角色自行放行版本访问')
}

// ------------------------------------ 数据来源文案（真实执行 Reports.vue 的原文）
function testDataSourceLabels() {
  const { DATA_SOURCE_LABELS, versionDataSourceLabel, formatVersionOptionLabel } = loadOptionLabelBuilder()

  // 三种已知来源的映射
  assert.equal(versionDataSourceLabel('REAL'), '真实证据')
  assert.equal(versionDataSourceLabel('DEMO'), '演示')
  assert.equal(versionDataSourceLabel('EMPTY'), '无数据')
  assert.deepEqual(
    { ...DATA_SOURCE_LABELS },
    { REAL: '真实证据', DEMO: '演示', EMPTY: '无数据' },
    '数据来源映射应只有这三种取值（其余一律回落到「来源未记录」）',
  )

  // 缺失/未知来源必须给出明确说明，不能是空串、'null'、'undefined'
  for (const missing of [null, undefined, '', 'LEGACY', 'real']) {
    const label = versionDataSourceLabel(missing)
    assert.equal(label, '来源未记录', `dataSource=${JSON.stringify(missing)} 应回落为「来源未记录」`)
    assert.ok(label.trim().length > 0, `dataSource=${JSON.stringify(missing)} 不得产生空白文案`)
  }

  // 四段式完整文案：每段都非空，且段数固定为 4
  const cases = [
    ['REAL', 'v3 · 已发布 · 2026-10-01 09:30 · 真实证据'],
    ['DEMO', 'v3 · 已发布 · 2026-10-01 09:30 · 演示'],
    ['EMPTY', 'v3 · 已发布 · 2026-10-01 09:30 · 无数据'],
    [null, 'v3 · 已发布 · 2026-10-01 09:30 · 来源未记录'],
    [undefined, 'v3 · 已发布 · 2026-10-01 09:30 · 来源未记录'],
  ]
  for (const [dataSource, expected] of cases) {
    const label = formatVersionOptionLabel(3, 'published', '2026-10-01T09:30:00', dataSource)
    assert.equal(label, expected, `dataSource=${JSON.stringify(dataSource)} 的选项文案不符`)
    const segments = label.split(' · ')
    assert.equal(segments.length, 4, `选项文案应恰好 4 段：${label}`)
    for (const segment of segments) {
      assert.ok(segment.trim().length > 0, `选项文案出现空段：${label}`)
    }
    assert.ok(!/null|undefined/.test(label), `选项文案不得泄漏 null/undefined：${label}`)
  }

  // 草稿状态文案
  assert.equal(
    formatVersionOptionLabel(2, 'draft', '2026-10-02T08:00:00', 'REAL'),
    'v2 · 草稿 · 2026-10-01 09:30 · 真实证据',
  )
  // 发布时间缺失仍是可读的占位，不产生空段
  const noDate = formatVersionOptionLabel(1, 'draft', undefined, null)
  assert.match(noDate, /^v1 · 草稿 · -- · 来源未记录$/, `发布时间缺失时文案不符：${noDate}`)
}

function testNoVHtml() {
  assert.ok(!/\bv-html\b/.test(reportsSource), 'Reports.vue 源码契约未满足：不得使用 v-html')
}

// ---------------------------------------------- (b) endpoints.ts 提供者契约
function testProviderContract() {
  assert.match(endpointsSource, /export interface ReportVersionSummary \{[\s\S]{0,900}?\n\}/,
    'endpoints.ts 未定义 ReportVersionSummary')
  for (const field of ['version', 'status', 'publishedAt', 'publishedBy', 'contentHash',
    'analysisVideoJobId', 'analysisAudioJobId', 'dataSource', 'climateScore']) {
    assert.ok(new RegExp(`\\b${field}\\??\\s*:`).test(
      endpointsSource.slice(endpointsSource.indexOf('export interface ReportVersionSummary'),
        endpointsSource.indexOf('export interface ReportVersionSummary') + 700)),
    `ReportVersionSummary 缺少字段 ${field}`)
  }
  // 新字段必须显式允许 null（旧快照缺失时不回填默认值）
  const summaryBlock = endpointsSource.slice(
    endpointsSource.indexOf('export interface ReportVersionSummary'),
    endpointsSource.indexOf('export interface ReportVersionSummary') + 700)
  assert.match(summaryBlock, /dataSource\?:\s*string \| null/,
    'dataSource 应为可选且允许 null')
  assert.match(summaryBlock, /climateScore\?:\s*number \| null/,
    'climateScore 应为可选且允许 null')

  const listBody = functionBody(endpointsSource, 'getReportVersions')
  assert.match(listBody, /Promise<ReportVersionSummary\[\]>/, 'getReportVersions 返回类型应为 ReportVersionSummary[]')
  assert.match(listBody, /apiClient\.get<ApiResponse<ReportVersionSummary\[\]>>\(`\/reports\/\$\{lessonSessionId\}\/versions`\)/,
    'getReportVersions 应请求 /reports/{id}/versions')
  assert.match(listBody, /return res\.data\.data/, 'getReportVersions 应返回解包后的 data')

  const detailBody = functionBody(endpointsSource, 'getReportVersion')
  assert.match(detailBody, /Promise<any>/, 'getReportVersion 返回类型应为 Promise<any>')
  assert.match(detailBody, /apiClient\.get<ApiResponse<any>>\(`\/reports\/\$\{lessonSessionId\}\/versions\/\$\{version\}`\)/,
    'getReportVersion 应请求 /reports/{id}/versions/{v}')

  // 不得吞异常：没有 try/catch，也没有 `catch { return ... }` 默认值
  for (const [name, body] of [['getReportVersions', listBody], ['getReportVersion', detailBody]]) {
    assert.ok(!/\btry\s*\{/.test(body), `${name} 不得包 try/catch（调用方需要区分 403/404/网络错误）`)
    assert.ok(!/\bcatch\b/.test(body), `${name} 不得吞掉异常`)
  }
  assert.ok(!/getReportVersions[\s\S]{0,200}?catch \{ return \[\] \}/.test(endpointsSource),
    'getReportVersions 不得沿用 catch { return [] } 的静默降级写法')
}

// ---------------------------------------------------------------- runner
function main() {
  const checks = [
    ['版本接口使用', testVersionEndpointsUsed],
    ['请求序号守卫', testRequestSequenceGuard],
    ['默认版本与实时接口', testDefaultAndLiveSelection],
    ['加载/失败状态', testLoadingAndFailureState],
    ['深链接', testDeepLink],
    ['证据画廊跟随展示版本', testGalleryFollowsDisplayedVersion],
    ['data-testid 契约', testTestIds],
    ['detail 探针（反回退）', testDetailPayloadProbes],
    ['选项文案与权限', testOptionLabelAndPermissions],
    ['数据来源文案（真实执行）', testDataSourceLabels],
    ['XSS 守卫', testNoVHtml],
    ['endpoints 提供者契约', testProviderContract],
  ]
  for (const [label, fn] of checks) {
    fn()
    console.log(`  ok  ${label}`)
  }
  console.log('report-versions regression ok: Reports.vue source contract + endpoints provider contract')
}

main()
