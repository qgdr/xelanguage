"""从仓库根运行：uv run python compiler/main.py SOURCE -o OUTPUT。"""
from xe_ast.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
