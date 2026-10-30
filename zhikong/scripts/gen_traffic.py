"""生成真实流量以填充可观测性指标（T1 证据链）。

流程：登录演示账号 -> 提交混合工作流（小额自动放行 / 大额进人工审批）
      -> 审批人批复 -> 打 /health 与带令牌的 /metrics 压力
      -> 制造无令牌 / 错误令牌的 401，使 5xx/4xx 错误率指标有数据。

用法：
    python scripts/gen_traffic.py --base http://localhost:8000 \
        --metrics-token zhikong-monitoring-demo-token
"""
from __future__ import annotations

import argparse
import json
import random

import httpx

API = "/api/v1"


def login(client: httpx.Client, username: str, password: str) -> str:
    r = client.post(
        f"{API}/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]["access_token"]


def submit_mix(client: httpx.Client, token: str, n: int) -> list[str]:
    """提交 n 条工作流：一半小额（自动放行），一半大额（进审批）。返回大额 thread_id。"""
    headers = {"Authorization": f"Bearer {token}"}
    paused: list[str] = []
    for i in range(n):
        amount = random.choice([1200.0, 3500.0, 8800.0, 66000.0, 120000.0])
        r = client.post(
            f"{API}/workflow/submit",
            headers=headers,
            json={
                "request_type": "procurement",
                "request_detail": f"第{i+1}笔采购：服务器/网络设备，部门IT",
                "amount": amount,
                "department": "IT",
            },
            timeout=60,
        )
        r.raise_for_status()
        tid = r.json()["data"].get("thread_id")
        if r.json()["data"].get("paused"):
            paused.append(tid)
    return paused


def approve(client: httpx.Client, token: str, thread_ids: list[str]) -> int:
    headers = {"Authorization": f"Bearer {token}"}
    ok = 0
    for tid in thread_ids:
        r = client.post(
            f"{API}/approval/decide/{tid}",
            headers=headers,
            json={"approved": True, "comment": "额度合规，批准"},
            timeout=60,
        )
        if r.status_code == 200:
            ok += 1
    return ok


def hammer(client: httpx.Client, token: str, health: int, metrics: int) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(health):
        client.get(f"{API}/health", timeout=10)
    for _ in range(metrics):
        # 带令牌的 /metrics（模拟 prometheus 刮取）
        client.get(f"{API}/metrics", headers=headers, timeout=10)


def inject_401(client: httpx.Client, count: int) -> None:
    """制造无令牌 / 错误令牌请求，使 4xx 错误率指标有数据。"""
    for _ in range(count):
        # 无令牌访问受保护端点 -> 401
        client.get(f"{API}/workflow/list", timeout=10)
        # 错误令牌 -> 401
        client.get(f"{API}/workflow/list",
                   headers={"Authorization": "Bearer bad.token.here"}, timeout=10)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--metrics-token", default="zhikong-monitoring-demo-token")
    p.add_argument("--submits", type=int, default=20)
    p.add_argument("--health", type=int, default=120)
    p.add_argument("--metrics", type=int, default=60)
    p.add_argument("--err", type=int, default=40)
    args = p.parse_args()

    summary: dict = {"base": args.base, "errors": []}
    with httpx.Client(base_url=args.base) as client:
        # 1) 登录三个角色
        tokens = {}
        for role, user, pwd in [
            ("applicant", "applicant01", "applicant123"),
            ("approver", "approver01", "approver123"),
            ("admin", "admin", "admin123"),
        ]:
            try:
                tokens[role] = login(client, user, pwd)
            except Exception as e:  # noqa: BLE001
                summary["errors"].append(f"login {role}: {e}")

        # 2) 提交混合工作流（用 applicant）
        paused = submit_mix(client, tokens["applicant"], args.submits)
        summary["submitted"] = args.submits
        summary["paused_for_approval"] = len(paused)

        # 3) 审批人批复（用 approver）
        summary["approved"] = approve(client, tokens["approver"], paused)

        # 4) 健康 + 指标刮取压力（用 admin）
        hammer(client, tokens["admin"], args.health, args.metrics)

        # 5) 制造 401 错误率
        inject_401(client, args.err)

        # 6) 无令牌访问 /metrics 应被拒（验证 scrape auth 生效）
        r = client.get(f"{API}/metrics", timeout=10)
        summary["metrics_no_token_status"] = r.status_code

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
