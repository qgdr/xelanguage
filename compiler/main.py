"""保留 python compiler/main.py 入口，统一使用 compiler 包身份。

从脚本所在目录启动时，Python 默认只将 compiler/ 加入搜索路径。
必须先定位仓库根，避免将 xe_ast 同时加载为顶层包和 compiler 子包。
"""
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compiler.xe_ast.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
