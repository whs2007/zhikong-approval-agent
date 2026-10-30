"""依赖锁校验（scripts/gen_lock.py --check）的单元测试。

为什么要测：CI 里 `--check` 是**卡住合并**的门禁，一个永远返回 0 的门禁等于没有门禁。
历史 bug 是反向的——它永远返回 1（重新向 PyPI 解析导致版本漂移误报）。
本测试同时覆盖"该过"与"该拦"两侧。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "gen_lock", ROOT / "scripts" / "gen_lock.py")
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """把模块级路径常量重定向到临时目录。"""
    req = tmp_path / "requirements.txt"
    lock = tmp_path / "requirements.lock.txt"
    monkeypatch.setattr(mod, "REQUIREMENTS", req)
    monkeypatch.setattr(mod, "LOCK", lock)
    return req, lock


# ---------- 解析层 ----------

def test_parse_requirements_ignores_comments_and_flags(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text(
        "# 注释行\n"
        "\n"
        "fastapi>=0.110   # 行内注释\n"
        "uvicorn[standard]>=0.27\n"
        "-r other.txt\n",
        encoding="utf-8",
    )
    reqs = mod.parse_requirements(p)
    assert [r.name for r in reqs] == ["fastapi", "uvicorn"]


def test_parse_lock_normalises_names(tmp_path):
    p = tmp_path / "l.txt"
    p.write_text("# 头\nFastAPI==0.162.0\nzope.interface==6.0\n", encoding="utf-8")
    pinned, bad = mod.parse_lock(p)
    assert bad == []
    assert pinned["fastapi"] == "0.162.0"
    assert pinned["zope-interface"] == "6.0"   # 规范化后连字符


def test_parse_lock_flags_bad_lines(tmp_path):
    p = tmp_path / "l.txt"
    p.write_text("fastapi>=0.110\n", encoding="utf-8")
    pinned, bad = mod.parse_lock(p)
    assert pinned == {}
    assert bad == ["fastapi>=0.110"]


# ---------- 校验层：该过的要过 ----------

def test_check_passes_when_consistent(workspace):
    req, lock = workspace
    req.write_text("fastapi>=0.110\nuvicorn>=0.27\n", encoding="utf-8")
    lock.write_text("fastapi==0.162.0\nuvicorn==0.40.0\n", encoding="utf-8")
    assert mod.check() == 0


# ---------- 校验层：该拦的要拦 ----------

def test_check_fails_when_direct_dep_missing(workspace, capsys):
    req, lock = workspace
    req.write_text("fastapi>=0.110\nhttpx>=0.27\n", encoding="utf-8")
    lock.write_text("fastapi==0.162.0\n", encoding="utf-8")
    assert mod.check() == 1
    assert "httpx" in capsys.readouterr().err


def test_check_fails_when_version_violates_constraint(workspace, capsys):
    req, lock = workspace
    req.write_text("fastapi>=0.110\n", encoding="utf-8")
    lock.write_text("fastapi==0.100.0\n", encoding="utf-8")
    assert mod.check() == 1
    err = capsys.readouterr().err
    assert "不满足" in err and "0.100.0" in err


def test_check_fails_when_lock_missing(workspace):
    req, _lock = workspace
    req.write_text("fastapi>=0.110\n", encoding="utf-8")
    assert mod.check() == 1


def test_check_fails_on_malformed_lock_line(workspace, capsys):
    req, lock = workspace
    req.write_text("fastapi>=0.110\n", encoding="utf-8")
    lock.write_text("fastapi==0.162.0\ngarbage line\n", encoding="utf-8")
    assert mod.check() == 1
    assert "非法格式" in capsys.readouterr().err


def test_check_handles_upper_bound(workspace):
    """上界约束也要校验：<2.0 时 pin 到 2.5 应被拦。"""
    req, lock = workspace
    req.write_text("pydantic>=2.5,<3.0\n", encoding="utf-8")
    lock.write_text("pydantic==3.1.0\n", encoding="utf-8")
    assert mod.check() == 1
