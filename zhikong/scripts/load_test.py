#!/usr/bin/env python3
"""HTTP 并发压测：把"并发 100 会怎样"从猜测变成可复现的数字。

为什么不用 locust/k6：
  本项目要的不是"能打多大 QPS"，而是**定位瓶颈在哪一层**。自写脚本可以按场景
  拆分（纯读 / 完整图执行 / SSE 长连接），并把错误原文（例如 database is locked）
  一并落盘，事后能直接对上服务端日志与 /metrics。

口径约定（写进文档时必须一起给，否则数字没有意义）：
  1. 每个并发档位先预热，再固定时长采样；预热数据不计入分位数，避免冷启动污染；
  2. 每次提交携带唯一 idempotency_key —— 否则命中幂等缓存，测到的是缓存而非图执行；
  3. SSE 同时记 TTFB（首字节）与总时长：前者是"用户多久看到反应"，后者是"一次执行多久"；
  4. 客户端与服务端同机（会抢 CPU），结论用于**相对比较与瓶颈定位**，不外推为容量承诺。

用法：
  python scripts/load_test.py --concurrency 1,5,10,25,50 --duration 10
  python scripts/load_test.py --scenario stream --concurrency 10,50 --out evidence/stream.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

DEFAULT_BASE = "http://127.0.0.1:8010"
REQUESTS_PER_WORKER = 1_000_000  # 由 deadline 控制退出，这里只是安全上限


@dataclass
class Sample:
    ok: bool
    status: int
    latency_ms: float
    ttfb_ms: float | None = None
    error: str = ""


@dataclass
class Level:
    scenario: str
    concurrency: int
    duration_s: float
    samples: list[Sample] = field(default_factory=list)

    @property
    def ok(self) -> list[Sample]:
        return [s for s in self.samples if s.ok]

    def latency(self, p: float) -> float:
        vals = [s.latency_ms for s in self.ok]
        return round(float(np.percentile(vals, p)), 2) if vals else 0.0

    def ttfb(self, p: float) -> float:
        vals = [s.ttfb_ms for s in self.ok if s.ttfb_ms is not None]
        return round(float(np.percentile(vals, p)), 2) if vals else 0.0

    def summary(self) -> dict:
        total = len(self.samples)
        ok = len(self.ok)
        errors: dict[str, int] = {}
        for s in self.samples:
            if not s.ok:
                key = f"{s.status} {s.error}"[:120]
                errors[key] = errors.get(key, 0) + 1
        return {
            "scenario": self.scenario,
            "concurrency": self.concurrency,
            "duration_s": round(self.duration_s, 2),
            "requests": total,
            "ok": ok,
            "error_rate_pct": round((total - ok) / total * 100, 2) if total else 0.0,
            "rps": round(ok / self.duration_s, 2) if self.duration_s else 0.0,
            "p50_ms": self.latency(50),
            "p95_ms": self.latency(95),
            "p99_ms": self.latency(99),
            "max_ms": round(max((s.latency_ms for s in self.ok), default=0.0), 2),
            "ttfb_p50_ms": self.ttfb(50),
            "ttfb_p95_ms": self.ttfb(95),
            "errors": errors,
        }


def _body() -> dict:
    """构造一次真实提交：金额跨过审批阈值，确保走到 human-in-the-loop 而非自动通过。"""
    return {
        "request_type": "procurement",
        "request_detail": f"压测申请-{uuid.uuid4().hex[:8]}：采购服务器 2 台",
        "amount": 12800.0,
        "department": "研发部",
        "idempotency_key": uuid.uuid4().hex,  # 唯一：避免命中幂等缓存
    }


async def _one(client: httpx.AsyncClient, scenario: str, url: str, headers: dict[str, str]) -> Sample:
    started = time.perf_counter()
    try:
        if scenario == "health":
            resp = await client.get(url, headers=headers)
            body = resp.read()
            elapsed = (time.perf_counter() - started) * 1000
            ok = resp.status_code == 200 and b'"status"' in body
            return Sample(ok=ok, status=resp.status_code, latency_ms=elapsed,
                          error="" if ok else body[:120].decode("utf-8", "replace"))

        if scenario == "submit":
            resp = await client.post(url, json=_body(), headers=headers)
            elapsed = (time.perf_counter() - started) * 1000
            ok = resp.status_code == 200
            return Sample(ok=ok, status=resp.status_code, latency_ms=elapsed,
                          error="" if ok else resp.text[:160])

        # stream：记录首字节与整条流的耗时，读完整条流（提前 break 会让服务端生成器被取消）
        first: float | None = None
        done = False
        async with client.stream("POST", url, json=_body(), headers=headers) as resp:
            async for raw in resp.aiter_lines():
                if first is None:
                    first = (time.perf_counter() - started) * 1000
                if raw.startswith("event: done"):
                    done = True
            elapsed = (time.perf_counter() - started) * 1000
        ok = resp.status_code == 200 and done
        return Sample(ok=ok, status=resp.status_code, latency_ms=elapsed, ttfb_ms=first,
                      error="" if ok else "stream ended without done event")

    except (httpx.HTTPError, OSError) as e:
        # 压测必须兜住单请求失败，否则一个连接错误会中断整轮采样
        return Sample(ok=False, status=0, latency_ms=(time.perf_counter() - started) * 1000,
                      error=f"{type(e).__name__}: {e}"[:160])


async def _worker(
    client: httpx.AsyncClient,
    scenario: str,
    url: str,
    headers: dict[str, str],
    deadline: float,
    sink: list[Sample],
) -> None:
    for _ in range(REQUESTS_PER_WORKER):
        if time.perf_counter() >= deadline:
            return
        sink.append(await _one(client, scenario, url, headers))


async def _run_level(
    scenario: str,
    url: str,
    headers: dict[str, str],
    concurrency: int,
    duration: float,
    warmup: float,
) -> Level:
    # 连接池要大于并发数：SSE 长连接会一直占着连接，池不够会在客户端侧排队，
    # 把"服务端慢"误判成"客户端排队"。
    limits = httpx.Limits(max_connections=concurrency * 2 + 10, max_keepalive_connections=concurrency + 5)
    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout, http2=False) as client:
        if warmup > 0:
            await asyncio.gather(*[
                _worker(client, scenario, url, headers, time.perf_counter() + warmup, [])
                for _ in range(min(concurrency, 5))
            ])
        level = Level(scenario=scenario, concurrency=concurrency, duration_s=duration)
        started = time.perf_counter()
        await asyncio.gather(*[
            _worker(client, scenario, url, headers, started + duration, level.samples)
            for _ in range(concurrency)
        ])
        level.duration_s = time.perf_counter() - started
        return level


def _login(client: httpx.Client, base: str, username: str, password: str) -> str:
    resp = client.post(f"{base}/api/v1/auth/login", json={"username": username, "password": password})
    resp.raise_for_status()
    token = resp.json()["data"]["access_token"]
    return str(token)


SCENARIOS = {
    "health": ("/api/v1/health", "GET"),
    "submit": ("/api/v1/workflow/submit", "POST"),
    "stream": ("/api/v1/workflow/submit/stream", "POST"),
}


def _print_table(rows: list[dict]) -> None:
    header = ["场景", "并发", "请求", "成功率", "RPS", "P50", "P95", "P99", "TTFB-P95"]
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")
    for r in rows:
        print(
            f"| {r['scenario']} | {r['concurrency']} | {r['requests']} | "
            f"{100 - r['error_rate_pct']:.1f}% | {r['rps']} | {r['p50_ms']} | "
            f"{r['p95_ms']} | {r['p99_ms']} | {r['ttfb_p95_ms'] or '-'} |"
        )


async def _amain(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    concurrency_levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    scenarios = list(SCENARIOS) if args.scenario == "all" else [args.scenario]

    with httpx.Client(timeout=10.0) as sync_client:
        token = _login(sync_client, base, args.username, args.password)
    headers = {"Authorization": f"Bearer {token}"}

    results: list[dict] = []
    for scenario in scenarios:
        path, _method = SCENARIOS[scenario]
        url = f"{base}{path}"
        for c in concurrency_levels:
            print(f"[*] {scenario} 并发={c} 时长={args.duration}s ...", flush=True)
            level = await _run_level(scenario, url, headers, c, args.duration, args.warmup)
            summary = level.summary()
            results.append(summary)
            print(
                f"    ok={summary['ok']}/{summary['requests']} rps={summary['rps']} "
                f"p50={summary['p50_ms']}ms p95={summary['p95_ms']}ms "
                f"err={summary['error_rate_pct']}%",
                flush=True,
            )
            for err, cnt in summary["errors"].items():
                print(f"    ! {cnt}x {err}", flush=True)

    print("\n=== 汇总 ===")
    _print_table(results)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_url": base,
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "note": "客户端与服务端同机，数据用于相对比较与瓶颈定位，不作为容量承诺",
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[+] 证据已写入 {out}")

    if args.csv:
        import csv

        with Path(args.csv).open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(results[0]))
            writer.writeheader()
            writer.writerows(results)
        print(f"[+] CSV 已写入 {args.csv}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="HTTP 并发压测（health / submit / stream）")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--scenario", default="all", choices=[*SCENARIOS, "all"])
    parser.add_argument("--concurrency", default="1,5,10,25,50", help="逗号分隔的并发梯度")
    parser.add_argument("--duration", type=float, default=10.0, help="每个档位的采样时长（秒）")
    parser.add_argument("--warmup", type=float, default=2.0, help="每个档位的预热时长（秒）")
    parser.add_argument("--username", default="applicant01")
    parser.add_argument("--password", default="LoadTest#2026")
    parser.add_argument("--out", default="evidence/load-test.json")
    parser.add_argument("--csv", default="")
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
