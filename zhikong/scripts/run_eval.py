#!/usr/bin/env python3
"""离线评测（P1-5）：风险分级 / 注入鲁棒性 / 知识检索命中率。

与 RAGAS 等"用 LLM 评 LLM"的方案不同，本项目的默认链路是**规则 + 离线向量检索**，
结论本身是确定性的，因此评测也应当是确定性、可离线、零 API 成本的：
这样它才能进 CI，每次提交都跑，而不是只在论文里好看。

判分项：
- risk       ：risk_level 必须与期望完全一致（金额阈值分级）
- injection  ：风险等级不得低于期望下限（注入不得诱导降级）
- retrieval  ：检索上下文必须包含全部关键片段（否则 RAG 等于没召回）

用法：
    python scripts/run_eval.py                      # 人读报告
    python scripts/run_eval.py --json               # 机器可读（CI 归档）
    python scripts/run_eval.py --threshold 0.9      # 低于阈值退出码 1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import settings  # noqa: E402
from services import knowledge_service as ks  # noqa: E402
from workflows.providers import build_llm  # noqa: E402

CASES_FILE = ROOT / "evals" / "cases.jsonl"
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def load_cases() -> list[dict]:
    cases = []
    for raw in CASES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("//"):
            cases.append(json.loads(line))
    return cases


def run_case(case: dict, llm, top_k: int) -> tuple[bool, str]:
    """返回 (是否通过, 说明)。"""
    kind = case["kind"]
    expect = case["expect"]

    if kind in ("risk", "injection"):
        knowledge = ks.search(case.get("detail", ""), top_k=top_k)
        res = llm.analyze(
            request_type=case.get("request_type", "procurement"),
            detail=case.get("detail", ""),
            amount=float(case.get("amount", 0)),
            department=case.get("department", ""),
            knowledge=knowledge,
        )
        if kind == "risk":
            ok = res.risk_level == expect["risk_level"]
            return ok, f"期望 {expect['risk_level']} / 实际 {res.risk_level}"
        floor = expect["min_risk"]
        ok = _RISK_ORDER.get(res.risk_level, 0) >= _RISK_ORDER[floor]
        return ok, f"不得低于 {floor} / 实际 {res.risk_level}"

    if kind == "retrieval":
        ctx = ks.search(case["query"], top_k=top_k)
        missing = [kw for kw in expect.get("must_contain", []) if kw not in ctx]
        ok = not missing
        detail = "命中" if ok else f"未召回: {missing}"

        # 判别性检查：不仅"召回了"，还要求**排在 top-1**——
        # 否则 top_k 调大就能刷满分，评测失去区分度（小知识库尤其容易假通过）。
        if ok and expect.get("top1_contains"):
            top1 = ks.search(case["query"], top_k=1)
            miss1 = [kw for kw in expect["top1_contains"] if kw not in top1]
            if miss1:
                ok = False
                detail = f"top-1 未命中: {miss1}"

        # 负例：与规则无关的查询不应召回关键条款（防止"总有召回"的自欺）
        if ok and expect.get("must_not_contain"):
            leaked = [kw for kw in expect["must_not_contain"] if kw in ctx]
            if leaked:
                ok = False
                detail = f"不应召回却命中: {leaked}"

        return ok, detail

    return False, f"未知用例类型 {kind}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.9, help="总准确率阈值，低于则退出码 1")
    ap.add_argument("--top-k", type=int, default=settings.TOP_K)
    ap.add_argument("--json", action="store_true", help="输出 JSON（供 CI 归档）")
    args = ap.parse_args()

    ks.build()
    llm = build_llm()
    cases = load_cases()

    results: list[dict] = []
    for case in cases:
        try:
            ok, detail = run_case(case, llm, args.top_k)
        except Exception as e:  # 单条失败不应中断整轮评测
            ok, detail = False, f"执行异常: {e}"
        results.append({"id": case["id"], "kind": case["kind"], "passed": ok,
                        "detail": detail, "note": case.get("note", "")})

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    acc = passed / total if total else 0.0

    by_kind: dict[str, list[bool]] = {}
    for r in results:
        by_kind.setdefault(r["kind"], []).append(r["passed"])

    if args.json:
        print(json.dumps({
            "total": total, "passed": passed, "accuracy": round(acc, 4),
            "by_kind": {k: {"total": len(v), "passed": sum(v),
                            "accuracy": round(sum(v) / len(v), 4)}
                        for k, v in by_kind.items()},
            "cases": results,
        }, ensure_ascii=False, indent=2))
    else:
        print("=" * 62)
        print(f"评测集：{CASES_FILE.relative_to(ROOT)}   用例数：{total}")
        print("=" * 62)
        for r in results:
            flag = "✅" if r["passed"] else "❌"
            print(f"{flag} [{r['kind']:<9}] {r['id']:<8} {r['detail']}"
                  + (f"   ({r['note']})" if r["note"] else ""))
        print("-" * 62)
        for k, v in by_kind.items():
            print(f"  {k:<10} {sum(v)}/{len(v)}  ({sum(v) / len(v):.0%})")
        print("-" * 62)
        print(f"总计：{passed}/{total}  准确率 {acc:.1%}  阈值 {args.threshold:.0%}")

    if acc < args.threshold:
        print(f"未达阈值 {args.threshold:.0%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
