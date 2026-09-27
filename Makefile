.DEFAULT_GOAL := ast

UV ?= uv
ifneq ($(filter emit-c build run,$(MAKECMDGOALS)),)
SOURCE ?= tests/stage999/struct_move.xe
else
SOURCE ?= tests/stage999/maybe_error.xe
endif
OUTPUT ?= target/ast/$(notdir $(SOURCE)).ast.json
CHECK_FLAGS ?=
# 类型、写权限及所有权始终检查；普通指针风险只 warning。
# --check-safety / --check-borrows 仅兼容旧命令，不建立独占借用。
BACKEND_FLAGS ?=
PROGRAM ?= target/debug/$(basename $(notdir $(SOURCE)))
C_OUTPUT ?= target/c/$(notdir $(SOURCE)).c
CC ?= cc

.PHONY: ast ast-test check check-safety check-borrows compiler-test emit-c build run demo audit clean

ast:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" -o "$(OUTPUT)"

ast-test:
	$(UV) run --project . --frozen --offline python -m unittest discover -s compiler/tests -v

check:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --check $(CHECK_FLAGS)

check-borrows:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --check --check-borrows $(CHECK_FLAGS)

check-safety:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --check --check-safety $(CHECK_FLAGS)

compiler-test: ast-test

emit-c:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --emit-c -o "$(C_OUTPUT)" $(BACKEND_FLAGS)

build:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --build -o "$(PROGRAM)" --cc "$(CC)" $(BACKEND_FLAGS)

run:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --run -o "$(PROGRAM)" --cc "$(CC)" $(BACKEND_FLAGS)

# 可读的真实项目；从仓库根运行，表达式文件路径相对于根目录。
demo:
	$(UV) run --project . --frozen --offline python compiler/main.py examples/calculator/main.xe --run -o target/debug/calculator --cc "$(CC)"

# AST、语义、生成C、系统编译及实际运行分层审核；报告不是完整规范的证明。
audit:
	$(UV) run --project . --frozen --offline python compiler/audit.py tests/stage999 -o target/audit/stage999.json --cc "$(CC)"

clean:
	find ./tests -name "*.out" -exec rm -f {} +
	find ./tests -name "*.ll" -exec rm -f {} +
	find ./tests -name "*.bc" -exec rm -f {} +
	find ./tests -name "*.ast.json" -exec rm -f {} +
