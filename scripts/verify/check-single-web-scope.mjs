#!/usr/bin/env node
/**
 * 单端交付范围门禁（2026-10-27 范围决定，2026-10-28 补齐自动检查）。
 *
 * 为什么需要它：`student-web` 已经下线，但"下线"靠的是人记住不要把它加回去。
 * 这类决定会以三种方式被悄悄推翻，而且都不会让任何现有测试变红：
 *
 *   1. 正式 Compose 重新加一个 `student-web` service（包括新增的 compose 文件）；
 *   2. workspace / package.json 重新把它拉进 `pnpm -r <script>` 的覆盖范围
 *      ——**直接写成员名**或**用 `frontend/apps/*` 这类通配符**都算；
 *   3. 后端或 ai-service 的 CORS 默认值重新放行学生端端口 3001（以及双端时代
 *      的第二个 Vite 端口 5174/5175）——配置层面的"还有第二个 Web 端"。
 *
 * 本脚本把这三条变成机器检查，任一条命中即非零退出。
 *
 * 2026-10-28 复核修复（独立复核者证明的三个 fail-open）：
 *   H1  只扫两个写死的 compose 文件 -> 改成扫仓库根下**所有** `docker-compose*.yml`；
 *   H2  workspace 成员只做子串匹配 -> 改成把成员 pattern 解析到文件系统，
 *       任何被匹配到的非 teacher-web 应用目录（以及非 packages/*）一律失败；
 *   H3  `docker compose config` 任何异常都记成"跳过" -> 改成区分
 *       「docker CLI / daemon 不可用」（信息性跳过）与「compose 文件本身非法」（失败）；
 *   H4  门禁自身没有测试 -> 增加 `--self-test`，在临时副本上注入变异并断言全部被拒。
 *
 * 用法：
 *   node scripts/verify/check-single-web-scope.mjs               # 检查
 *   node scripts/verify/check-single-web-scope.mjs --json        # 机器可读
 *   node scripts/verify/check-single-web-scope.mjs --self-test   # 门禁自测（变异必须全部被拒）
 *   node scripts/verify/check-single-web-scope.mjs --root <dir>  # 对指定根目录执行同一套检查
 *
 * 说明：不删除 student-web 源码——它作为历史原型保留，所以本脚本只检查
 * 构建/部署/门禁/默认配置的**生效路径**，不把源码目录本身当作违规。
 */
import {
  readFileSync, writeFileSync, existsSync, readdirSync, statSync, mkdirSync, copyFileSync, rmSync,
} from 'node:fs'
import { execFileSync } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const DEFAULT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
// Compose 自己也认 `compose.yml` / `compose.yaml`（以及带后缀的变体），所以
// 门禁按同一套名字扫，而不是只扫 `docker-compose*.yml`——否则把 service 写进
// `compose.override.yml` 又是一个 fail-open。pnpm-*.yaml 这类非 compose 文件不匹配。
const COMPOSE_PATTERN = /^(docker-)?compose(\.[A-Za-z0-9_-]+)?\.ya?ml$/
// 仅用于 `docker compose config` 语法校验的占位密钥：不会启动任何容器。
const CONFIG_ONLY_ENV = {
  MACT_JWT_SECRET: 'config-check-only-placeholder-0000000000',
  MACT_SERVICE_TOKEN: 'config-check-only-placeholder-0000000000',
}

function argValue(flag) {
  const index = process.argv.indexOf(flag)
  return index >= 0 ? process.argv[index + 1] : null
}

// ---------------------------------------------------------------------------
// 检查实现（接收 root，便于 --self-test 对临时副本执行同一套逻辑）
// ---------------------------------------------------------------------------
function runChecks(root) {
  const failures = []
  const passes = []

  function read(rel) {
    const path = join(root, rel)
    if (!existsSync(path) || !statSync(path).isFile()) return null
    return readFileSync(path, 'utf8')
  }

  function check(name, ok, detail) {
    if (ok) passes.push({ name, detail })
    else failures.push({ name, detail })
  }

  // -------------------------------------------------------------- 1. Compose
  // H1：不写死文件名，扫根目录下所有 docker-compose*.yml。新增一个 compose
  // 文件（例如 docker-compose.staging.yml）会被自动纳入，不需要改本脚本。
  const composeFiles = existsSync(root)
    ? readdirSync(root)
      .filter((name) => COMPOSE_PATTERN.test(name))
      .filter((name) => statSync(join(root, name)).isFile())
      .sort()
    : []
  check(
    '至少存在一个 compose 文件（docker-compose*.yml / compose*.yml）',
    composeFiles.length > 0,
    composeFiles.join(', ') || '未找到 compose 文件',
  )

  for (const file of composeFiles) {
    const text = read(file)
    if (text == null) {
      check(`compose 文件存在: ${file}`, false, '文件缺失')
      continue
    }
    const serviceNames = parseServiceNames(text)
    check(
      `${file} 的 services 段可解析`,
      serviceNames.length > 0,
      serviceNames.join(', ') || '未能解析 services 段',
    )
    check(
      `${file} 不含 student-web service`,
      serviceNames.length > 0 && !serviceNames.includes('student-web'),
      serviceNames.join(', ') || '未能解析 services 段',
    )
  }

  // 用 compose 自己的解析器再确认一次。H3：必须区分两种失败——
  //   * docker CLI / daemon 不可用  -> 信息性跳过（文件级检查已覆盖）；
  //   * docker 可用但 compose 非法  -> 失败（以前这里被 catch 成"通过"）。
  const dockerUsable = dockerInfoOk()
  if (!dockerUsable) {
    passes.push({
      name: 'docker compose 解析',
      detail: 'docker info 不可用，跳过（文件级检查已覆盖；这不代表 compose 文件合法）',
    })
  } else {
    // 叠加文件（acceptance/cpu/capacity）单独 `config` 会因为缺 image/build 而报错，
    // 必须叠在基础文件上校验。基础文件按 Compose 自己的优先级取，不写死。
    const baseCompose = pickBaseCompose(composeFiles)
    for (const file of composeFiles) {
      const result = composeConfig(root, file, baseCompose)
      check(`docker compose config 校验: ${file}`, result.ok, result.detail)
    }
    const services = composeServices(root, baseCompose)
    if (services.error != null) {
      check('docker compose config --services 可解析', false, services.error)
    } else {
      check(
        'docker compose config --services 不含 student-web',
        !services.names.includes('student-web'),
        services.names.join(', ') || '未解析到任何 service',
      )
      check(
        'docker compose config --services 含 teacher-web',
        services.names.includes('teacher-web'),
        services.names.join(', '),
      )
    }
  }

  // ------------------------------------------- 2. workspace / package.json
  // H2：成员 pattern 必须解析到文件系统，而不是只看成员行里有没有 "student-web"。
  // `frontend/apps/*` 这种通配符会把 student-web 重新拉回 `pnpm -r build`。
  const workspace = read('pnpm-workspace.yaml')
  if (workspace == null) {
    check('pnpm-workspace.yaml 存在', false, '文件缺失')
  } else {
    const members = parseWorkspaceMembers(workspace)
    check('pnpm-workspace.yaml 可解析出成员', members.length > 0, members.join(', ') || '未解析到成员')

    // 成员行里直接写 student-web：即使目录不存在也视为违规意图。
    const literal = members.filter((m) => m.includes('student-web'))
    check('pnpm-workspace.yaml 成员不含 student-web 字面量', literal.length === 0, literal.join(', '))

    // 把每个 pattern 展开到**实际存在**的应用/包目录上。
    const patterns = members.map(globToRegExp)
    const candidates = listFrontendPackages(root)
    const resolved = candidates.filter((rel) => patterns.some((re) => re.test(rel)))
    const offending = resolved.filter(
      (rel) => rel !== 'frontend/apps/teacher-web' && !rel.startsWith('frontend/packages/'),
    )
    check(
      'workspace 成员展开后只包含 teacher-web（及 packages/*）',
      offending.length === 0,
      offending.length === 0
        ? `展开结果: ${resolved.join(', ') || '∅'}`
        : `被拉入的非正式应用: ${offending.join(', ')}（展开结果: ${resolved.join(', ') || '∅'}）`,
    )
    check(
      'workspace 成员展开后包含 teacher-web',
      resolved.includes('frontend/apps/teacher-web'),
      `展开结果: ${resolved.join(', ') || '∅'}`,
    )
  }

  const pkgText = read('package.json')
  let pkg = {}
  try {
    pkg = JSON.parse(pkgText ?? '{}')
  } catch {
    check('package.json 可解析', false, 'JSON 解析失败')
  }

  // H2 补充：`package.json` 的 `workspaces` 字段同样能定义 workspace 成员
  // （pnpm 在没有 pnpm-workspace.yaml 时读取它）。用同一套展开逻辑检查，
  // 否则把 student-web 挪到那里就是一个新的 fail-open。
  if (pkg.workspaces != null) {
    const raw = Array.isArray(pkg.workspaces)
      ? pkg.workspaces
      : (Array.isArray(pkg.workspaces.packages) ? pkg.workspaces.packages : [])
    const fieldMembers = raw.map((m) => String(m).replace(/^\.\//, '').replace(/\/$/, ''))
    const fieldPatterns = fieldMembers.map(globToRegExp)
    const fieldResolved = listFrontendPackages(root)
      .filter((rel) => fieldPatterns.some((re) => re.test(rel)))
    const fieldOffending = fieldResolved.filter(
      (rel) => rel !== 'frontend/apps/teacher-web' && !rel.startsWith('frontend/packages/'),
    )
    check(
      'package.json workspaces 字段未把非正式应用拉回 workspace',
      fieldOffending.length === 0,
      fieldOffending.length === 0
        ? `workspaces 展开结果: ${fieldResolved.join(', ') || '∅'}`
        : `被拉入的非正式应用: ${fieldOffending.join(', ')}`,
    )
  } else {
    passes.push({
      name: 'package.json 无 workspaces 字段',
      detail: 'workspace 成员只由 pnpm-workspace.yaml 定义',
    })
  }

  const scripts = Object.keys(pkg.scripts ?? {})
  const scriptOffenders = scripts.filter((name) => {
    const cmd = String(pkg.scripts[name])
    // teacher-web 是唯一正式入口；出现 student-web 的脚本一律违规。
    return cmd.includes('student-web') || name.includes('student-web')
  })
  check('package.json 脚本不含 student-web', scriptOffenders.length === 0, scriptOffenders.join(', '))

  const buildWeb = String(pkg.scripts?.['build:web'] ?? '')
  const typecheckWeb = String(pkg.scripts?.['typecheck:web'] ?? '')
  check('build:web 只构建 teacher-web', buildWeb.includes('teacher-web'), buildWeb)
  check('typecheck:web 只检查 teacher-web', typecheckWeb.includes('teacher-web'), typecheckWeb)

  // ---------------------------------------------------------- 3. CORS 默认值
  const retiredPorts = ['3001', '5174', '5175']

  function originsOf(raw) {
    return String(raw ?? '')
      .split(',')
      .map((o) => o.trim())
      .filter(Boolean)
  }

  // backend: application.yml 的 mact.cors.allowed-origins
  const appYml = read('backend/src/main/resources/application.yml')
  if (appYml != null) {
    const match = appYml.match(/^\s*allowed-origins:\s*(.+)$/m)
    const origins = originsOf(match?.[1])
    check('backend CORS 默认来源可解析', origins.length > 0, String(match?.[1] ?? ''))
    for (const port of retiredPorts) {
      const hit = origins.filter((o) => o.endsWith(`:${port}`))
      check(`backend CORS 默认值不含端口 ${port}`, hit.length === 0, hit.join(', '))
    }
    check('backend CORS 默认值含 5173（teacher-web 开发态）',
      origins.some((o) => o.endsWith(':5173')), origins.join(', '))
  } else {
    check('application.yml 存在', false, '文件缺失')
  }

  // ai-service: app/core/config.py 的 cors_origins 默认值
  const aiConfig = read('ai-service/app/core/config.py')
  if (aiConfig != null) {
    const block = aiConfig.match(/cors_origins:\s*str\s*=\s*\(([\s\S]*?)\)/)
    const inline = aiConfig.match(/cors_origins:\s*str\s*=\s*"([^"]*)"/)
    const raw = block
      ? [...block[1].matchAll(/"([^"]*)"/g)].map((m) => m[1]).join(',')
      : (inline?.[1] ?? '')
    // 相邻字符串字面量会被拼成一个逗号串（如 "...3000,http://localhost:5173," 之后
    // 紧接下一行的 "http://127..."），join 后可能留下空段，originsOf 会过滤掉。
    const origins = originsOf(raw)
    check('ai-service CORS 默认来源可解析', origins.length > 0, raw.slice(0, 160))
    for (const port of retiredPorts) {
      const hit = origins.filter((o) => o.endsWith(`:${port}`))
      check(`ai-service CORS 默认值不含端口 ${port}`, hit.length === 0, hit.join(', '))
    }
    check('ai-service CORS 默认值含 5173（teacher-web 开发态）',
      origins.some((o) => o.endsWith(':5173')), origins.join(', '))
  } else {
    check('ai-service config.py 存在', false, '文件缺失')
  }

  return { passes, failures, composeFiles }
}

// --------------------------------------------------------------- 辅助函数
/**
 * 只取 `services:` 段下的二级 key。
 *
 * 不假设固定缩进（Compose 允许 2 空格、4 空格或任意一致的缩进）：进入 `services:`
 * 后，第一行有效内容的缩进即 service 键的缩进层级；只有**恰好**这个缩进的行才算
 * service 名，服务体内部的更深缩进键（image/environment/...）不算。注释行与空行
 * 跳过；缩进回退到 `services:` 同级即视为该段结束。
 */
function parseServiceNames(text) {
  const names = []
  let serviceIndent = null
  for (const raw of text.split(/\r?\n/)) {
    if (/^\s*#/.test(raw) || raw.trim() === '') continue
    const line = raw.replace(/\s+$/, '')

    if (serviceIndent == null) {
      if (/^services:\s*(#.*)?$/.test(line)) serviceIndent = 0
      continue
    }
    // services: 之后的第一行有效内容决定 service 键缩进。
    // 键后面允许跟值（`student-web: {image: nginx}` 这类流式写法），只要缩进
    // 恰好等于 service 缩进就算 service；服务体内更深的键缩进不同，自然被排除。
    const match = line.match(/^( *)(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_.-]+)):(\s.*)?$/)
    if (!match) continue
    const indent = match[1].length
    if (serviceIndent === 0) {
      if (indent === 0) {
        // 回到顶层键（volumes/networks/...）：services 段结束。
        serviceIndent = null
        continue
      }
      serviceIndent = indent
    }
    if (indent === serviceIndent) names.push(match[2] ?? match[3] ?? match[4])
    else if (indent === 0) serviceIndent = null
  }
  return names
}

function parseWorkspaceMembers(text) {
  const members = []
  let inPackages = false
  for (const line of text.split(/\r?\n/)) {
    if (/^\s*#/.test(line) || line.trim() === '') continue
    if (!/^\s/.test(line)) {
      inPackages = /^packages:\s*(#.*)?$/.test(line)
      continue
    }
    if (!inPackages) continue
    const match = line.match(/^\s*-\s*(.+?)\s*$/)
    if (!match) continue
    members.push(match[1].replace(/^['"]|['"]$/g, '').replace(/^\.\//, '').replace(/\/$/, ''))
  }
  return members
}

/** 把 workspace 成员 pattern 编译成正则：`*` 单层、`**` 跨层。 */
function globToRegExp(pattern) {
  let out = '^'
  for (let i = 0; i < pattern.length; i += 1) {
    const ch = pattern[i]
    if (ch === '*') {
      if (pattern[i + 1] === '*') {
        out += '.*'
        i += 1
        if (pattern[i + 1] === '/') i += 1
      } else {
        out += '[^/]*'
      }
    } else if (ch === '?') {
      out += '[^/]'
    } else {
      out += ch.replace(/[.+^${}()|[\]\\]/g, '\\$&')
    }
  }
  return new RegExp(`${out}$`)
}

/**
 * 仓库里实际存在的前端应用/共享包目录（相对路径，`/` 分隔）。
 *
 * 不写死 `frontend/apps` + `frontend/packages`：成员 pattern 若能匹配到其它
 * 位置的 package.json，那同样会把该包拉进 `pnpm -r`。所以这里在 `frontend/`
 * 下按深度扫描（跳过 node_modules/dist/build 等），凡是带 package.json 的目录
 * 都算候选，再交由成员 pattern 判断。
 */
function listFrontendPackages(root) {
  const found = []
  const skip = new Set(['node_modules', 'dist', 'build', 'coverage', '.vite', '.turbo'])
  const MAX_DEPTH = 3

  function walk(relDir, depth) {
    if (depth > MAX_DEPTH) return
    const dir = join(root, relDir)
    if (!existsSync(dir)) return
    if (existsSync(join(dir, 'package.json'))) found.push(relDir)
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue
      if (skip.has(entry.name) || entry.name.startsWith('.')) continue
      walk(`${relDir}/${entry.name}`, depth + 1)
    }
  }

  walk('frontend', 0)
  return found.sort()
}

function dockerInfoOk() {
  try {
    execFileSync('docker', ['info'], {
      stdio: ['ignore', 'ignore', 'ignore'],
      timeout: 20000,
    })
    return true
  } catch {
    return false
  }
}

/**
 * 基础 compose 文件（`docker compose config` 的起点）。
 *
 * Compose 的默认查找顺序是 `compose.yaml` -> `compose.yml` -> `docker-compose.yaml`
 * -> `docker-compose.yml`；本仓库用 `docker-compose.yml`。按顺序取第一个存在的，
 * 全都不存在时退回列表首项（后续 `config` 会明确失败，而不是静默跳过）。
 */
function pickBaseCompose(composeFiles) {
  const preferred = ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']
  return preferred.find((name) => composeFiles.includes(name)) ?? composeFiles[0] ?? 'docker-compose.yml'
}

/** H3：docker 可用时，compose 文件非法必须失败，不能记成"跳过"。 */
function composeConfig(root, file, baseCompose) {
  const isBase = file === baseCompose
  const args = isBase
    ? ['compose', '-f', file, 'config', '--quiet']
    : ['compose', '-f', baseCompose, '-f', file, 'config', '--quiet']
  try {
    execFileSync('docker', args, {
      cwd: root,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, ...CONFIG_ONLY_ENV },
      timeout: 60000,
    })
    return { ok: true, detail: 'docker compose config 通过' }
  } catch (error) {
    const stderr = String(error.stderr ?? error.message ?? '').trim()
    const firstLines = stderr.split(/\r?\n/).filter(Boolean).slice(0, 3).join(' | ')
    return { ok: false, detail: `docker compose 拒绝该文件（exit=${error.status ?? '?'}）: ${firstLines}` }
  }
}

function composeServices(root, file) {
  try {
    const out = execFileSync('docker', ['compose', '-f', file, 'config', '--services'], {
      cwd: root,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, ...CONFIG_ONLY_ENV },
      timeout: 60000,
    })
    return { names: out.split(/\r?\n/).map((s) => s.trim()).filter(Boolean), error: null }
  } catch (error) {
    const stderr = String(error.stderr ?? error.message ?? '').trim()
    return { names: [], error: stderr.split(/\r?\n/).filter(Boolean).slice(0, 2).join(' | ') }
  }
}

// ------------------------------------------------------------ 正常检查输出
function report(root) {
  const { passes, failures } = runChecks(root)
  if (process.argv.includes('--json')) {
    console.log(JSON.stringify({
      check: 'single-web-scope',
      passed: failures.length === 0,
      checks: [...passes, ...failures.map((f) => ({ ...f, failed: true }))],
      failureCount: failures.length,
    }, null, 2))
  } else {
    for (const p of passes) console.log(`  OK   ${p.name}${p.detail ? ` — ${p.detail}` : ''}`)
    for (const f of failures) console.error(`  FAIL ${f.name}${f.detail ? ` — ${f.detail}` : ''}`)
    console.log(
      failures.length === 0
        ? `\n单端范围检查通过（${passes.length} 项）`
        : `\n单端范围检查失败：${failures.length} 项`,
    )
  }
  process.exit(failures.length === 0 ? 0 : 1)
}

// -------------------------------------------------------------- H4 self-test
const SELFTEST_DIR = resolve(DEFAULT_ROOT, '.cache/tmp/scope-selftest')

function copyInto(dest, rel) {
  const from = join(DEFAULT_ROOT, rel)
  if (!existsSync(from)) return
  const to = join(dest, rel)
  mkdirSync(dirname(to), { recursive: true })
  copyFileSync(from, to)
}

function prepareCopy(name) {
  const dir = join(SELFTEST_DIR, name)
  rmSync(dir, { recursive: true, force: true })
  mkdirSync(dir, { recursive: true })
  for (const file of readdirSync(DEFAULT_ROOT)) {
    if (COMPOSE_PATTERN.test(file) && statSync(join(DEFAULT_ROOT, file)).isFile()) copyInto(dir, file)
  }
  for (const rel of [
    'pnpm-workspace.yaml',
    'package.json',
    'backend/src/main/resources/application.yml',
    'ai-service/app/core/config.py',
  ]) copyInto(dir, rel)
  for (const pkg of listFrontendPackages(DEFAULT_ROOT)) copyInto(dir, `${pkg}/package.json`)
  return dir
}

function appendText(rel, text) {
  return (dir) => {
    const path = join(dir, rel)
    const current = existsSync(path) ? readFileSync(path, 'utf8') : ''
    const separator = current.endsWith('\n') || current === '' ? '' : '\n'
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, `${current}${separator}${text}`, 'utf8')
  }
}

const MUTATIONS = [
  {
    id: 'baseline',
    label: '未改动副本（基线）',
    expect: 'pass',
    apply: () => {},
  },
  {
    id: 'compose-service-acceptance',
    label: 'H1: docker-compose.acceptance.yml 增加 student-web service',
    expect: 'fail',
    apply: appendText('docker-compose.acceptance.yml', '\n  student-web:\n    image: nginx:alpine\n'),
  },
  {
    id: 'compose-new-file',
    label: 'H1: 新增 docker-compose.staging.yml（内含 student-web），证明是 glob 而非白名单',
    expect: 'fail',
    apply: (dir) => {
      writeFileSync(join(dir, 'docker-compose.staging.yml'),
        'services:\n  student-web:\n    image: nginx:alpine\n')
    },
  },
  {
    id: 'compose-altname',
    label: 'H1: 新增 compose.override.yml（Compose 的另一种文件名）内含 student-web',
    expect: 'fail',
    apply: (dir) => {
      writeFileSync(join(dir, 'compose.override.yml'),
        'services:\n  student-web:\n    image: nginx:alpine\n')
    },
  },
  {
    id: 'workspace-glob',
    label: 'H2: workspace 成员改成通配符 frontend/apps/*',
    expect: 'fail',
    apply: (dir) => {
      const path = join(dir, 'pnpm-workspace.yaml')
      const text = readFileSync(path, 'utf8')
      writeFileSync(path, text.replace(/"frontend\/apps\/teacher-web"/, '"frontend/apps/*"'))
    },
  },
  {
    id: 'workspace-literal',
    label: 'H2: workspace 成员直接写回 frontend/apps/student-web',
    expect: 'fail',
    apply: (dir) => {
      const path = join(dir, 'pnpm-workspace.yaml')
      const text = readFileSync(path, 'utf8')
      // 必须插进 `packages:` 列表内部；追加到文件末尾会落到 allowBuilds 之下，
      // 那不是 workspace 成员，门禁理应（且确实）不把它当作违规。
      writeFileSync(path, text.replace(
        /(^\s*-\s*"frontend\/apps\/teacher-web"[^\n]*\n)/m,
        '$1  - "frontend/apps/student-web"\n',
      ))
    },
  },
  {
    id: 'package-json-workspaces',
    label: 'H2: package.json 的 workspaces 字段改成 frontend/apps/*（绕过 pnpm-workspace.yaml）',
    expect: 'fail',
    apply: (dir) => {
      const path = join(dir, 'package.json')
      const pkg = JSON.parse(readFileSync(path, 'utf8'))
      pkg.workspaces = ['frontend/apps/*']
      writeFileSync(path, `${JSON.stringify(pkg, null, 2)}\n`, 'utf8')
    },
  },
  {
    id: 'cors-port',
    label: 'H0: backend CORS 默认值放行学生端端口 3001',
    expect: 'fail',
    apply: (dir) => {
      const path = join(dir, 'backend/src/main/resources/application.yml')
      const text = readFileSync(path, 'utf8')
      writeFileSync(path, text.replace(/(allowed-origins:\s*\S+)/, '$1,http://localhost:3001'))
    },
  },
  {
    id: 'broken-compose-yaml',
    label: 'H3: compose 文件 YAML 损坏（需 docker 可用）',
    expect: 'fail',
    requiresDocker: true,
    apply: (dir) => {
      writeFileSync(join(dir, 'docker-compose.cpu.yml'),
        'services:\n  ai-service:\n    environment:\n      - MACT: "unterminated\n')
    },
  },
  {
    id: 'empty-services',
    label: 'H3: compose 文件 services 段为空',
    expect: 'fail',
    apply: (dir) => {
      writeFileSync(join(dir, 'docker-compose.cpu.yml'), '# truncated\nservices:\n')
    },
  },
]

function selfTest() {
  mkdirSync(SELFTEST_DIR, { recursive: true })
  const dockerUsable = dockerInfoOk()
  const rows = []
  let bad = 0

  for (const mutation of MUTATIONS) {
    if (mutation.requiresDocker && !dockerUsable) {
      rows.push({ id: mutation.id, status: 'SKIP', detail: 'docker info 不可用，该变异无法验证（需 docker）' })
      continue
    }
    const dir = prepareCopy(mutation.id)
    mutation.apply(dir)
    const { passes, failures } = runChecks(dir)
    if (mutation.expect === 'pass') {
      const ok = failures.length === 0
      if (!ok) bad += 1
      rows.push({
        id: mutation.id,
        status: ok ? 'OK' : 'BAD',
        detail: ok ? `基线通过（${passes.length} 项）` : `基线本应通过，却失败: ${failures.map((f) => f.name).join('; ')}`,
      })
    } else {
      const caught = failures.length > 0
      if (!caught) bad += 1
      rows.push({
        id: mutation.id,
        status: caught ? 'OK' : 'BAD',
        detail: caught
          ? `已拒绝（${failures.length} 项失败）: ${failures.map((f) => f.name).slice(0, 2).join('; ')}`
          : `!! 未被拒绝（${passes.length} 项通过）—— 这是 fail-open`,
      })
    }
  }

  console.log('\n单端范围门禁自测（变异必须全部被拒）')
  console.log(`  临时目录: ${SELFTEST_DIR}`)
  for (const row of rows) {
    const mutation = MUTATIONS.find((m) => m.id === row.id)
    console.log(`  ${row.status.padEnd(4)} ${row.id.padEnd(28)} ${row.detail}`)
    console.log(`       ${mutation?.label ?? ''}`)
  }
  const skipped = rows.filter((r) => r.status === 'SKIP').length
  console.log(
    bad === 0
      ? `\n自测通过：${rows.length - skipped} 个变异全部按预期处理${skipped > 0 ? `，${skipped} 个因 docker 不可用跳过` : ''}`
      : `\n自测失败：${bad} 个变异行为不符合预期`,
  )
  rmSync(SELFTEST_DIR, { recursive: true, force: true })
  process.exit(bad === 0 ? 0 : 1)
}

// ---------------------------------------------------------------------- 入口
if (process.argv.includes('--self-test')) {
  selfTest()
} else {
  report(argValue('--root') ? resolve(argValue('--root')) : DEFAULT_ROOT)
}
