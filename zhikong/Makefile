# 本地质量门禁（与 CI 保持一致，见 .github/workflows/ci.yml）
.PHONY: install lint typecheck test audit lock check all loadtest alert-receiver

install:            ## 安装锁定版本依赖 + 开发工具
	python3.11 -m pip install -r requirements.lock.txt -r requirements-dev.txt

lint:               ## 静态检查（ruff）
	ruff check .

typecheck:          ## 类型检查（mypy）
	mypy .

test:               ## 单元与集成测试（pytest）
	pytest

audit:              ## 依赖漏洞审计（pip-audit）
	python3.11 -m pip_audit -r requirements.txt

lock:               ## 重新生成依赖锁
	python3.11 scripts/gen_lock.py

check:              ## 全部质量门禁（提交前建议跑一次）
	$(MAKE) lint typecheck test audit

all: check

loadtest:           ## 并发压测（需先在 8010 起一个隔离实例，见 docs/21）
	python3.11 scripts/load_test.py --concurrency 1,5,10,25,50,100 --duration 10

alert-receiver:     ## 启动告警 webhook 接收端（:8099，见 docs/22）
	python3.11 scripts/alert_webhook_receiver.py --port 8099 --out evidence/alert-deliveries.jsonl
