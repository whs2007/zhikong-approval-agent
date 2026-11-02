"""PostgreSQL Checkpointer 跨进程恢复验证（可复现）。

为什么需要这个脚本：`tests/` 里的单测不能连 PG（CI 没有数据库），而"跨进程恢复"
这个能力只有**真的起两次进程**才证明得了。所以把它做成两步脚本：

    # 第一步：提交（进程 A），状态落 PG，返回 thread_id
    python scripts/verify_pg_checkpointer.py --submit

    # 第二步：另起进程 B，从 PG 读回同一个 thread 并审批恢复
    python scripts/verify_pg_checkpointer.py --resume WF-xxxxxxxx

依赖：pip install -r requirements-postgres.txt
环境：CHECKPOINTER=postgres  POSTGRES_URL=postgresql://...
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import settings  # noqa: E402


def _require_pg() -> None:
    if (settings.CHECKPOINTER or "").lower() != "postgres":
        print(f"请设置 CHECKPOINTER=postgres（当前 {settings.CHECKPOINTER!r}）")
        raise SystemExit(2)
    if not settings.POSTGRES_URL:
        print("请设置 POSTGRES_URL")
        raise SystemExit(2)


async def _submit() -> str:
    from services.workflow_service import workflow_service

    out = await workflow_service.submit(
        {
            "request_type": "procurement",
            "request_detail": "PG 跨进程恢复验证：采购服务器",
            "amount": 88000,
            "department": "研发部",
        },
        actor="verify-bot", actor_role="applicant")
    st = out["state"]
    tid = st["thread_id"]
    print(f"提交完成 thread={tid} phase={st.get('current_phase')} "
          f"approval={st.get('approval_status')} paused={out.get('paused')}")
    print(f"下一步：另起进程执行 --resume {tid}")
    return tid


async def _resume(thread_id: str, approve: bool) -> None:
    from services.workflow_service import workflow_service

    before = await workflow_service.status(thread_id)
    if before is None:
        print(f"PG 中没有 thread={thread_id} 的状态")
        raise SystemExit(1)
    print(f"[恢复前] phase={before.get('current_phase')} "
          f"approval={before.get('approval_status')} amount={before.get('amount')}")

    out = await workflow_service.decide(
        thread_id, approve, "跨进程恢复后审批", "verify-bot", "approver")
    print(f"[恢复后] phase={out.get('current_phase')} "
          f"approval={out.get('approval_status')} "
          f"exec={str(out.get('execution_result'))[:80]}")

def main() -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--submit", action="store_true", help="提交一个会进入人工审批的申请")
    g.add_argument("--resume", metavar="THREAD_ID", help="从 PG 恢复指定 thread 并审批")
    p.add_argument("--reject", action="store_true", help="配合 --resume：改为拒绝")
    args = p.parse_args()

    print(f"checkpointer={settings.CHECKPOINTER} url={settings.POSTGRES_URL}")
    _require_pg()
    if args.submit:
        asyncio.run(_submit())
    else:
        asyncio.run(_resume(args.resume, not args.reject))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("TOOL_FAILURE_RATE", "0")
    raise SystemExit(main())
