"""P0-4 生产加固测试：密钥合规自检 + 生产跳过弱口令种子 + init-admin 冷启动引导。

这些测试刻意构造 Settings 实例并 monkeypatch 服务模块持有的 settings/连接，
避免污染全局单例与既有测试数据。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from core.config import Settings
from services import auth_service as au


# ----------------------- 1) 生产密钥合规自检 -----------------------

def test_validate_prod_blocks_code_default_key():
    s = Settings()
    s.ENV = "prod"
    s.SECRET_KEY = "dev-secret-change-me-in-prod"  # 代码默认值
    with pytest.raises(RuntimeError):
        s.validate_for_production()


def test_validate_prod_blocks_empty_key():
    s = Settings()
    s.ENV = "prod"
    s.SECRET_KEY = ""
    with pytest.raises(RuntimeError):
        s.validate_for_production()


def test_validate_prod_blocks_short_key():
    s = Settings()
    s.ENV = "prod"
    s.SECRET_KEY = "tooshort"
    with pytest.raises(RuntimeError):
        s.validate_for_production()


def test_validate_prod_ok_with_strong_key():
    s = Settings()
    s.ENV = "prod"
    s.SECRET_KEY = "a" * 48  # ≥32 位随机串
    s.validate_for_production()  # 不应抛异常


def test_dev_skips_validation_with_default_key():
    s = Settings()
    s.ENV = "dev"
    s.SECRET_KEY = "dev-secret-change-me-in-prod"
    s.validate_for_production()  # dev 下跳过，不应抛异常


def test_is_production_variants():
    assert Settings().is_production() is False
    s = Settings(); s.ENV = "production"
    assert s.is_production() is True
    s2 = Settings(); s2.ENV = "PROD"
    assert s2.is_production() is True


# ----------------------- 2) 生产跳过弱口令种子 -----------------------

def _point_auth_to(tmp_path, monkeypatch, env="dev"):
    s = Settings()
    s.ENV = env
    s.DB_URL = f"sqlite:///{tmp_path / 'zhikong_test.db'}"
    monkeypatch.setattr(au, "settings", s)
    monkeypatch.setattr(au, "_conn", None)
    return s


def test_seed_skipped_in_prod(monkeypatch, tmp_path):
    _point_auth_to(tmp_path, monkeypatch, env="prod")
    au._seed()
    assert au.get_by_username("admin") is None
    assert au.get_user_count() == 0


def test_seed_runs_in_dev(monkeypatch, tmp_path):
    _point_auth_to(tmp_path, monkeypatch, env="dev")
    au._seed()
    assert au.get_by_username("admin") is not None
    assert au.get_user_count() == 3


# ----------------------- 3) init-admin 冷启动引导 -----------------------

def test_init_admin_bootstrap_then_locked(monkeypatch, tmp_path):
    """零用户时开放引导；已有用户后未带管理员令牌必须 403。"""
    _point_auth_to(tmp_path, monkeypatch, env="prod")  # 生产：默认不种子
    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as c:
        # 0 用户 -> 开放冷启动引导（init-admin 为查询参数）
        r = c.post("/api/v1/auth/init-admin",
                   params={"username": "boss", "password": "StrongPass!23", "display_name": "Boss"})
        assert r.status_code == 200, r.text
        assert au.get_user_count() == 1

        # 已有管理员 -> 无令牌再次调用应被拒
        r2 = c.post("/api/v1/auth/init-admin",
                    params={"username": "boss2", "password": "x" * 24})
        assert r2.status_code == 403, r2.text

        # 用刚刚引导的管理员登录，再创建第二个管理员应成功
        login = c.post("/api/v1/auth/login",
                      json={"username": "boss", "password": "StrongPass!23"})
        tok = login.json()["data"]["access_token"]
        r3 = c.post("/api/v1/auth/init-admin",
                    params={"username": "boss2", "password": "YyStr0ng!Admin2"},
                    headers={"Authorization": f"Bearer {tok}"})
        assert r3.status_code == 200, r3.text
        assert au.get_user_count() == 2


# ----------------------- 4) 弱口令治理反例 -----------------------

def test_init_admin_rejects_weak_passwords(monkeypatch, tmp_path):
    """反例（缺口①）：常见词/纯数字/纯字母/过短/低多样性口令必须 400 拒绝且不落库。"""
    _point_auth_to(tmp_path, monkeypatch, env="prod")  # 0 用户，避免 403 干扰
    from fastapi.testclient import TestClient
    from api.main import app

    weak = [
        "admin123",   # 常见弱口令字典
        "12345678",   # 纯数字
        "abcdefgh",   # 纯字母
        "Ab1!",       # 过短（<8）
        "aaaa1111",   # 字符多样性不足
    ]
    with TestClient(app) as c:
        for pw in weak:
            r = c.post("/api/v1/auth/init-admin",
                       params={"username": "boss", "password": pw, "display_name": "Boss"})
            assert r.status_code == 400, (pw, r.text)
    # 没有任何弱口令账号被创建
    assert au.get_user_count() == 0


def test_init_admin_accepts_strong_password(monkeypatch, tmp_path):
    """正例（缺口①）：强口令可正常创建管理员。"""
    _point_auth_to(tmp_path, monkeypatch, env="prod")
    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as c:
        r = c.post("/api/v1/auth/init-admin",
                   params={"username": "boss", "password": "YyStr0ng!Boss3", "display_name": "Boss"})
        assert r.status_code == 200, r.text
    assert au.get_user_count() == 1


# ----------------------- 5) init-admin 并发原子性 -----------------------

def test_create_first_admin_concurrent_atomic(monkeypatch, tmp_path):
    """反例（缺口②）：并发调用原子引导函数，最终仅一个管理员，无异常/无重复。"""
    _point_auth_to(tmp_path, monkeypatch, env="prod")  # 0 用户
    import threading

    errors = []

    def worker():
        try:
            au.create_first_admin_if_absent("boss", "YyStr0ng!Boss3", "Boss")
        except Exception as e:  # noqa: BLE001 - 收集并发异常
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert au.get_user_count() == 1
