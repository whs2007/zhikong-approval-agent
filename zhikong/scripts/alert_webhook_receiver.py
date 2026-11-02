#!/usr/bin/env python3
"""Alertmanager Webhook 接收端（演示/取证用，不是生产通知服务）。

为什么需要它：告警规则写了 ≠ 告警能送达。要让"闭环"可验证，必须有一个真实端点
接收 Alertmanager 的 POST，并把 payload 落盘——文档里的截图/JSON 才是证据，
而不是"我配置了"。

生产替换：把 alertmanager.yml 的 webhook url 换成钉钉/飞书/企业微信机器人，
或接入 PagerDuty；本脚本仅用于本地演示与回归。

用法：
  python scripts/alert_webhook_receiver.py --port 8099 --out evidence/alert-deliveries.jsonl
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def build_handler(out_path: Path) -> type[BaseHTTPRequestHandler]:
    """闭包持有输出路径，避免用模块级 global 传参（可测试、无共享状态）。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # 静默默认访问日志，避免刷屏
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) or b"{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return

            record = {
                "received_at": datetime.now().isoformat(timespec="seconds"),
                "status": payload.get("status"),
                "receiver": payload.get("receiver"),
                "alerts": [
                    {
                        "name": a.get("labels", {}).get("alertname"),
                        "status": a.get("status"),
                        "severity": a.get("labels", {}).get("severity"),
                        "summary": a.get("annotations", {}).get("summary"),
                        "startsAt": a.get("startsAt"),
                        "endsAt": a.get("endsAt"),
                    }
                    for a in payload.get("alerts", [])
                ],
            }
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

            # 控制台留痕：演示时肉眼可见"告警真的到了"
            print(f"[ALERT] {record['status']} "
                  + ", ".join(f"{a['name']}({a['status']})" for a in record["alerts"]), flush=True)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Alertmanager webhook 接收端")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--out", default="evidence/alert-deliveries.jsonl")
    args = parser.parse_args()

    out_path = Path(args.out)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), build_handler(out_path))
    print(f"[*] webhook receiver listening on 0.0.0.0:{args.port} -> {out_path}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
