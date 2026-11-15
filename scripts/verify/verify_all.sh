#!/usr/bin/env bash
# 一键验证入口（OPS06）。本文件是"能不能发布"的唯一机器门禁。
#
#   bash scripts/verify/verify_all.sh
#
# 设计原则：**宁可红，不可假绿**。
#   * 缺依赖 -> 失败，并打印修复命令，不静默跳过；
#   * 需要 Docker 的检查在 docker 不可用时失败；确需在无 Docker 机器上跑，
#     显式设置 SKIP_DOCKER=1，此时末尾摘要会把它记为"未执行"且返回 3；
#   * 任何一步失败立即退出（set -e），不做"后面还有更重要的检查"式继续。
#
# 环境变量：
#   SKIP_DOCKER=1      跳过需要 docker 的检查（会返回 3 = 覆盖不完整）
#   RUN_PROD_SMOKE=1   额外启动打包后的 backend（prod profile）做启动冒烟
#   MACT_VERIFY_JSON=路径  额外把结果摘要写成 JSON
#   MACT_GATE_TIER=full|fast   门禁档位（默认 full）
#       full —— 本机/发布前完整门禁：含浏览器验收环境自检。
#       fast —— PR 快速门禁：**不包含**浏览器 E2E 与任何镜像构建。
#       fast 档位不把被排除的步骤记成 skipped（那会让退出码变成 3，
#       把"本档位没打算跑"与"该跑没跑成"混为一谈）。被排除的步骤
#       会在摘要里逐条列出，读者能看见覆盖边界在哪里。
#   VERIFY_PYTHON=路径 指定 .venv-verify 的解释器（默认自动探测）
#
# 磁盘约束：所有缓存与临时目录都指向 E:/Personal_Design 下的 .cache/，
# 不写 C 盘、不写系统 TEMP、不使用默认 Docker volume。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

TIER="${MACT_GATE_TIER:-full}"
case "$TIER" in
  full|fast) ;;
  *) echo "!! MACT_GATE_TIER 只支持 full 或 fast（收到 ${TIER}）" >&2; exit 2 ;;
esac

CACHE="$ROOT/.cache"

# 全部**强制**指向 E 盘，不用 "${VAR:-default}"。
# 原因：本机 TEMP/TMP 已被系统设为 C:\Users\...\Temp，而 ":-" 只在变量为空时生效，
# 于是"临时文件不落 C 盘"这条承诺实际是假的。这里直接覆盖，并在下面打印解析结果。
export PIP_CACHE_DIR="$CACHE/pip"
export PLAYWRIGHT_BROWSERS_PATH="$CACHE/ms-playwright"
export TMPDIR="$CACHE/tmp"
export TEMP="$CACHE/tmp"
export TMP="$CACHE/tmp"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$CACHE/tmp/pycache"
# CI=true：让 pnpm 在非交互环境里不因为 "modules dir needs re-creation"
# 而中止（ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY）。本脚本本身就是
# 非交互的，缺这个变量时 typecheck 会随机因为 node_modules 的 storeDir
# 与当前 store 不一致而失败——那是环境问题，不是代码问题。
export CI=true
# 以下四个变量不设就会默认写回 C 盘，且此前没有任何一处检查会因此变红
# （check_disk_policy 对未设置的变量只记 skip）。这是"承诺不落 C 盘"与
# "实际会落 C 盘"之间的缝，必须在这里堵住：
#   MPLCONFIGDIR   —— matplotlib 的字体缓存，capacity_common 画图时写
#   HF_HOME        —— model 权重缓存（faster-whisper / ultralytics）
#   TORCH_HOME     —— torch hub 下载的扩展与权重
#   PNPM_*         —— pnpm 的 store/cache/state，与 store-dir 是两回事
export MPLCONFIGDIR="$CACHE/tmp/mpl"
export HF_HOME="$CACHE/huggingface"
export HUGGINGFACE_HUB_CACHE="$CACHE/huggingface/hub"
export TORCH_HOME="$CACHE/torch"
export PNPM_CONFIG_STORE_DIR="$CACHE/pnpm"
export PNPM_CONFIG_CACHE_DIR="$CACHE/pnpm-cache"
export PNPM_CONFIG_STATE_DIR="$CACHE/pnpm-state"
# Maven 本地仓库与 pnpm store 也必须落在项目内，否则每次构建都往 C 盘写。
MAVEN_REPO="$CACHE/maven"
PNPM_STORE="$CACHE/pnpm"
export MAVEN_OPTS="${MAVEN_OPTS:-} -Dmaven.repo.local=$MAVEN_REPO"
# PNPM_STORE 这个名字 pnpm **从不读取**。实测：设了它之后 `pnpm store path`
# 仍返回 E:/.pnpm-store/v11（另一个项目的目录，不在本项目内）。
# 真正生效的两处是 pnpm-workspace.yaml 的 storeDir 与命令行 --store-dir；
# 这里再补一层 npm_config_store_dir（pnpm 认 npm_config_* 形式的配置）。
export npm_config_store_dir="$PNPM_STORE"
mkdir -p "$PIP_CACHE_DIR" "$PLAYWRIGHT_BROWSERS_PATH" "$TMPDIR" "$MAVEN_REPO"          "$PNPM_STORE" "$PNPM_CONFIG_CACHE_DIR" "$PNPM_CONFIG_STATE_DIR"          "$MPLCONFIGDIR" "$HF_HOME" "$TORCH_HOME"

# 验证 venv（P0-1.7）：三个纯 Python 套件必须用**固定**解释器跑。
# 用 PATH 上的 python 意味着结论取决于某台机器恰好装了什么；
# 本机的 python 还是 E:/anaconda3，既不在项目内，也会把 pip cache 写回原处。
VERIFY_PY="${VERIFY_PYTHON:-}"
if [[ -z "$VERIFY_PY" ]]; then
  if [[ -x "$ROOT/.venv-verify/Scripts/python.exe" ]]; then
    VERIFY_PY="$ROOT/.venv-verify/Scripts/python.exe"
  elif [[ -x "$ROOT/.venv-verify/bin/python" ]]; then
    VERIFY_PY="$ROOT/.venv-verify/bin/python"
  fi
fi
export VERIFY_PYTHON="$VERIFY_PY"

echo "==> 输出目录（全部必须在 $ROOT 下）"
for dir in "$PIP_CACHE_DIR" "$PLAYWRIGHT_BROWSERS_PATH" "$TMPDIR" "$MAVEN_REPO"            "$PNPM_STORE" "$PNPM_CONFIG_CACHE_DIR" "$PNPM_CONFIG_STATE_DIR"            "$MPLCONFIGDIR" "$HF_HOME" "$TORCH_HOME"; do
  case "$dir" in
    "$ROOT"/*) printf '    OK   %s\n' "$dir" ;;
    *) printf '    FAIL %s 不在项目根内\n' "$dir" >&2; exit 1 ;;
  esac
done
if docker info >/dev/null 2>&1; then
  printf '    Docker Root Dir: %s\n' "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
fi
printf '    验证解释器: %s\n' "${VERIFY_PY:-<缺失>}"
printf '    pnpm store: %s\n' "$(pnpm store path 2>/dev/null || echo '<pnpm 不可用>')"

# 解释器必须存在且在项目内。前缀判断交给 Python：`sys.prefix` 是 Windows 写法
# （E:\...），而 $ROOT 是 MSYS 写法（/e/...），用 bash 的 case 比较永远不相等。
if [[ -z "$VERIFY_PY" || ! -x "$VERIFY_PY" ]]; then
  echo "!! 未找到验证 venv：$ROOT/.venv-verify" >&2
  echo "   修复：bash scripts/verify/setup_verify_env.sh" >&2
  exit 1
fi
# $ROOT 是 MSYS 写法（/e/...）。直接交给 Windows python 会被当成"当前盘符下的
# 相对路径"，解析成 E:\e\Personal_Design —— 项目内的 venv 反而被判成越界。
# 先用 cygpath 转成 Windows 路径（实测：不转就必然误报）。
ROOT_WIN="$ROOT"
if command -v cygpath >/dev/null 2>&1; then
  ROOT_WIN="$(cygpath -w "$ROOT")"
fi
if ! "$VERIFY_PY" -I -c "import pathlib,sys; p=pathlib.Path(sys.prefix).resolve(); r=pathlib.Path(sys.argv[1]).resolve(); sys.exit(0 if (p==r or r in p.parents) else 1)" "$ROOT_WIN"; then
  echo "!! 验证解释器不在项目根内：$VERIFY_PY（项目根 $ROOT_WIN）" >&2
  exit 1
fi

# 浏览器验收使用项目固定 venv（P0-3），不依赖开发机全局安装。
BROWSER_PY="${MACT_BROWSER_PYTHON:-$ROOT/.venv-browser/Scripts/python.exe}"
[[ -x "$BROWSER_PY" ]] || BROWSER_PY="$ROOT/.venv-browser/bin/python"

RESULT_FILE="${MACT_VERIFY_JSON:-}"
STEPS=()

record() { # record <status> <name> [detail]
  STEPS+=("$1|$2|${3:-}")
}

run_step() { # run_step <name> <cmd...>
  local name="$1"; shift
  echo
  echo "==> $name"
  local started
  started=$(date +%s)
  if "$@"; then
    record "pass" "$name" "$(( $(date +%s) - started ))s"
  else
    local code=$?
    record "fail" "$name" "exit=$code"
    echo "!! $name 失败（exit=$code）" >&2
    write_summary
    exit "$code"
  fi
}

docker_available() { docker info >/dev/null 2>&1; }

skip_or_fail() { # skip_or_fail <name> <why>
  if [[ "${SKIP_DOCKER:-0}" == "1" ]]; then
    echo "==> $1（SKIP_DOCKER=1，跳过：$2）"
    record "skipped" "$1" "$2"
    return 1
  fi
  echo "!! $1 需要 Docker，但当前不可用：$2" >&2
  echo "   修复：启动 Docker Desktop，或设置 SKIP_DOCKER=1 明确跳过（结果会标记覆盖不完整）。" >&2
  record "fail" "$1" "docker unavailable: $2"
  write_summary
  exit 1
}

write_summary() {
  local failed=0 skipped=0
  echo
  echo "================ 验证摘要 ================"
  for entry in "${STEPS[@]:-}"; do
    [[ -n "$entry" ]] || continue
    local status name detail
    status="${entry%%|*}"; entry="${entry#*|}"
    name="${entry%%|*}"; detail="${entry#*|}"
    case "$status" in
      pass)    printf '  PASS     %-46s %s\n' "$name" "$detail" ;;
      fail)    printf '  FAIL     %-46s %s\n' "$name" "$detail"; failed=$((failed+1)) ;;
      skipped) printf '  SKIPPED  %-46s %s\n' "$name" "$detail"; skipped=$((skipped+1)) ;;
    esac
  done
  echo "=========================================="
  echo "  缓存目录: pip=$PIP_CACHE_DIR"
  echo "            playwright=$PLAYWRIGHT_BROWSERS_PATH"
  echo "            tmp=$TMPDIR"
  echo "            mpl=$MPLCONFIGDIR"
  echo "            hf=$HF_HOME"
  echo "            pnpm=$PNPM_CONFIG_STORE_DIR"
  echo "  验证解释器: ${VERIFY_PY:-<缺失>}"
  if docker_available; then
    echo "  Docker Root Dir: $(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
  fi
  if [[ -n "$RESULT_FILE" ]]; then
    {
      printf '{\n  "steps": [\n'
      local first=1
      for entry in "${STEPS[@]:-}"; do
        [[ -n "$entry" ]] || continue
        local status name detail
        status="${entry%%|*}"; entry="${entry#*|}"
        name="${entry%%|*}"; detail="${entry#*|}"
        [[ $first -eq 1 ]] || printf ',\n'
        first=0
        printf '    {"name": "%s", "status": "%s", "detail": "%s"}' "$name" "$status" "$detail"
      done
      printf '\n  ],\n  "failed": %d,\n  "skipped": %d\n}\n' "$failed" "$skipped"
    } > "$RESULT_FILE"
    echo "  结果已写入 $RESULT_FILE"
  fi
  echo "  档位: $TIER"
  if [[ "$TIER" == "fast" ]]; then
    echo "  本档位不包含（不是失败，是范围之外）:"
    echo "    - browser e2e self-check（需要 .venv-browser + Chromium 685 MB）"
    echo "    - prod profile startup smoke（需要镜像构建）"
  fi
  if (( failed > 0 )); then
    echo "  RESULT: FAILED"
  elif (( skipped > 0 )); then
    echo "  RESULT: INCOMPLETE（$skipped 项未执行，不得当作通过）"
  else
    echo "  RESULT: PASSED"
  fi
}

# ---------------------------------------------------------------------------
# 0. 单端范围门禁（最快、最便宜，先跑）
# ---------------------------------------------------------------------------
run_step "单端范围检查（compose/workspace/CORS 不得含 student-web）" \
  node scripts/verify/check-single-web-scope.mjs

# ---------------------------------------------------------------------------
# 1. 后端
# ---------------------------------------------------------------------------
run_step "backend tests (mvn test)" \
  mvn -q -f backend/pom.xml -Dmaven.repo.local="$MAVEN_REPO" test

# ---------------------------------------------------------------------------
# 2. Python 工具链
# ---------------------------------------------------------------------------
# 三个纯 Python 套件一律走 $VERIFY_PY（.venv-verify），不再用 PATH 上的 python。
# 这是 P0-1.7 的实质：以前那几条裸 `python -m pytest` 用的是 E:/anaconda3，
# 换台机器就会静默换成另一套依赖版本，而门禁照样"绿"。
run_step "public-data tools tests" \
  "$VERIFY_PY" -m pytest tools/public_data/tests -q -p no:cacheprovider

run_step "ops tools tests（V33 迁移证明等）" \
  "$VERIFY_PY" -m pytest scripts/ops/tests -q -p no:cacheprovider

run_step "verify 脚本自身测试（E2E 状态机等）" \
  "$VERIFY_PY" -m pytest scripts/verify/tests -q -p no:cacheprovider

run_step "ai-service tests（隔离 venv）" \
  "$VERIFY_PY" scripts/verify/run_ai_tests.py

# ---------------------------------------------------------------------------
# 3. 教师端
# ---------------------------------------------------------------------------
run_step "teacher-web auth-session regression" \
  bash -c 'cd frontend/apps/teacher-web && node --experimental-strip-types --no-warnings tests/auth-session.test.mjs'

run_step "teacher-web evidence gallery regression" \
  node --experimental-strip-types --no-warnings frontend/apps/teacher-web/tests/evidence-gallery.test.mjs

run_step "teacher-web report-versions regression" \
  node --experimental-strip-types --no-warnings frontend/apps/teacher-web/tests/report-versions.test.mjs

# 单端范围门禁自身的回归测试：在 .cache/tmp 的副本上注入变异（compose 新增
# student-web、workspace 通配符、CORS 端口、YAML 损坏），断言每一种都被拒绝。
run_step "单端范围门禁自测（变异必须全部被拒）" \
  node scripts/verify/check-single-web-scope.mjs --self-test

# ---------------------------------------------------------------------------
# 2b. 安全契约（SEC-01 / SEC-02 / SEC-03，2026-10-28）
# ---------------------------------------------------------------------------
# 这些是**静态契约**检查，不需要 Docker：真实响应头要靠 curl 抓容器，
# 而本轮 Docker 数据根仍在 C 盘、按任务书必须阻塞 docker build/up。
# 静态检查不能证明运行时行为，但能守住"配置被改回去"这类回归；
# 运行时证据写在交付报告的未验证清单里，不在这里假装有。
run_step "安全响应头与 WS 凭据契约（含 17 个变异自测）" \
  "$VERIFY_PY" -I scripts/verify/test_security_headers.py --self-test

# 真实 nginx 解析 + 静态层响应头（B1）。用**本机** nginx 二进制，不跑容器：
#   * 能证明：配置语法正确、渲染后的指令合法、静态/404/502 都带齐安全头、
#     access.log 不含查询串。
#   * 不能证明：容器内路径、Dockerfile 的 COPY、nginx:alpine 的实际行为、
#     非 root 运行。后者需要 docker build（被磁盘门禁阻塞）。
# 之所以值得单列一步：只做文本契约检查发现不了"指令写在不允许的上下文"
# 这类只有解析器才知道的问题——本轮 `error_log /dev/null` 就是被真 nginx
# 报 emerg 才发现的。缺本机 nginx 时该步返回 2（环境缺失），不会假绿。
run_step "nginx 真实解析 + 静态层响应头（本机 nginx 二进制）" \
  bash scripts/verify/verify_nginx_config.sh

# 真浏览器 + 构建产物 + 本机 nginx（B1.4）：回答 curl 回答不了的问题——
# CSP 配置正确但页面白屏。检查 ECharts 是否渲染、证据帧图片是否真的解码、
# /api/ 与 /ws/ 经反代是否可达、全程是否零 CSP 违规。
# 需要后端 :8000 活着；不在时返回 2（环境缺失），不会假绿。
run_step "真浏览器 + nginx：CSP 不阻断功能（ECharts/图片/WS）" \
  bash scripts/verify/verify_nginx_browser_csp.sh

run_step "teacher-web typecheck" pnpm --config.storeDir="$PNPM_STORE" typecheck:web

run_step "teacher-web production build" pnpm --config.storeDir="$PNPM_STORE" build:web

# ---------------------------------------------------------------------------
# 4. 浏览器验收环境自检（P0-3）
# ---------------------------------------------------------------------------
if [[ "$TIER" == "fast" ]]; then
  echo
  echo "==> 浏览器验收环境 self-check（fast 档位不含此步，见摘要）"
  BROWSER_PY=""
fi
if [[ -n "$BROWSER_PY" ]]; then
  echo
  echo "==> 浏览器验收环境 self-check"
  if [[ ! -x "$BROWSER_PY" ]]; then
    echo "!! 未找到浏览器验收 venv：$ROOT/.venv-browser" >&2
    echo "   修复：bash scripts/verify/setup_browser_env.sh" >&2
    record "fail" "browser e2e self-check" "venv missing"
    write_summary
    exit 1
  fi
  run_step "browser e2e self-check（依赖/Chromium/证据目录/选择器契约）" \
    "$BROWSER_PY" scripts/verify/browser_evidence_e2e.py --self-check
fi

# ---------------------------------------------------------------------------
# 5. Docker 相关
# ---------------------------------------------------------------------------
# 磁盘路径门禁必须在任何 docker build/up 之前。它失败时不是"提醒一下"，
# 而是**禁止继续**：DockerRootDir 在 C 盘时，镜像层/命名卷/可写层全部写进
# C:\Users\...\Docker\wsl\disk\docker_data.vhdx，事后无法从 compose 修正。
# 这一步刻意**不**用 run_step：run_step 失败会立即退出，那样后面所有不依赖
# Docker 的检查（git diff、prod 冒烟之外的全部）都不会运行，读者拿到的唯一
# 信息就是"磁盘门禁红了"。而磁盘门禁红的原因是用户必须手工完成的事
# （Docker Desktop 数据盘迁移），与代码正确性无关——应当先把其余结论跑完，
# 再把 Docker 段标成阻塞、末尾返回非零。
DISK_GATE_OK=0
run_step_soft() { # run_step_soft <name> <cmd...>  —— 失败只记录，不退出
  local name="$1"; shift
  echo
  echo "==> $name"
  local started
  started=$(date +%s)
  # 必须用 `|| code=$?` 捕获：`if cmd; then ...; fi` 之后 `$?` 是**整个 if
  # 语句**的状态（无 else 时为 0），不是 cmd 的。第一版写成 `local code=$?`
  # 在 if 之后，于是失败被记成 "exit=0"，读的人会以为是门禁脚本自己出错。
  local code=0
  "$@" || code=$?
  if [[ "$code" -eq 0 ]]; then
    record "pass" "$name" "$(( $(date +%s) - started ))s"
    return 0
  fi
  record "fail" "$name" "exit=$code（磁盘门禁未通过，Docker 步骤将被阻塞）"
  echo "!! $name 失败（exit=$code）；Docker 构建/启动将被跳过" >&2
  return 1
}

if run_step_soft "磁盘路径门禁（项目内 bind mount / 缓存 / Docker 数据根）" \
     bash scripts/verify/check_disk_policy.sh --json "docs/reviews/evidence/disk-policy-$(date +%Y%m%dT%H%M).json"; then
  DISK_GATE_OK=1
fi
run_step "磁盘路径门禁自测（变异必须全部被拒）" \
  bash scripts/verify/check_disk_policy.sh --self-test

if [[ "$DISK_GATE_OK" -ne 1 ]]; then
  # 不是 skip：skip 会被算成"未执行"，而这里**执行了并且失败了**。
  # 单独记一条 blocked，并在末尾汇总里一起计入非零退出。
  echo
  echo "!! 磁盘路径门禁未通过：跳过所有 docker build/up/run（任务书要求）。"
  echo "   常见原因：Docker Desktop 的数据盘仍在 C 盘。修复方式见"
  echo "   docs/operations/runbook.md 的磁盘章节——需要用户手工迁移，脚本不代做。"
  record "fail" "docker 步骤（被磁盘门禁阻塞）" "disk policy gate failed"
elif docker_available; then
  run_step "docker compose config（正式）" \
    docker compose config --quiet
  # 容量叠加文件用 "${VAR:?}" 强制要求两个密钥，连 `config` 都会失败。
  # 这里注入**仅用于语法校验**的临时值（不启动任何容器，因此不会用于认证）。
  run_step "docker compose config（容量叠加）" \
    env MACT_JWT_SECRET=config-check-only-placeholder-0000000000 \
        MACT_SERVICE_TOKEN=config-check-only-placeholder-0000000000 \
    docker compose -f docker-compose.yml -f docker-compose.capacity-cpu.yml config --quiet
  run_step "docker fail-fast check" \
    "$VERIFY_PY" scripts/verify/docker_failfast_test.py
else
  skip_or_fail "docker compose config（正式）" "docker 不可用" || true
  skip_or_fail "docker compose config（容量叠加）" "docker 不可用" || true
  skip_or_fail "docker fail-fast check" "docker 不可用" || true
fi

# ---------------------------------------------------------------------------
# 6. 工作区整洁度
# ---------------------------------------------------------------------------
run_step "git diff --check（无空白错误）" git diff --check

# ---------------------------------------------------------------------------
# 7. 可选：prod 启动冒烟
# ---------------------------------------------------------------------------
if [[ "${RUN_PROD_SMOKE:-0}" == "1" && "$TIER" == "full" ]]; then
  if docker_available; then
    run_step "prod profile startup smoke" bash scripts/verify/prod_startup_smoke.sh
  else
    skip_or_fail "prod profile startup smoke" "docker 不可用" || true
  fi
fi

write_summary

# 有跳过项 -> 3（覆盖不完整），不能返回 0。
# 不用 `printf | grep -q`：pipefail 下 grep 提前退出会让管道返回 141，
# 有跳过也会走到下面的成功分支。改成先取计数再判断。
skip_count=$(printf '%s
' "${STEPS[@]:-}" | grep -c '^skipped|' || true)
# 失败计数必须自己数，不能靠 `set -e`：本脚本用 run_step_soft 有意让磁盘门禁
# 失败后继续跑（否则后面所有不需要 Docker 的结论都拿不到），于是"某一步失败"
# 不会让脚本提前退出。
#
# 这个漏洞本轮真实发生过：磁盘门禁与 docker 段都记了 FAIL、摘要打印
# RESULT: FAILED，脚本仍走到这里只检查 skip_count，最后打印
# "ALL VERIFICATION CHECKS PASSED" 并 exit 0。那是最坏的一种假绿——
# 摘要说红、退出码说绿，任何按退出码判断的 CI 或钩子都会放行。
fail_count=$(printf '%s
' "${STEPS[@]:-}" | grep -c '^fail|' || true)
if [[ "${fail_count:-0}" -gt 0 ]]; then
  echo "$fail_count 项失败：不得视为通过（详见上面的 FAIL 行）。" >&2
  exit 1
fi
if [[ "${skip_count:-0}" -gt 0 ]]; then
  echo "$skip_count 项未执行：覆盖不完整，不能视为通过。" >&2
  exit 3
fi
echo "ALL VERIFICATION CHECKS PASSED"
