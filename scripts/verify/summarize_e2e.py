#!/usr/bin/env python
"""把一份 E2E 证据 JSON 压成几行 Markdown（CI 摘要用）。

    python scripts/verify/summarize_e2e.py docs/reviews/evidence/e2e-*.json

存在的理由：CI 摘要里如果只写"失败"，读者还得下载制品才知道哪一条挂了；
而如果摘要写死一份表格，它与 JSON 会逐渐不一致。这里从**同一份 JSON**
生成摘要，与交付报告用同一套字段。

字段口径（照 browser_evidence_e2e.py 的实际输出，不是猜的）：

    cases[].case / status / assertions / missing / requiredSkips
    summary.passed / failed / skipped / incomplete / coverage
    summary.assertions_pass / assertions_fail / assertions_skip
    source.head / source.workingTreeDirty

退出码：0 = 用例全通过；1 = 有失败/未执行的用例；2 = 读不出 JSON。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"无法读取 {path}: {exc}", file=sys.stderr)
        return 2

    cases = data.get("cases") or []
    summary = data.get("summary") or {}

    # 以 summary 为准、cases 为补充：summary 是脚本自己 finalize 后的结论，
    # cases 里可能有 status 尚未归一化的条目。
    total = summary.get("cases_produced", len(cases))
    passed = summary.get("cases_passed", sum(1 for c in cases if c.get("status") == "passed"))

    print(f"- 证据文件：`{path.name}`")
    print(f"- 用例：**{passed}/{total}** passed"
          f"，coverage=`{summary.get('coverage', 'unknown')}`")
    print(f"- 断言：{summary.get('assertions_pass', '?')} pass / "
          f"{summary.get('assertions_fail', '?')} fail / "
          f"{summary.get('assertions_skip', '?')} skip")

    bad = {
        "failed": summary.get("failed", 0),
        "incomplete": summary.get("incomplete", 0),
        "requiredSkips": summary.get("required_skips_count", 0),
    }
    for label, count in bad.items():
        if count:
            ids = [c.get("case", "?") for c in cases
                   if c.get("status") in ("failed", "error", "incomplete")
                   or c.get("requiredSkips")]
            print(f"- **{label}：{count}** — {', '.join(sorted(set(ids))) or '见 JSON'}")
    optional = summary.get("optional_skips_count", 0)
    if optional:
        print(f"- 可选跳过：{optional}（不阻塞，但覆盖不完整处见 JSON 的 optionalSkips）")

    source = data.get("source") or {}
    print(f"- 代码版本：`{str(source.get('head', '?'))[:12]}` "
          f"dirty=`{source.get('workingTreeDirty')}`")

    ok = (bad["failed"] == 0 and bad["incomplete"] == 0
          and bad["requiredSkips"] == 0 and total > 0)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
