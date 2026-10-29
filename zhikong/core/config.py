"""全局配置（环境变量驱动，零硬编码密钥）。

所有敏感/环境相关参数均从环境变量读取，代码库内不含任何明文密钥。
"""
from __future__ import annotations

import os
from pathlib import Path

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
    SECRET_KEY: str = _env("SECRET_KEY", "dev-secret-change-me-in-prod")
    JWT_EXPIRE_MINUTES: int = _env_int("JWT_EXPIRE_MINUTES", 60)
    JWT_ALG: str = "HS256"

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

    # ---- 审批超时（进阶） ----
    APPROVAL_TIMEOUT_HOURS: int = _env_int("APPROVAL_TIMEOUT_HOURS", 24)
    APPROVAL_ESCALATE_HOURS: int = _env_int("APPROVAL_ESCALATE_HOURS", 72)

    # 服务
    HOST: str = _env("HOST", "0.0.0.0")
    PORT: int = _env_int("PORT", 8000)
    CORS_ORIGINS: list[str] = [o.strip() for o in _env("CORS_ORIGINS", "*").split(",") if o.strip()]

    # ----------------------- 生产自检 -----------------------
    _PROD_DEFAULT_SECRET = "dev-secret-change-me-in-prod"

    def is_production(self) -> bool:
        """是否生产环境。ENV ∈ {prod, production} 视为生产。"""
        return self.ENV.strip().lower() in ("prod", "production")

    def validate_for_production(self) -> None:
        """生产环境启动前自检：杜绝使用示例/空/弱密钥上线。

        仅在 is_production() 为 True 时生效；开发与测试环境（默认 ENV=dev）
        始终跳过，不影响 pytest 与本地联调。
        """
        if not self.is_production():
            return
        if not self.SECRET_KEY or self.SECRET_KEY == self._PROD_DEFAULT_SECRET:
            raise RuntimeError(
                "生产环境禁止使用示例/空 SECRET_KEY，请设置环境变量 SECRET_KEY 为 "
                "≥32 位的随机串（如 `openssl rand -hex 32`）。"
            )
        if len(self.SECRET_KEY) < 32:
            raise RuntimeError(
                f"生产环境 SECRET_KEY 长度不足（当前 {len(self.SECRET_KEY)} 位），"
                f"要求 ≥32 位以降低被暴力破解的风险。"
            )


settings = Settings()
