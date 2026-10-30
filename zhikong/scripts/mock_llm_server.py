"""本地 OpenAI 兼容 mock 服务（T2 链路自测用，**不是真实模型**）。

用途：在无真实密钥的环境下，验证 run_eval_llm.py 的
「HTTP 调用 -> 解析 -> judge 调用 -> 分歧统计」整条链路能跑通。
它按金额阈值返回风险等级（模拟一个"行为正确"的模型），judge 请求返回 pass。

明确声明：本 mock 的结果**不代表任何真实模型能力**，仅用于证明脚本可执行。

用法：
    python scripts/mock_llm_server.py --port 18080
    # 另开终端：
    OPENAI_API_KEY=test OPENAI_BASE_URL=http://127.0.0.1:18080/v1 \
      python scripts/run_eval_llm.py --provider openai --report /tmp/mock_eval.md
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer


def _risk_for_amount(amount: float) -> str:
    if amount > 50000:
        return "high"
    if amount > 5000:
        return "medium"
    return "low"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默访问日志
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages", [])
        sys_text = " ".join(m.get("content", "") for m in messages if m.get("role") == "system")
        user_text = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")

        if "评审员" in sys_text:
            # judge 请求：一律判 pass（模拟一个"与规则一致"的评审员）
            content = json.dumps({"verdict": "pass", "score": 0.95, "reason": "结论与金额阈值一致"},
                                 ensure_ascii=False)
        else:
            m = re.search(r"金额[:：]\s*([0-9.]+)", user_text)
            amount = float(m.group(1)) if m else 0.0
            risk = _risk_for_amount(amount)
            content = json.dumps({
                "risk_level": risk,
                "reason": f"按金额 {amount} 判定为 {risk}",
                "required_approvers": [],
            }, ensure_ascii=False)

        resp = {
            "id": "mock-1",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
        }
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    args = ap.parse_args()
    srv = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock LLM server on http://127.0.0.1:{args.port}/v1 (NOT a real model)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
