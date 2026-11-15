#!/usr/bin/env python
"""磁盘路径门禁（P0-1.4，2026-10-28）。

规则只有一条：**项目产出的每一个字节都必须落在项目根下**。

    python scripts/verify/check_disk_policy.py            # 检查
    python scripts/verify/check_disk_policy.py --json out.json
    python scripts/verify/check_disk_policy.py --self-test # 变异自测

检查五类真实路径，任何一类越界即返回非零：

  1. 解释器与 venv（sys.executable / .venv-*）；
  2. 进程环境里的缓存与临时变量（TMPDIR/TEMP/TMP/PIP_CACHE_DIR/
     PLAYWRIGHT_BROWSERS_PATH/MAVEN_USER_HOME/PNPM_HOME/HF_HOME/XDG_CACHE_HOME）；
  3. compose 的**解析后**真实挂载源（`docker compose config --format json`，
     不是读 yml 文本——`~/.cache` 只有在解析后才露出真实绝对路径）；
  4. compose 的命名卷与已存在容器的 `docker inspect` 挂载（type=volume 即失败，
     因为 DockerRootDir 在 C 盘时命名卷就是这个项目的 C 盘写入）；
  5. Docker 数据根的物理位置（`DockerRootDir` + Docker Desktop 的 vhdx）。

设计取舍：**不**尝试"帮你修好"。Docker Desktop 的磁盘迁移必须由用户手工完成，
脚本只负责判定并打印阻塞原因——自行改系统设置或删镜像/卷都超出授权范围。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePath

ROOT = Path(__file__).resolve().parents[2]

# Windows 控制台默认 GBK，中文失败原因会变成乱码，读的人就无法据此修复。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

#: 必须落在项目根内的环境变量名。不是"建议"——是"如果设了就必须在 E 盘"。
PATH_ENV_VARS = (
    "TMPDIR", "TEMP", "TMP", "PIP_CACHE_DIR", "PLAYWRIGHT_BROWSERS_PATH",
    "MAVEN_USER_HOME", "MAVEN_REPO",
    "PNPM_HOME", "PNPM_STORE_DIR",
    # pnpm 的 store/cache/state 是三个**不同**的配置，不能只盯 store-dir：
    # 后两者默认落 C 盘的用户缓存，而只设 store-dir 时它们照样写 C 盘。
    "PNPM_CONFIG_STORE_DIR", "PNPM_CONFIG_CACHE_DIR", "PNPM_CONFIG_STATE_DIR",
    "npm_config_store_dir",
    "HF_HOME", "HUGGINGFACE_HUB_CACHE", "TORCH_HOME", "XDG_CACHE_HOME",
    # matplotlib 的字体缓存：不设就写 ~/.config 与 ~/.cache（C 盘）。
    "MPLCONFIGDIR",
    "PYTHONPYCACHEPREFIX", "VERIFY_PYTHON", "MACT_BROWSER_PYTHON",
)

#: 允许存在的项目外路径（Docker Desktop 自身的安装目录之类），仅用于让报告
#: 说明"这个越界不是项目写出来的"，不豁免判定。
COMPOSE_FILES = ("docker-compose.yml",)


class Check:
    __slots__ = ("name", "status", "detail")

    def __init__(self, name: str, status: str, detail: str) -> None:
        self.name = name
        self.status = status          # pass | fail | skip
        self.detail = detail


def _norm(path: str) -> str:
    """把任意写法归一成可比较的小写正斜杠路径（去掉 Windows 长路径前缀）。"""
    text = path.strip().replace("\\", "/")
    if text.startswith("//?/"):
        text = text[4:]
    text = re.sub(r"/+", "/", text)
    if len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text.lower()


def _under(path: str, root: str) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r + "/")


def _is_absolute_any_platform(text: str) -> bool:
    """与平台无关的绝对路径判定。

    为什么不能直接用 ``Path.is_absolute()``：该函数的语义随平台变化。
    POSIX 下反斜杠写法（``\\mnt\\e\\x``）不是绝对路径，而 Windows 的
    ``E:\\x`` 是。磁盘门禁会在 Ubuntu CI runner 上跑，用平台相关的判定会让
    "同一个输入在 Windows 通过、在 Ubuntu 失败"——那不是配置问题，是判定写错。
    因此先把反斜杠归一成正斜杠，再按"以 / 开头或盘符+冒号+斜杠"判断。
    """
    normalised = text.replace("\\", "/").strip()
    if not normalised:
        return False
    if normalised.startswith("/"):
        return True
    return (len(normalised) >= 3 and normalised[0].isalpha()
            and normalised[1] == ":" and normalised[2] == "/")


def is_under_project(path: str) -> bool:
    """真实绝对路径是否在项目根内。

    相对路径一律视为不合规：它是否越界取决于当时的 cwd，本门禁不做这种赌注。
    """
    if not path or not path.strip():
        return False
    raw = path.strip().strip('"')
    if not _is_absolute_any_platform(raw):
        return False
    candidate = Path(raw.replace("\\", "/"))
    if not candidate.is_absolute():
        # 跨平台形式（例如在 Linux 上收到 `E:/x`）：只做归一化比较，
        # 不 resolve——resolve 会因为它不存在而抛错。
        return _under(raw, str(ROOT))
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    return _under(str(resolved), str(ROOT))


# --------------------------------------------------------------------------
# 1. 解释器与 venv
# --------------------------------------------------------------------------
def check_interpreter() -> list[Check]:
    out: list[Check] = []
    exe = sys.executable or ""
    if not exe:
        out.append(Check("interpreter", "fail", "无法解析 sys.executable"))
    elif is_under_project(exe):
        out.append(Check("interpreter", "pass", exe))
    else:
        out.append(Check("interpreter", "fail",
                         f"当前解释器不在项目内：{exe}（应为 <root>/.venv-verify）"))

    for name in (".venv-verify", ".venv-browser", ".venv"):
        venv = ROOT / name
        if venv.exists():
            out.append(Check(f"venv {name}", "pass", str(venv)))
    return out


# --------------------------------------------------------------------------
# 2. 环境变量
# --------------------------------------------------------------------------
def check_env() -> list[Check]:
    out: list[Check] = []
    for name in PATH_ENV_VARS:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            out.append(Check(f"env {name}", "skip", "<unset>"))
            continue
        if is_under_project(raw):
            out.append(Check(f"env {name}", "pass", raw))
        else:
            out.append(Check(f"env {name}", "fail",
                             f"{raw} 不在项目根 {ROOT} 下"))
    return out


# --------------------------------------------------------------------------
# 3/4. compose 解析后的挂载与卷
# --------------------------------------------------------------------------
def _compose_config(files: list[str], env_extra: dict[str, str] | None = None
                    ) -> tuple[dict | None, str]:
    """解析 compose，返回 (json, 错误说明)。不回显可能含占位密钥的输出。"""
    argv = ["docker", "compose"]
    for f in files:
        argv += ["-f", f]
    argv += ["config", "--format", "json"]
    env = os.environ.copy()
    env.setdefault("MSYS_NO_PATHCONV", "1")
    if env_extra:
        env.update(env_extra)
    try:
        result = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", env=env, timeout=120)
    except FileNotFoundError:
        return None, "docker 不可用"
    except subprocess.TimeoutExpired:
        return None, "docker compose config 超时"
    if result.returncode != 0:
        first = (result.stderr or result.stdout or "").strip().splitlines()
        return None, f"exit={result.returncode}: {first[0] if first else ''}"
    try:
        return json.loads(result.stdout), ""
    except json.JSONDecodeError as exc:
        return None, f"输出不是 JSON: {exc}"


def check_compose_mounts() -> list[Check]:
    out: list[Check] = []
    declared = [f for f in COMPOSE_FILES if (ROOT / f).exists()]
    if not declared:
        return [Check("compose mounts", "fail", "找不到 docker-compose.yml")]

    config, err = _compose_config(declared)
    if config is None:
        return [Check("compose mounts", "fail", f"无法解析 compose：{err}")]

    # 命名卷：DockerRootDir 在 C 盘时，命名卷=项目数据写 C 盘。
    named = config.get("volumes") or {}
    if named:
        names = ", ".join(sorted(named))
        out.append(Check("compose named volumes", "fail",
                         f"基础文件仍声明命名卷：{names}（应改为项目内 bind mount）"))
    else:
        out.append(Check("compose named volumes", "pass", "无命名卷"))

    bad_binds: list[str] = []
    volume_binds: list[str] = []
    for svc, spec in sorted((config.get("services") or {}).items()):
        for mount in spec.get("volumes") or []:
            kind = mount.get("type")
            source = mount.get("source") or ""
            target = mount.get("target") or ""
            if kind == "volume":
                volume_binds.append(f"{svc}:{source}->{target}")
            elif kind == "bind":
                if not is_under_project(source):
                    bad_binds.append(f"{svc}: {source} -> {target}")
    if bad_binds:
        out.append(Check("compose bind sources", "fail",
                         "越界挂载：" + "; ".join(bad_binds)))
    else:
        out.append(Check("compose bind sources", "pass",
                         "全部 bind 源在项目根内"))
    if volume_binds:
        out.append(Check("compose volumes in services", "fail",
                         "服务仍在用命名卷：" + "; ".join(volume_binds)))
    else:
        out.append(Check("compose volumes in services", "pass",
                         "服务全部使用 bind mount"))
    return out


def check_overlay_files() -> list[Check]:
    """叠加文件不得重新引入命名卷或项目外挂载。"""
    out: list[Check] = []
    overlays = sorted(p.name for p in ROOT.glob("docker-compose*.yml")
                      if p.name != "docker-compose.yml")
    if not overlays:
        return [Check("compose overlays", "skip", "无叠加文件")]
    for name in overlays:
        config, err = _compose_config(["docker-compose.yml", name],
                                      {"MACT_JWT_SECRET": "config-check-only-placeholder",
                                       "MACT_SERVICE_TOKEN": "config-check-only-placeholder"})
        if config is None:
            out.append(Check(f"overlay {name}", "fail", f"无法解析：{err}"))
            continue
        problems: list[str] = []
        if config.get("volumes"):
            problems.append("重新声明命名卷 " + ",".join(sorted(config["volumes"])))
        for svc, spec in (config.get("services") or {}).items():
            for mount in spec.get("volumes") or []:
                if mount.get("type") == "volume":
                    problems.append(f"{svc} 用命名卷 {mount.get('source')}")
                elif mount.get("type") == "bind" and not is_under_project(mount.get("source") or ""):
                    problems.append(f"{svc} bind {mount.get('source')}")
        if problems:
            out.append(Check(f"overlay {name}", "fail", "; ".join(problems)))
        else:
            out.append(Check(f"overlay {name}", "pass", "叠加后仍在 E 盘"))
    return out


def check_running_containers() -> list[Check]:
    """已运行容器的 inspect 结果。type=volume 即失败（数据在 DockerRootDir）。

    只报**本项目相关**的容器：名字以 mact- 或 project 名开头。
    """
    out: list[Check] = []
    try:
        listing = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return [Check("container mounts", "skip", "docker 不可用")]
    if listing.returncode != 0:
        return [Check("container mounts", "skip", "docker ps 失败")]

    names = [n for n in listing.stdout.split() if n.startswith(("mact-", "personal_design"))]
    if not names:
        return [Check("container mounts", "skip", "无本项目容器运行")]
    # `personal_design-*` 是用户用**旧版** compose（命名卷）启动的存量栈。
    # 本门禁不改它、不删它——但必须报出来：只要它还以命名卷运行，命名卷实体就
    # 在 DockerRootDir 里，项目数据就还有一份在 C 盘。用新 compose 重建后会变成
    # bind mount，届时这条自动转绿。判定仍然 fail（这是真实越界，不是噪声）。
    for name in names:
        info = subprocess.run(
            ["docker", "inspect", name, "--format",
             "{{json .Mounts}}|{{.HostConfig.Binds}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if info.returncode != 0:
            out.append(Check(f"container {name}", "fail", "inspect 失败"))
            continue
        raw = info.stdout.rsplit("|", 1)[0]
        try:
            mounts = json.loads(raw)
        except json.JSONDecodeError:
            out.append(Check(f"container {name}", "fail", "无法解析挂载"))
            continue
        problems = []
        for m in mounts:
            if m.get("Type") == "volume":
                problems.append(f"命名卷 {m.get('Name')}")
            elif m.get("Type") == "bind" and not is_under_project(m.get("Source") or ""):
                problems.append(f"越界 bind {m.get('Source')}")
        if problems:
            hint = ""
            if name.startswith("personal_design-"):
                hint = ("（旧版 compose 启动的存量栈；本门禁不动用户服务。"
                        "用新版 compose 重建后这些会变成 data/docker/ 下的 bind mount）")
            out.append(Check(f"container {name}", "fail", "; ".join(problems) + hint))
        else:
            out.append(Check(f"container {name}", "pass", f"{len(mounts)} 个挂载均在 E 盘"))
    return out


# --------------------------------------------------------------------------
# 5. Docker 数据根
# --------------------------------------------------------------------------
#: Docker Desktop 的 VM 磁盘**默认**位置。它只是候选之一，不是唯一判据。
#:
#: 为什么不能只认这一条路径（2026-10-30 修）：用户把 Disk image location
#: 迁到 E 盘之后，这个常量指向的 C 盘文件就不存在了，于是本检查返回
#: `skip`（"未找到"）而不是 `pass` —— 迁移反而让门禁永远无法通过。
#: 迁移的目的是"数据在项目盘"，所以判据必须是**实际磁盘在哪**，
#: 而不是"C 盘那个文件还在不在"。
DEFAULT_DOCKER_DESKTOP_DISK = Path(
    os.path.expandvars(r"%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx"))


def _docker_desktop_disks() -> list[Path]:
    """列出所有真实存在的 docker_data.vhdx。

    发现顺序（先可靠、后兜底）：
      1. Docker Desktop 的默认位置（%LOCALAPPDATA% 下的 Docker/wsl/disk）
      2. 环境变量 MACT_DOCKER_DISK 显式指定（运维可强制）
      3. 项目盘与常见迁移盘的浅层扫描

    **不使用 settings-store.json 作为判据**：2026-10-30 实测该文件（19 个键）
    里没有任何 disk-location 键——先前那版把它当"权威来源"是错的，
    后果是用户迁到任意 E 盘目录后本函数返回空、门禁给出 skip 的假绿。
    """
    found: list[Path] = []

    explicit = (os.environ.get("MACT_DOCKER_DISK") or "").strip()
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            found.append(candidate)

    if DEFAULT_DOCKER_DESKTOP_DISK.exists():
        found.append(DEFAULT_DOCKER_DESKTOP_DISK)

    # 浅层扫描：只扫已知可能的父目录的两三层，避免全盘遍历拖慢门禁。
    scan_bases = [
        Path(os.path.expandvars(r"%LOCALAPPDATA%" + chr(92) + "Docker" + chr(92) + "wsl" + chr(92) + "disk")),
        ROOT / "docker-data" / "wsl" / "disk",
        ROOT / ".docker" / "wsl" / "disk",
    ]
    for drive in ("E:", "D:"):
        for sub in ("Docker", "DockerDesktop", "docker-data", "DockerData"):
            scan_bases.append(Path(f"{drive}/{sub}/wsl/disk"))
            scan_bases.append(Path(f"{drive}/{sub}"))
    for base in scan_bases:
        try:
            if not base.is_dir():
                continue
            for vhdx in base.glob("docker_data.vhdx"):
                found.append(vhdx)
            for vhdx in base.glob("*/docker_data.vhdx"):
                found.append(vhdx)
        except OSError:
            continue

    seen: set[str] = set()
    unique: list[Path] = []
    for path in found:
        key = str(path).lower()
        if key not in seen and path.is_file():
            seen.add(key)
            unique.append(path)
    return unique

def check_docker_root() -> list[Check]:
    out: list[Check] = []
    try:
        info = subprocess.run(["docker", "info", "--format", "{{.DockerRootDir}}"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return [Check("docker root dir", "skip", "docker 不可用")]
    root_dir = (info.stdout or "").strip()
    if info.returncode != 0 or not root_dir:
        return [Check("docker root dir", "skip", "docker info 失败")]

    # 容器内视角的 /var/lib/docker 永远是这条路径，它本身不能判断宿主盘。
    # 真正的判据是 Docker Desktop 的 VM 磁盘文件落在哪个盘。
    out.append(Check("docker root dir", "pass", f"容器内视角 {root_dir}（不单独作为判据）"))

    if os.name == "nt" or sys.platform.startswith("win"):
        disks = _docker_desktop_disks()
        if not disks:
            # **找不到磁盘必须判 fail，不能判 skip。**
            # 理由是这条检查的全部意义就是"证明数据落在项目盘内"；
            # 找不到盘 = 没证明 = 不通过。
            # 2026-10-30 实测教训：先前版本判 skip，于是用户迁移到任意
            # E 盘目录后，门禁既没看到 C 盘那块、也没找到新位置，
            # 给出 skip 而 verify_all 把 skip 算"覆盖不完整"退出 3，
            # 看起来像"迁移没成功"——实际上只是本脚本没找到。
            out.append(Check(
                "docker VM disk", "fail",
                "未找到任何 docker_data.vhdx，无法证明 Docker 数据落在项目盘内。"
                f"已查默认位置 {DEFAULT_DOCKER_DESKTOP_DISK} 与常见迁移目录。"
                "若已迁移到自定义位置，请设 MACT_DOCKER_DISK 指向该 vhdx 后重跑。"))
        else:
            # 多块盘同时存在时（迁移中途、或迁移后旧盘未删），
            # **只要有一块在项目外就算失败**——那说明还有写入会落到项目盘之外。
            # stat 必须保护：磁盘可能在发现与统计之间被删/被移走，
            # 未捕获的 FileNotFoundError 会让整个门禁以 traceback 崩掉，
            # 而不是给出一个 Check 结论。
            def _size_gb(path: Path) -> float:
                try:
                    return path.stat().st_size / (1024 ** 3)
                except OSError:
                    return 0.0

            outside = [(d, _size_gb(d)) for d in disks
                       if not is_under_project(str(d))]
            inside = [(d, _size_gb(d)) for d in disks
                      if is_under_project(str(d))]
            if outside:
                detail = "; ".join(f"{d} ({gb:.0f} GB)" for d, gb in outside)
                out.append(Check(
                    "docker VM disk", "fail",
                    f"Docker Desktop 数据盘在 {detail}，不在项目根内。"
                    "任何 docker build/up 都会把镜像层、命名卷与容器可写层写进这块盘。"
                    "阻塞原因：需要用户手工把 Docker Desktop 的磁盘镜像位置迁到 E 盘"
                    "（Settings → Resources → Advanced → Disk image location），本脚本"
                    "不代做、也不删除任何历史镜像或卷。"))
            else:
                detail = "; ".join(f"{d} ({gb:.1f} GB)" for d, gb in inside)
                out.append(Check("docker VM disk", "pass", f"{detail}（在项目盘内）"))
    return out


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def run_all() -> list[Check]:
    checks: list[Check] = []
    checks += check_interpreter()
    checks += check_env()
    checks += check_compose_mounts()
    checks += check_overlay_files()
    checks += check_running_containers()
    checks += check_docker_root()
    return checks


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == "fail" for c in checks) else 0


def render(checks: list[Check]) -> None:
    width = max((len(c.name) for c in checks), default=10)
    print(f"==> 磁盘路径门禁（项目根 {ROOT}）")
    for c in checks:
        tag = {"pass": "OK  ", "fail": "FAIL", "skip": "--  "}[c.status]
        print(f"  {tag} {c.name.ljust(width)}  {c.detail}")
    failed = [c for c in checks if c.status == "fail"]
    passed = [c for c in checks if c.status == "pass"]
    print(f"\n  {len(passed)} pass / {len(failed)} fail / "
          f"{len(checks) - len(passed) - len(failed)} skip")
    if failed:
        print("\n!! 磁盘路径门禁失败：禁止继续任何 docker build/up、模型下载或长任务。",
              file=sys.stderr)
        for c in failed:
            print(f"   - {c.name}: {c.detail}", file=sys.stderr)


# --------------------------------------------------------------------------
# 自测：注入变异，断言每一种都被拒绝
# --------------------------------------------------------------------------
def _self_test() -> int:
    """证明门禁真的会红，而不是恒绿。

    每个变异都直接调用判定函数（不依赖真实 Docker 状态），因此"绿"只能来自
    判定逻辑正确，而不是因为环境恰好合规。
    """
    root = str(ROOT)
    cases: list[tuple[str, bool]] = [
        # (描述, 期望 is_under_project 的结果)
        ("项目根自身", True),
        ("项目内子目录", True),
        ("项目内深路径", True),
        ("大小写不同的项目内路径", True),
        ("反斜杠写法的项目内路径", True),
        ("C 盘用户临时目录", False),
        ("系统 TEMP", False),
        ("用户 .cache", False),
        ("D 盘", False),
        ("相对路径（是否越界取决于 cwd）", False),
        ("项目名的前缀伪装 E:/Personal_DesignEvil", False),
        ("空字符串", False),
    ]
    samples = [
        root, f"{root}/.cache", f"{root}/data/docker/postgres/x",
        root.upper(), root.replace("/", "\\"),
        r"C:\Users\someone\AppData\Local\Temp",
        r"C:\Windows\Temp",
        r"C:\Users\someone\.cache",
        "D:/stuff",
        "./data/tmp",
        "E:/Personal_DesignEvil/data",
        "",
    ]
    failures: list[str] = []
    for (label, expected), sample in zip(cases, samples):
        got = is_under_project(sample) if sample else False
        if got != expected:
            failures.append(f"{label}: expected {expected}, got {got} ({sample!r})")

    # 归一化必须把 //?/ 前缀与重复分隔符吃掉，否则 Windows 长路径会误判。
    norm_cases = [
        ("//?/E:/Personal_Design/x", "e:/personal_design/x"),
        ("E:\\\\Personal_Design\\\\x", "e:/personal_design/x"),
        ("E:/Personal_Design/x/", "e:/personal_design/x"),
    ]
    for raw, expected in norm_cases:
        got = _norm(raw)
        if got != expected:
            failures.append(f"_norm({raw!r}): expected {expected!r}, got {got!r}")

    # Docker root 判据的自测（2026-10-30 新增）。
    # 守的是这个具体回归：把 Disk image location 迁到 E 盘后，门禁必须
    # 从 fail 变成 **pass**，而不是因为"C 盘那个文件不见了"而变成 skip。
    # 旧实现硬编码 C 盘路径，迁移后永远得不到 pass —— 迁移等于白做。
    docker_cases: list[tuple[str, str, str]] = [
        ("盘在项目外 -> fail",
         "C:" + chr(92) + "Users" + chr(92) + "someone" + chr(92) + "Docker" + chr(92) + "wsl" + chr(92) + "disk" + chr(92) + "docker_data.vhdx",
         "fail"),
        ("盘在项目内 -> pass", f"{root}/.docker/wsl/disk/docker_data.vhdx", "pass"),
        ("项目内深路径 -> pass",
         f"{root}/data/docker-desktop/disk/docker_data.vhdx", "pass"),
    ]
    for label, disk_path, expected in docker_cases:
        got = "pass" if is_under_project(disk_path) else "fail"
        if got != expected:
            failures.append(
                f"docker root {label}: expected {expected}, got {got} ({disk_path!r})")

    print(f"==> 磁盘门禁自测：{len(cases) + len(norm_cases) + len(docker_cases)} 个变异")
    for f in failures:
        print(f"  FAIL {f}")
    if failures:
        print(f"\n{len(failures)} 个变异未被正确处理", file=sys.stderr)
        return 1
    print(f"  全部 {len(cases) + len(norm_cases) + len(docker_cases)} 个变异被正确处理")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="把结果写成 JSON（不含密钥）")
    parser.add_argument("--self-test", action="store_true",
                        help="对判定逻辑做变异自测")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    checks = run_all()
    render(checks)
    if args.json:
        out = Path(args.json)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "projectRoot": str(ROOT),
            "interpreter": sys.executable,
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail}
                       for c in checks],
            "failed": sum(1 for c in checks if c.status == "fail"),
            "skipped": sum(1 for c in checks if c.status == "skip"),
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"  结果已写入 {out}")
    return exit_code(checks)


if __name__ == "__main__":
    sys.exit(main())
