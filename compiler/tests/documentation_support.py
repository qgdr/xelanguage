"""Local historical fixtures are optional in the public Core source package.

Public manuals have separate counted, executable tests. Missing one local
fixture is an error when the private directory exists; only a checkout/archive
without that directory skips the corresponding historical document test.
This helper never selects or changes a language profile.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def private_path(name: str) -> Path:
    directory = ROOT / "internal"
    if not directory.is_dir():
        raise unittest.SkipTest("本地历史资料不随Core源码分发；公开程序另有执行回归")
    path = directory / name
    if not path.exists():
        raise AssertionError(f"本地历史资料遗失：{path}")
    return path


def private_document(name: str) -> Path:
    path = private_path(name)
    if not path.is_file():
        raise AssertionError(f"本地开发文档不是文件：{path}")
    return path


def private_example(name: str) -> Path:
    return private_path(f"examples/{name}")
