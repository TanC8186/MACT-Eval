/**
 * 认证会话回归：只执行 teacher-web 真实的 authSession.ts。
 *
 * 运行：node --experimental-strip-types --no-warnings tests/auth-session.test.mjs
 * 正式单端交付只检查 teacher-web；学生端源码保留为历史原型，不参与本回归。
 * 若 loadModule 被指向 teacher-web 以外的应用，本文件直接失败，避免单端范围被
 * 悄悄拓宽（2026-10-27 单端交付审查）。
 *
 * 覆盖审查 FE01 的可复现场景：
 *   1. 连续两轮刷新必须提交轮换后的 refresh token；
 *   2. 10 个并发刷新只触发一次网络请求；
 *   3. 刷新失败清理会话，可续期状态不残留；
 *   4. base64url JWT 解析与站内 redirect 白名单。
 */
import assert from 'node:assert/strict'
import axios from 'axios'

class MemoryStorage {
  constructor() { this.map = new Map() }
  getItem(key) { return this.map.has(key) ? this.map.get(key) : null }
  setItem(key, value) { this.map.set(key, String(value)) }
  removeItem(key) { this.map.delete(key) }
  clear() { this.map.clear() }
}

globalThis.localStorage = new MemoryStorage()
globalThis.CustomEvent = class CustomEvent {
  constructor(type) { this.type = type }
}
globalThis.window = {
  location: { pathname: '/dashboard', search: '?x=1' },
  dispatchEvent() {},
}

let refreshCalls = 0
let submittedRefreshTokens = []
let failNext = false
let networkFail = false
let sequence = 0

axios.defaults.adapter = async (config) => {
  if (config.url !== '/api/auth/refresh') {
    throw new Error(`unexpected request: ${config.url}`)
  }
  refreshCalls++
  const body = typeof config.data === 'string' ? JSON.parse(config.data) : config.data
  submittedRefreshTokens.push(body.refreshToken)
  if (networkFail) {
    throw new Error('ECONNREFUSED')
  }
  if (failNext) {
    const error = new Error('refresh rejected')
    error.response = { status: 401, data: { code: 401, message: '刷新令牌已失效' } }
    throw error
  }
  sequence++
  return {
    data: {
      code: 200,
      message: 'success',
      data: {
        token: `access-${sequence}`,
        refreshToken: `refresh-${sequence}`,
        userId: 'user-1',
        role: 'teacher',
        name: '测试教师',
      },
    },
    status: 200,
    statusText: 'OK',
    headers: {},
    config,
  }
}

const modules = new Map()

async function loadModule(app) {
  // 每个应用的模块只加载一次；单飞 Promise 在每次刷新结束后会自行清空。
  if (!modules.has(app)) {
    const url = new URL(`../../../apps/${app}/src/api/authSession.ts`, import.meta.url).href
    modules.set(app, await import(url))
  }
  return modules.get(app)
}

function resetStorage() {
  localStorage.clear()
  refreshCalls = 0
  submittedRefreshTokens = []
  failNext = false
  networkFail = false
  sequence = 0
}

async function testSequentialRotation(app) {
  resetStorage()
  const auth = await loadModule(app)
  auth.saveLoginResponse({
    token: 'access-0', refreshToken: 'refresh-0',
    userId: 'user-1', role: 'teacher', name: '测试教师',
  })

  await auth.refreshSession()
  assert.equal(localStorage.getItem('token'), 'access-1')
  assert.equal(localStorage.getItem('refreshToken'), 'refresh-1',
    `${app}: 第一轮刷新后必须保存轮换后的 refresh token`)

  await auth.refreshSession()
  assert.deepEqual(submittedRefreshTokens, ['refresh-0', 'refresh-1'],
    `${app}: 第二轮必须提交轮换后的 token，而不是已消费的 refresh-0`)
  assert.equal(localStorage.getItem('refreshToken'), 'refresh-2')
}

async function testConcurrentSingleFlight(app) {
  resetStorage()
  const auth = await loadModule(app)
  auth.saveLoginResponse({
    token: 'access-0', refreshToken: 'refresh-0',
    userId: 'user-1', role: 'teacher', name: '测试教师',
  })

  const results = await Promise.all(Array.from({ length: 10 }, () => auth.refreshSession()))
  assert.equal(refreshCalls, 1, `${app}: 10 个并发刷新只能产生 1 次请求`)
  assert.equal(new Set(results.map(r => r.token)).size, 1)
  assert.equal(localStorage.getItem('refreshToken'), 'refresh-1')
}

async function testFailureClearsSession(app) {
  resetStorage()
  failNext = true
  const auth = await loadModule(app)
  auth.saveLoginResponse({
    token: 'access-0', refreshToken: 'refresh-0',
    userId: 'user-1', role: 'teacher', name: '测试教师',
  })

  const token = await auth.ensureAccessToken()
  assert.equal(token, null)
  assert.equal(localStorage.getItem('token'), null)
  assert.equal(localStorage.getItem('refreshToken'), null,
    `${app}: 刷新失败后必须一次性清理 access/refresh`)
}

function base64url(value) {
  return Buffer.from(JSON.stringify(value)).toString('base64url')
}

async function testNetworkFailureKeepsSession(app) {
  resetStorage()
  networkFail = true
  const auth = await loadModule(app)
  auth.saveLoginResponse({
    token: 'access-0', refreshToken: 'refresh-0',
    userId: 'user-1', role: 'teacher', name: '测试教师',
  })

  const token = await auth.ensureAccessToken()
  assert.equal(token, null)
  assert.equal(localStorage.getItem('refreshToken'), 'refresh-0',
    `${app}: 网络错误不能删除仍可续期的 refresh token`)
}

async function testJwtAndRedirect(app) {
  resetStorage()
  const auth = await loadModule(app)
  const future = Math.floor(Date.now() / 1000) + 3600
  const token = `h.${base64url({ sub: 'u1', role: 'teacher', exp: future })}.s`
  assert.equal(auth.decodeJwtPayload(token).role, 'teacher')
  assert.equal(auth.isTokenUsable(token), true)
  assert.equal(auth.isTokenUsable(`h.${base64url({ exp: 1 })}.s`), false)

  assert.equal(auth.sanitizeRedirect('/reports?tab=1'), '/reports?tab=1')
  assert.equal(auth.sanitizeRedirect('//evil.example'), null)
  assert.equal(auth.sanitizeRedirect('https://evil.example'), null)
  assert.equal(auth.sanitizeRedirect('/\\evil'), null)
}

// 单端交付门禁：只允许 teacher-web。放宽这个列表等于把学生端重新纳入正式回归。
const DELIVERED_APPS = ['teacher-web']

for (const app of DELIVERED_APPS) {
  if (app !== 'teacher-web') {
    throw new Error(
      `auth-session 回归只覆盖正式交付的 teacher-web，被请求的是 ${app}；` +
        '学生端源码保留为历史原型，不参与本回归。',
    )
  }
  await testSequentialRotation(app)
  await testConcurrentSingleFlight(app)
  await testFailureClearsSession(app)
  await testNetworkFailureKeepsSession(app)
  await testJwtAndRedirect(app)
  console.log(`auth-session regression ok: ${app}`)
}
