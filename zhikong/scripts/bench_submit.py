#!/usr/bin/env python3
"""单请求成本基准：把"一次提交到底花多少毫秒"量出来。

为什么单独做这个：吞吐 = 1 / 单请求串行成本。压测只能看到"排队后的延迟"，
看不到"服务一个请求的真实成本"；两者对不上时，说明瓶颈在排队而不是计算。
docs/21 里的 10.6ms / 17.8ms / 44.2ms 都是这个脚本量的。

用法（要用独立库，避免污染开发数据）：
  DB_URL='sqlite:////tmp/b/z.db' AUDIT_DB=/tmp/b/a.db CHECKPOINTER_DB=/tmp/b/c.db \
  python scripts/bench_submit.py --n 30
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.tracing import new_trace_id  # noqa: E402
from services.workflow_service import _initial_state, workflow_service  # noqa: E402


def _body() -> dict:
    return {
        "request_type": "procurement",
        "request_detail": "单请求基准",
        "amount": 12800.0,  # 跨过审批阈值，确保走完整图（分析 → 中断 → 审计）
        "department": "研发部",
        "idempotency_key": uuid.uuid4().hex,
    }


def run(n: int) -> None:
    for _ in range(n):
        tid = f"WF-{uuid.uuid4().hex[:12]}"
        workflow_service._submit_sync(
            _initial_state(_body(), tid, new_trace_id()), "applicant01", "applicant"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="单请求串行成本基准")
    parser.add_argument("--n", type=int, default=30, help="采样次数（不含预热）")
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--verbose", action="store_true", help="保留业务日志")
    args = parser.parse_args()

    if not args.verbose:
        logging.disable(logging.INFO)

    run(args.warmup)  # 预热：建表、知识库、连接池
    started = time.perf_counter()
    run(args.n)
    per = (time.perf_counter() - started) / args.n * 1000
    print(f"单请求串行成本: {per:.2f} ms  →  理论上限 {1000 / per:.1f} RPS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
