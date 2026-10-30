"""全局配置（环境变量驱动，零硬编码密钥）。

所有敏感/环境相关参数均从环境变量读取，代码库内不含任何明文密钥。
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import ClassVar

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_DIR = DATA_DIR / "db"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
DB_DIR.mkdir(parents=True, exist_ok=True)


def _env(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key, str(default)).lower()
    return v in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


class Settings:
    """运行配置。直接读取环境变量，避免额外依赖。"""

    ENV: str = _env("ENV", "dev")

    # ---- 安全 ----
    # 代码库内不含任何可用密钥：缺省为空，由 _ensure_dev_secret 在非生产环境
    # 生成随机密钥并持久化到 data/db/.dev_secret（已 gitignore），生产环境缺失即启动失败。
    SECRET_KEY: str = _env("SECRET_KEY", "")

    # 监控刮取令牌（T1 可观测性闭环）：Prometheus 等采集端用它作为 Bearer 抓取
    # /metrics，无需为每个采集器签发/轮换短期 JWT。
    # - 留空：/metrics 仅接受 JWT（保持原有行为，不降低安全姿态）；
    # - 设置后：JWT 与 该令牌 二选一即可访问 /metrics。
    # 指标可能暴露业务量等运营信息，故仍要求令牌；切勿用弱令牌上线。
    METRICS_TOKEN: str = _env("METRICS_TOKEN", "")
    JWT_EXPIRE_MINUTES: int = _env_int("JWT_EXPIRE_MINUTES", 60)
    JWT_ALG: str = "HS256"

    # ---- 演示账号种子（默认关闭） ----
    # 安全基线：任何环境都不会凭代码默认值自动创建可登录账号。
    # 仅当 ENV ∈ {dev,development,local,test} 且 DEMO_SEED=1 时创建；口令必须来自环境变量，
    # 未提供则随机生成并打印到日志（一次性，不落代码、不落仓库）。
    DEMO_SEED: bool = _env_bool("DEMO_SEED", False)
    DEMO_ADMIN_PASSWORD: str = _env("DEMO_ADMIN_PASSWORD", "")
    DEMO_APPROVER_PASSWORD: str = _env("DEMO_APPROVER_PASSWORD", "")
    DEMO_APPLICANT_PASSWORD: str = _env("DEMO_APPLICANT_PASSWORD", "")

    # ---- 业务库 / 审计库（sqlite 文件，生产可换 PG 连接串） ----
    DB_URL: str = _env("DB_URL", f"sqlite:///{DB_DIR / 'zhikong.db'}")
    AUDIT_DB: str = _env("AUDIT_DB", str(DB_DIR / "audit.db"))

    # ---- LangGraph Checkpointer ----
    # sqlite（默认，文件持久化） | memory（仅测试） | postgres
    CHECKPOINTER: str = _env("CHECKPOINTER", "sqlite")
    CHECKPOINTER_DB: str = _env("CHECKPOINTER_DB", str(DB_DIR / "checkpoints.db"))
    POSTGRES_URL: str = _env("POSTGRES_URL", "")

    # ---- 知识引擎 / RAG ----
    EMBEDDING_PROVIDER: str = _env("EMBEDDING_PROVIDER", "local")  # local|st|openai|dashscope
    LLM_PROVIDER: str = _env("LLM_PROVIDER", "rule")  # rule|openai|dashscope
    OPENAI_API_KEY: str = _env("OPENAI_API_KEY", "")
    OPENAI_BASE_URL: str = _env("OPENAI_BASE_URL", "https://api.openai.com/v1")
    OPENAI_MODEL: str = _env("OPENAI_MODEL", "gpt-4o-mini")
    DASHSCOPE_API_KEY: str = _env("DASHSCOPE_API_KEY", "")
    DASHSCOPE_MODEL: str = _env("DASHSCOPE_MODEL", "qwen-plus")
    ST_MODEL: str = _env("ST_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

    # 向量检索
    TOP_K: int = _env_int("TOP_K", 3)
    EMBED_DIM: int = _env_int("EMBED_DIM", 256)
    # 混合检索：词项覆盖率打分所占权重（0=纯向量，1=纯关键词）
    HYBRID_LEXICAL_WEIGHT: float = _env_float("HYBRID_LEXICAL_WEIGHT", 0.5)
    # 最小相关度阈值：低于此值不返回片段，避免无关查询硬凑上下文导致幻觉
    RELEVANCE_MIN_SCORE: float = _env_float("RELEVANCE_MIN_SCORE", 0.12)

    # ---- 工作流阈值 ----
    AUTO_APPROVE_THRESHOLD: float = _env_int("AUTO_APPROVE_THRESHOLD", 5000)
    MAX_RETRY: int = _env_int("MAX_RETRY", 3)
    # 重试退避（指数退避 + 抖动），避免失败后立即重入打爆下游
    RETRY_BASE_DELAY_SECONDS: float = _env_float("RETRY_BASE_DELAY_SECONDS", 0.2)
    RETRY_MAX_DELAY_SECONDS: float = _env_float("RETRY_MAX_DELAY_SECONDS", 5.0)
    RETRY_JITTER: float = _env_float("RETRY_JITTER", 0.3)
    # 高风险金额阈值（超过进入人工审批）
    HIGH_RISK_THRESHOLD: float = _env_int("HIGH_RISK_THRESHOLD", 50000)
    MEDIUM_RISK_THRESHOLD: float = _env_int("MEDIUM_RISK_THRESHOLD", 5000)

    # ---- 工具调用韧性（P1-1：超时 / 退避重试 / 熔断） ----
    TOOL_TIMEOUT_SECONDS: float = _env_float("TOOL_TIMEOUT_SECONDS", 5.0)
    TOOL_MAX_RETRIES: int = _env_int("TOOL_MAX_RETRIES", 3)
    BREAKER_FAILURE_THRESHOLD: int = _env_int("BREAKER_FAILURE_THRESHOLD", 3)
    BREAKER_RECOVERY_SECONDS: float = _env_float("BREAKER_RECOVERY_SECONDS", 30.0)
    # 演示工具（SupplierOrderTool）的模拟失败率；生产接真实 API 时不使用。
    # 测试环境由 conftest 置 0 以保证用例确定性。
    TOOL_FAILURE_RATE: float = _env_float("TOOL_FAILURE_RATE", 0.4)

    # ---- LLM 调用治理（P1-4：超时 / 退避重试 / 并发限流 / 降级） ----
    LLM_TIMEOUT_SECONDS: float = _env_float("LLM_TIMEOUT_SECONDS", 20.0)
    LLM_MAX_RETRIES: int = _env_int("LLM_MAX_RETRIES", 2)
    # 并发上限：图在 to_thread 中执行（同步），故用线程信号量限流
    LLM_MAX_CONCURRENCY: int = _env_int("LLM_MAX_CONCURRENCY", 4)
    # 等待并发额度的排队上限；超时即降级，避免无限排队拖垮请求
    LLM_QUEUE_TIMEOUT_SECONDS: float = _env_float("LLM_QUEUE_TIMEOUT_SECONDS", 5.0)
    # 重试耗尽后是否降级到规则模型（关闭则直接抛错，适用于"结论必须来自 LLM"的场景）
    LLM_FALLBACK_ENABLED: bool = _env_bool("LLM_FALLBACK_ENABLED", True)

    # ---- 审批超时（进阶） ----
    APPROVAL_TIMEOUT_HOURS: int = _env_int("APPROVAL_TIMEOUT_HOURS", 24)
    APPROVAL_ESCALATE_HOURS: int = _env_int("APPROVAL_ESCALATE_HOURS", 72)

    # 服务
    HOST: str = _env("HOST", "0.0.0.0")
    PORT: int = _env_int("PORT", 8000)
    # ClassVar：该列表为类级共享配置，不参与实例可变状态（ruff RUF012）
    CORS_ORIGINS: ClassVar[list[str]] = [
        o.strip() for o in _env("CORS_ORIGINS", "*").split(",") if o.strip()
    ]

    # ----------------------- 环境判定 -----------------------

    # 仅这些环境允许演示种子（白名单制，避免 ENV 拼错/自定义值意外放行）
    _DEMO_ENV_WHITELIST = ("dev", "development", "local", "test")

    def is_production(self) -> bool:
        """是否生产环境。ENV ∈ {prod, production} 视为生产。"""
        return self.ENV.strip().lower() in ("prod", "production")

    def is_demo_seed_allowed(self) -> bool:
        """是否允许创建演示账号。白名单环境 + 显式 DEMO_SEED=1，二者缺一不可。"""
        return (
            self.ENV.strip().lower() in self._DEMO_ENV_WHITELIST
            and self.DEMO_SEED
        )

    # ----------------------- 密钥自愈（仅非生产） -----------------------

    @staticmethod
    def _dev_secret_path() -> Path:
        return DB_DIR / ".dev_secret"

    def _ensure_dev_secret(self) -> None:
        """非生产环境且未配置 SECRET_KEY 时，生成随机密钥并持久化。

        目的：代码库不再携带任何可用密钥（含"弱但可用"的默认值），
        同时保证开发/测试开箱可用——密钥存于 data/db/.dev_secret（已被 .gitignore 覆盖），
        重启后复用，避免每次启动都让已签发 token 失效。
        """
        if self.SECRET_KEY or self.is_production():
            return
        path = self._dev_secret_path()
        if path.exists():
            saved = path.read_text(encoding="utf-8").strip()
            if saved:
                self.SECRET_KEY = saved
                return
        import secrets

        generated = secrets.token_urlsafe(48)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated, encoding="utf-8")
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        self.SECRET_KEY = generated

    # ----------------------- 生产自检 -----------------------

    def validate_for_production(self) -> None:
        """生产环境启动前自检：杜绝使用空/弱密钥或演示种子上线。

        仅在 is_production() 为 True 时生效；开发与测试环境（默认 ENV=dev）
        始终跳过，不影响 pytest 与本地联调。
        """
        if not self.is_production():
            return
        if not self.SECRET_KEY:
            raise RuntimeError(
                "生产环境禁止空 SECRET_KEY，请设置环境变量 SECRET_KEY 为 "
                "≥32 位的随机串（如 `openssl rand -hex 32`）。"
            )
        if len(self.SECRET_KEY) < 32:
            raise RuntimeError(
                f"生产环境 SECRET_KEY 长度不足（当前 {len(self.SECRET_KEY)} 位），"
                f"要求 ≥32 位以降低被暴力破解的风险。"
            )
        if self.DEMO_SEED:
            raise RuntimeError(
                "生产环境禁止开启 DEMO_SEED（会自动创建演示账号），请移除该配置。"
            )


settings = Settings()
settings._ensure_dev_secret()
