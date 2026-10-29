#!/usr/bin/env python3
"""重新生成 requirements.lock.txt（依赖锁）。

用法：
    python scripts/gen_lock.py           # 生成到 requirements.lock.txt
    python scripts/gen_lock.py --check   # 仅校验：与现有锁文件是否一致（CI 可用）

原理：用 `pip install --dry-run --report` 解析 requirements.txt 的完整依赖树
（含传递依赖）并固化精确版本，避免"直接依赖没变、传递依赖悄悄漂移"。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock.txt"


def resolve() -> list[tuple[str, str]]:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        report_path = tmp.name
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
         "--quiet", "--report", report_path, "-r", str(REQUIREMENTS)],
        check=True,
        cwd=ROOT,
    )
    data = json.loads(Path(report_path).read_text())
    return sorted({(i["metadata"]["name"].lower(), i["metadata"]["version"])
                   for i in data["install"]})


def render(pkgs: list[tuple[str, str]]) -> str:
    head = [
        "# 依赖锁文件（P1-6）：精确版本，保证开发/CI/生产三环境依赖一致。",
        "# 生成方式：pip install --dry-run --report -r requirements.txt 解析完整依赖树后固化。",
        "# 重新生成：make lock（或 python scripts/gen_lock.py）；升级依赖后需重新生成并跑一次 CI。",
        f"# 生成时间：{datetime.now(UTC).isoformat(timespec='seconds')}"
        f"（{len(pkgs)} 个包，含传递依赖）",
        "",
    ]
    return "\n".join(head + [f"{n}=={v}" for n, v in pkgs]) + "\n"


def main() -> int:
    pkgs = resolve()
    content = render(pkgs)
    if "--check" in sys.argv:
        if not LOCK.exists():
            print("锁文件不存在", file=sys.stderr)
            return 1
        existing = LOCK.read_text().splitlines()
        new_lines = content.splitlines()
        # 忽略首部时间戳行
        diff = [line for line in existing
                if line not in new_lines and not line.startswith("# 生成时间")]
        if diff:
            print("锁文件已过期，请运行 make lock：", file=sys.stderr)
            for line in diff[:10]:
                print("  " + line, file=sys.stderr)
            return 1
        print("锁文件一致")
        return 0
    LOCK.write_text(content)
    print(f"已生成 {LOCK.relative_to(ROOT)}（{len(pkgs)} 个包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
