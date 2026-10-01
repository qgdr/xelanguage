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
# 作为 shell 实参传入，例如 ARGS='echo "你好 Xe"'；不要放编译器选项。
ARGS ?=
PROGRAM ?= target/debug/$(basename $(notdir $(SOURCE)))
C_OUTPUT ?= target/c/$(notdir $(SOURCE)).c
CC ?= cc

.PHONY: ast ast-test check check-safety check-borrows compiler-test python-check emit-c build run demo iterator-demo thread-demo source-scan feature-tool feature-run feature-check stdlib-test audit bootstrap bootstrap-sanitize bootstrap-test xe toolchain-test toolchain-demo clean

# 新统一工具；旧目标保留原路径/默认值兼容性。
XE_ARGS ?= --help
xe:
	$(UV) run --project . --frozen --offline python -m compiler $(XE_ARGS)

toolchain-test:
	$(UV) run --project . --frozen --offline python -m unittest compiler.tests.test_toolchain -v

# 与 VS Code 的项目配置一致，不与 Xe 源码的 check/lint 混淆。
python-check:
	$(UV) run --project . --frozen --offline pyright
	$(UV) run --project . --frozen --offline ruff check compiler bootstrap

toolchain-demo:
	$(UV) run --project . --frozen --offline python -m compiler run --manifest-path examples/toolchain

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
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --run -o "$(PROGRAM)" --cc "$(CC)" $(BACKEND_FLAGS) -- $(ARGS)

# 可读的真实项目；从仓库根运行，表达式文件路径相对于根目录。
demo:
	$(UV) run --project . --frozen --offline python compiler/main.py examples/calculator/main.xe --run -o target/debug/calculator --cc "$(CC)"

iterator-demo:
	$(UV) run --project . --frozen --offline python compiler/main.py examples/iterators/main.xe --run -o target/debug/iterators --cc "$(CC)" $(BACKEND_FLAGS)

thread-demo:
	$(UV) run --project . --frozen --offline python compiler/main.py examples/threads/main.xe --run -o target/debug/threads --cc "$(CC)" $(BACKEND_FLAGS)

# 真正的跨文件包；INPUT / REPORT 覆盖扫描路径，输出不是编译器产物。
INPUT ?= examples/source_scan/src/main.xe
REPORT ?= target/debug/words.txt
source-scan:
	$(UV) run --project . --frozen --offline python compiler/main.py examples/source_scan/src/main.xe --run -o target/debug/source-scan --cc "$(CC)" $(BACKEND_FLAGS) -- "$(INPUT)" "$(REPORT)"

# 无参数交互使用；传入 check 子命令可直接自动验收，不等待 stdin。
feature-tool:
	$(UV) run --project . --frozen --offline python compiler/main.py examples/feature_check/main.xe --build -o target/debug/xe-feature-check --cc "$(CC)" $(BACKEND_FLAGS)

feature-run: feature-tool
	./target/debug/xe-feature-check $(ARGS)

feature-check: feature-tool
	./target/debug/xe-feature-check check

stdlib-test:
	$(UV) run --project . --frozen --offline python -m unittest compiler.tests.test_stdlib_semantic compiler.tests.test_stdlib_io compiler.tests.test_stdio_terminal compiler.tests.test_stdlib_env compiler.tests.test_feature_check_project -v

# AST、语义、生成C、系统编译及实际运行分层审核；报告不是完整规范的证明。
audit:
	$(UV) run --project . --frozen --offline python compiler/audit.py tests/stage999 -o target/audit/stage999.json --cc "$(CC)"

# Xe 源码由上一代 Xe 可执行文件解析和发射；Python 只引导 seed 并调用系统 cc。
bootstrap:
	$(UV) run --project . --frozen --offline python bootstrap/verify.py --cc "$(CC)"

bootstrap-sanitize:
	$(UV) run --project . --frozen --offline python bootstrap/verify.py --cc "$(CC)" --sanitize

bootstrap-test:
	$(UV) run --project . --frozen --offline python -m unittest compiler.tests.test_selfhost -v

clean:
	find ./tests -name "*.out" -exec rm -f {} +
	find ./tests -name "*.ll" -exec rm -f {} +
	find ./tests -name "*.bc" -exec rm -f {} +
	find ./tests -name "*.ast.json" -exec rm -f {} +
