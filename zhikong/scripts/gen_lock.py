#!/usr/bin/env python3
"""依赖锁（requirements.lock.txt）的生成与校验。

用法：
    python scripts/gen_lock.py           # 生成/更新 requirements.lock.txt（需联网，解析完整依赖树）
    python scripts/gen_lock.py --check   # 离线校验：锁文件是否覆盖并满足 requirements.txt（CI 用）

两种模式的差别（重要，P1-6 修复）：
- 生成：`pip install --dry-run --report` 向 PyPI 重新解析完整依赖树，固化精确版本。
- 校验：**不联网、不重解析**，只做静态一致性检查。原因见下。

为什么 --check 不能重新解析（历史 bug）：
    早期实现让 --check 也跑一次 pip resolve，再与锁文件逐行比对。这会导致
    "上游发布新版本 → CI 必然变红"——锁文件本身没任何问题，只是时间过去了。
    实测在首次推送时因此误报：langchain-core/langsmith/pydantic 等出现版本漂移。
    依赖锁的语义是"锁定已知可用版本"，不是"永远等于 PyPI 最新版"，故校验
    只回答两个可确定的问题：
      1) requirements.txt 的每个直接依赖，锁里是否都有精确版本？
      2) 锁里的版本是否满足 requirements.txt 的版本约束？
    传递依赖的完整性由"生成时固化"保证，运行时由 pip 安装失败暴露。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock.txt"

_LOCK_LINE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s#;]+)\s*$")


def resolve() -> list[tuple[str, str]]:
    """联网解析完整依赖树（仅用于生成锁文件）。"""
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


def parse_requirements(path: Path) -> list[Requirement]:
    """解析 requirements.txt 的直接依赖（忽略注释/空行/`-r` 引用）。"""
    reqs: list[Requirement] = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            reqs.append(Requirement(line))
        except InvalidRequirement:
            print(f"警告：无法解析依赖行，已跳过：{raw!r}", file=sys.stderr)
    return reqs


def parse_lock(path: Path) -> tuple[dict[str, str], list[str]]:
    """解析锁文件为 {规范化名: 版本}，同时返回格式异常行。"""
    pinned: dict[str, str] = {}
    bad: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LOCK_LINE.match(line)
        if not m:
            bad.append(line)
            continue
        pinned[canonicalize_name(m.group("name"))] = m.group("version")
    return pinned, bad


def check() -> int:
    """离线校验锁文件与 requirements.txt 的一致性。返回退出码。"""
    if not LOCK.exists():
        print("锁文件不存在，请先运行 python scripts/gen_lock.py", file=sys.stderr)
        return 1

    pinned, bad = parse_lock(LOCK)
    problems: list[str] = []

    if bad:
        problems.append(f"锁文件含 {len(bad)} 行非法格式（应为 name==version）："
                        + ", ".join(bad[:5]))

    if not pinned:
        problems.append("锁文件不含任何 `name==version` 行")

    for req in parse_requirements(REQUIREMENTS):
        name = canonicalize_name(req.name)
        if name not in pinned:
            problems.append(f"依赖 {req.name} 在 requirements.txt 中声明，但锁文件缺少精确版本")
            continue
        version = pinned[name]
        try:
            v = Version(version)
        except InvalidVersion:
            problems.append(f"锁文件中 {req.name} 的版本号非法：{version}")
            continue
        if req.specifier and not req.specifier.contains(v, prereleases=True):
            problems.append(f"{req.name}=={version} 不满足 requirements.txt 约束 {req.specifier}")

    if problems:
        print("锁文件与 requirements.txt 不一致（请运行 make lock 重新生成）：", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 1

    print(f"锁文件一致：{len(pinned)} 个包，直接依赖全部覆盖且版本满足约束")
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return check()
    pkgs = resolve()
    content = render(pkgs)
    LOCK.write_text(content)
    print(f"已生成 {LOCK.relative_to(ROOT)}（{len(pkgs)} 个包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
