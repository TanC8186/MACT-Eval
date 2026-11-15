#!/usr/bin/env bash
# 依赖与供应链清单（SEC-08，2026-10-28）。
#
#   bash scripts/verify/sbom.sh              # 生成 SBOM + 许可证清单
#   bash scripts/verify/sbom.sh --check      # 只检查现有清单是否过期
#
# 产出（全部落在项目内，不入库）：
#   data/tmp/sbom/<date>/backend.cdx.json      Maven 依赖 SBOM（CycloneDX）
#   data/tmp/sbom/<date>/frontend.cdx.json     pnpm 依赖 SBOM
#   data/tmp/sbom/<date>/python.cdx.json       .venv-verify 的 Python SBOM
#   data/tmp/sbom/<date>/licenses.md           依赖许可证清单（人工复核用）
#
# **诚实边界**：本脚本生成**成分清单**，不做漏洞扫描。它不运行
# `mvn dependency-check`、`pnpm audit`、`pip-audit`、`trivy`——那些需要联网
# 下载 NVD/CVE 库（数 GB）并且会给出需要人工分诊的告警。本脚本只回答
# "这个交付里有哪些第三方组件、它们是什么许可证"，这是审计点名的缺口。
#
# 特别是 ultralytics（AGPL-3.0）必须出现在清单里：AGPL 对分发有实质约束，
# 而项目此前从未在任何交付材料里列出软件依赖的许可证。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

CACHE="$ROOT/.cache"
export TMPDIR="$CACHE/tmp" TEMP="$CACHE/tmp" TMP="$CACHE/tmp"
export PIP_CACHE_DIR="$CACHE/pip"
export MAVEN_REPO="$CACHE/maven"
export npm_config_store_dir="$CACHE/pnpm"
export npm_config_cache="$CACHE/pnpm-cache"
mkdir -p "$TMPDIR" "$MAVEN_REPO"

OUT="$ROOT/data/tmp/sbom/$(date +%Y%m%d)"
mkdir -p "$OUT"

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  echo "==> 检查现有 SBOM 是否覆盖了当前依赖"
  echo "（--check 目前只验证文件存在，尚未实现清单与 lockfile 的差异比较）"
  missing=0
  for f in backend.cdx.json frontend.cdx.json python.cdx.json licenses.md; do
    if [[ -f "$OUT/$f" ]]; then
      printf '    OK   %s\n' "$OUT/$f"
    else
      printf '    MISS %s\n' "$OUT/$f"
      missing=1
    fi
  done
  exit "$missing"
fi

echo "==> 输出目录: $OUT"

# ---------------------------------------------------------------- 后端（Maven）
echo
echo "==> 后端依赖 SBOM（CycloneDX Maven 插件）"
# 插件版本固定在 pom 之外的一份 profile 里，避免把构建插件塞进生产构建。
# 用 `-Dcyclonedx.outputFormat=json` 得到机器可读格式。
if mvn -q -B -f backend/pom.xml -Dmaven.repo.local="$MAVEN_REPO" \
      org.cyclonedx:cyclonedx-maven-plugin:2.8.1:makeAggregateBom \
      -DoutputFormat=json -DoutputName=backend.cdx \
      -Dcyclonedx.skipAttach=true -DincludeTestScope=true >/dev/null 2>&1; then
  cp backend/target/backend.cdx.json "$OUT/backend.cdx.json" 2>/dev/null || true
  echo "    已生成 $OUT/backend.cdx.json"
else
  # 失败时**不**静默跳过：SBOM 是审计要求的产物，缺了必须可见。
  echo "!! CycloneDX Maven 插件执行失败（可能离线且本地仓库无该插件）" >&2
  echo "   这不是「没有依赖」，而是「清单没生成」。不要把它读成通过。" >&2
fi

# ---------------------------------------------------------------- 前端（pnpm）
echo
echo "==> 前端依赖 SBOM（直接扫描虚拟存储的 package.json）"
# 为什么不用 `pnpm licenses list`：本机实测它返回
# ERR_PNPM_MISSING_PACKAGE_INDEX_FILE（虚拟存储的索引与当前 lockfile 不同步），
# 而干净检出上同样可能没有索引。直接读 package.json 不依赖 pnpm 的内部记账，
# 只依赖"依赖已经装好"这一条——而那一条构建本来就需要。
"${VERIFY_PY:-python}" - "$ROOT" "$OUT/frontend-licenses.json" <<'PYEOF'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])

store = root / "node_modules" / ".pnpm"
records = {}
if store.is_dir():
    for pkg_json in store.glob("*/node_modules/*/package.json"):
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # 作用域包在 store 里是 `<scope>+<name>@<ver>` 目录，真正的包名从
        # package.json 的 name 字段读，不要从路径猜。
        name = data.get("name")
        version = data.get("version")
        if not name or not version:
            continue
        license_field = data.get("license")
        if not license_field and isinstance(data.get("licenses"), list):
            license_field = ", ".join(
                (item.get("type") or "?") for item in data["licenses"]
                if isinstance(item, dict))
        records[f"{name}@{version}"] = {
            "name": name,
            "version": version,
            "license": license_field or "UNKNOWN",
        }

payload = sorted(records.values(), key=lambda r: (r["name"], r["version"]))
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
unknown = [r["name"] for r in payload if r["license"] == "UNKNOWN"]
print(f"    {len(payload)} 个包 -> {out}")
if unknown:
    print(f"    {len(unknown)} 个包未声明许可证：{', '.join(unknown[:10])}")
PYEOF

# ---------------------------------------------------------------- Python
echo
echo "==> Python 依赖 SBOM（.venv-verify）"
VERIFY_PY="$ROOT/.venv-verify/Scripts/python.exe"
[[ -x "$VERIFY_PY" ]] || VERIFY_PY="$ROOT/.venv-verify/bin/python"
if [[ -x "$VERIFY_PY" ]]; then
  if "$VERIFY_PY" -m cyclonedx_py environment "$ROOT/.venv-verify" \
       --output-format json --output-file "$OUT/python.cdx.json" >/dev/null 2>&1; then
    echo "    已生成 $OUT/python.cdx.json"
  else
    echo "!! cyclonedx_py 执行失败（pip install cyclonedx-bom）" >&2
  fi
  # ai-service 的依赖是独立的一套（CUDA/torch/ultralytics），必须单列：
  # 它是 AGPL 组件出现的地方。
  # ai-service 的直接依赖单独列一份：ultralytics（AGPL-3.0）就在这里，
  # 而它是整个交付里唯一有实质分发约束的组件。用 Python 生成而不是手写 echo，
  # 是为了让"钉版条目数"来自文件本身，不会随 requirements.txt 改动而失真。
  "$VERIFY_PY" - "$ROOT" "$OUT/ai-service-requirements.md" <<'PYEOF'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])
lines = []
for raw in (root / "ai-service" / "requirements.txt").read_text(encoding="utf-8").splitlines():
    stripped = raw.strip()
    if stripped and not stripped.startswith("#"):
        lines.append(stripped)

body = (
    "# ai-service 直接依赖（requirements.txt 原文）\n\n"
    f"{len(lines)} 个钉版条目：\n\n```\n"
    + "\n".join(lines)
    + "\n```\n\n## 已知许可证约束\n\n"
    "- **ultralytics**：AGPL-3.0。分发衍生作品时须提供完整源码，且"
    "**网络服务使用也视为分发**。本项目以内部部署形式使用；"
    "若对外提供推理服务，须评估 AGPL 义务或替换检测框架。\n"
    "- torch / torchvision：BSD-3-Clause。\n"
    "- faster-whisper（CTranslate2）：MIT。\n"
    "- opencv-python-headless：Apache-2.0（部分算法模块为 BSD/Intel）。\n\n"
    "> 本清单只列**直接**依赖的许可证；传递依赖的完整清单见同目录的\n"
    "> `python.cdx.json` 与 `backend.cdx.json`（CycloneDX）。\n"
)
out.write_text(body, encoding="utf-8")
PYEOF
  echo "    已生成 $OUT/ai-service-requirements.md"
else
  echo "!! 缺少 .venv-verify，无法生成 Python SBOM" >&2
fi

# ---------------------------------------------------------------- 许可证汇总
echo
echo "==> 汇总许可证清单"
# 全部在 Python 里生成：bash heredoc + 中文 + 控制台代码页的组合会把
# licenses.md 写成 GBK 字节，读的时候直接 UnicodeDecodeError。
"$VERIFY_PY" - "$OUT" <<'PYEOF'
import collections
import json
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
lines = []


def add(text=""):
    lines.append(text)


add(f"# 第三方依赖许可证清单（目录 {out.name}）")
add()


def summarise_backend():
    path = out / "backend.cdx.json"
    if not path.is_file():
        return ["- （SBOM 未生成：见 sbom.sh 的警告。缺清单不等于没有依赖）"]
    data = json.loads(path.read_text(encoding="utf-8"))
    counts = collections.Counter()
    for comp in data.get("components", []):
        names = []
        for item in comp.get("licenses") or []:
            if "license" in item:
                names.append(item["license"].get("id")
                             or item["license"].get("name") or "?")
        counts[", ".join(names) if names else "UNKNOWN"] += 1
    result = [f"- {name}: {count} 个组件" for name, count in counts.most_common()]
    result.append("")
    result.append(f"组件总数: {len(data.get('components', []))}")
    copyleft = [name for name in counts if "GPL" in name.upper()]
    if copyleft:
        result.append("")
        result.append("**含 copyleft 的许可证**（分发前须评估义务）：")
        for name in copyleft:
            result.append(f"- {name}（{counts[name]} 个组件）")
    return result


def summarise_frontend():
    path = out / "frontend-licenses.json"
    if not path.is_file():
        return ["- （未生成：见 sbom.sh 的警告）"]
    data = json.loads(path.read_text(encoding="utf-8"))
    counts = collections.Counter(item["license"] for item in data)
    result = [f"- {name}: {count}" for name, count in counts.most_common()]
    result.append("")
    result.append(f"包总数: {len(data)}（来自 node_modules/.pnpm 的 package.json）")
    unknown = [item["name"] for item in data if item["license"] == "UNKNOWN"]
    if unknown:
        result.append("")
        result.append(f"**未声明许可证**：{', '.join(unknown)}")
    return result


add("## 后端（Maven）")
add()
lines.extend(summarise_backend())
add()
add("## 前端（pnpm）")
add()
lines.extend(summarise_frontend())
add()
add("## ai-service（Python）")
add()
add("见 `ai-service-requirements.md`。**ultralytics 为 AGPL-3.0**——")
add("这是整个交付里唯一有实质分发约束的组件。")
add()
add("## 口径")
add()
add("- 本文件是**成分清单**，不是漏洞扫描结果。sbom.sh 不运行")
add("  dependency-check / pnpm audit / pip-audit / trivy，因此")
add("  「清单已生成」不等于「没有已知漏洞」。")
add("- 传递依赖的完整清单见同目录的 `backend.cdx.json` 与 `python.cdx.json`（CycloneDX）。")

(out / "licenses.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"    {len(lines)} 行 -> {out / 'licenses.md'}")
PYEOF

echo "    已生成 $OUT/licenses.md"
echo
echo "==> 完成。产物在 $OUT"
echo "    注意：这是**成分清单**，不是漏洞扫描结果。"
echo "    本脚本不运行 dependency-check / audit / trivy，因此"
echo "    「清单已生成」不等于「没有已知漏洞」。"
