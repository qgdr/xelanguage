.DEFAULT_GOAL := ast

UV ?= uv
# Default never enables the unreviewed toy language, even for old sample paths.
# Use LANGUAGE_PROFILE=legacy explicitly to run archived examples.
LANGUAGE_PROFILE ?= core
LANGUAGE_FLAGS = --language-profile=$(LANGUAGE_PROFILE)
SOURCE ?= examples/core/main.xe
OUTPUT ?= target/ast/$(notdir $(SOURCE)).ast.json
CHECK_FLAGS ?=
# 类型、权限/所有权始终检查；core已知词法来源逃逸报错，其余风险不等于已证明安全。
# --check-safety / --check-borrows 仅兼容旧命令，不建立独占借用。
BACKEND_FLAGS ?=
# 作为 shell 实参传入，例如 ARGS='echo "你好 Xe"'；不要放编译器选项。
ARGS ?=
PROGRAM ?= target/debug/$(basename $(notdir $(SOURCE)))
C_OUTPUT ?= target/c/$(notdir $(SOURCE)).c
CC ?= cc
LEAN ?= lean

.PHONY: ast ast-test check check-safety check-borrows compiler-test core-test release-test python-check proof-check emit-c build run demo legacy-stdlib-test audit legacy-bootstrap legacy-bootstrap-sanitize legacy-bootstrap-test xe toolchain-test clean release-check release-package release-smoke planner planner-check planner-sanitize

# 新统一工具；旧目标保留原路径/默认值兼容性。
XE_ARGS ?= --help
xe:
	$(UV) run --project . --frozen --offline python -m compiler $(XE_ARGS)

toolchain-test:
	$(UV) run --project . --frozen --offline python -m unittest compiler.tests.test_toolchain -v

# 与 VS Code 的项目配置一致，不与 Xe 源码的 check/lint 混淆。
python-check:
	$(UV) run --project . --frozen --offline pyright compiler bootstrap/verify.py $(wildcard internal/formal/lean/check.py)
	$(UV) run --project . --frozen --offline ruff check compiler bootstrap $(wildcard internal/formal)

# 固定工具链重查每个证明；.olean、摘要和报告只写target/lean。
proof-check:
	$(if $(wildcard internal/formal/lean/check.py),,$(error proof-check needs local development material: internal/formal/lean/check.py; not included in Core Preview))
	$(UV) run --project . --frozen --offline python internal/formal/lean/check.py --lean "$(LEAN)"

# 公开的Core示例；差分验证器与对照源码仅在本地internal/保留。
planner:
	$(UV) run --project . --frozen --offline python -m compiler run examples/dependency-planner/main.xe --cc "$(CC)"

planner-check:
	$(if $(wildcard internal/examples/dependency_planner/verify.py),,$(error planner-check needs local development material: internal/examples/dependency_planner/verify.py; not included in Core Preview))
	$(UV) run --project . --frozen --offline python internal/examples/dependency_planner/verify.py --cc "$(CC)"

planner-sanitize:
	$(if $(wildcard internal/examples/dependency_planner/verify.py),,$(error planner-sanitize needs local development material: internal/examples/dependency_planner/verify.py; not included in Core Preview))
	$(UV) run --project . --frozen --offline python internal/examples/dependency_planner/verify.py --cc "$(CC)" --sanitize

# 从唯一版本来源取包名；SemVer 与 Python 的 PEP 440 拼写不混用。
RELEASE_VERSION = $(shell $(UV) run --project . --frozen --offline python -c 'from compiler.version import VERSION; print(VERSION)')
RELEASE_DIR ?= target/releases
RELEASE_FLAGS ?=
RELEASE_ARCHIVE = $(RELEASE_DIR)/xelanguage-$(RELEASE_VERSION)-source.tar.gz

release-package:
	$(UV) run --project . --frozen --offline python -m compiler.release package --output-dir "$(RELEASE_DIR)" $(RELEASE_FLAGS)

release-smoke:
	$(UV) run --project . --frozen --offline python -m compiler.release smoke "$(RELEASE_ARCHIVE)" --cc "$(CC)" $(LANGUAGE_FLAGS)

# 顺序执行，即使用 make -j 也不能在验收完成前打包快照。
# 全量回归保留显式legacy实现的维护检查，不能据此宣布它们是Core能力。
# Core用例和包外默认Core验收包含ASan/UBSan/default LSan。
# 不关闭 sanitizer，不修改 Git，不发布到外部平台。
release-check:
	$(UV) lock --project . --check --offline
	$(MAKE) python-check
	$(MAKE) compiler-test
	$(MAKE) release-package
	$(MAKE) release-smoke LANGUAGE_PROFILE=core

core-test:
	$(UV) run --project . --frozen --offline python -m unittest discover -s compiler/tests -p 'test_core_*.py' -v

release-test:
	$(UV) run --project . --frozen --offline python -m unittest compiler.tests.test_release compiler.tests.test_release_metadata compiler.tests.test_release_ci -v

ast:
	$(UV) run --project . --frozen --offline python compiler/main.py $(LANGUAGE_FLAGS) "$(SOURCE)" -o "$(OUTPUT)"

ast-test:
	$(UV) run --project . --frozen --offline python -m unittest discover -s compiler/tests -v

check:
	$(UV) run --project . --frozen --offline python compiler/main.py $(LANGUAGE_FLAGS) "$(SOURCE)" --check $(CHECK_FLAGS)

check-borrows:
	$(UV) run --project . --frozen --offline python compiler/main.py $(LANGUAGE_FLAGS) "$(SOURCE)" --check --check-borrows $(CHECK_FLAGS)

check-safety:
	$(UV) run --project . --frozen --offline python compiler/main.py $(LANGUAGE_FLAGS) "$(SOURCE)" --check --check-safety $(CHECK_FLAGS)

compiler-test: ast-test

emit-c:
	$(UV) run --project . --frozen --offline python compiler/main.py $(LANGUAGE_FLAGS) "$(SOURCE)" --emit-c -o "$(C_OUTPUT)" $(BACKEND_FLAGS)

build:
	$(UV) run --project . --frozen --offline python compiler/main.py $(LANGUAGE_FLAGS) "$(SOURCE)" --build -o "$(PROGRAM)" --cc "$(CC)" $(BACKEND_FLAGS)

run:
	$(UV) run --project . --frozen --offline python compiler/main.py $(LANGUAGE_FLAGS) "$(SOURCE)" --run -o "$(PROGRAM)" --cc "$(CC)" $(BACKEND_FLAGS) -- $(ARGS)

# 默认演示只运行公开Core，不会按源码路径悄悄切换legacy。
demo:
	$(UV) run --project . --frozen --offline python -m compiler --language-profile=core run examples/core/main.xe --cc "$(CC)" -o target/debug/core-demo

legacy-stdlib-test:
	$(UV) run --project . --frozen --offline python -m unittest compiler.tests.test_stdlib_semantic compiler.tests.test_stdlib_io compiler.tests.test_stdio_terminal compiler.tests.test_stdlib_env compiler.tests.test_feature_check_project -v

# AST、语义、生成C、系统编译及实际运行分层审核；报告不是完整规范的证明。
audit:
	$(UV) run --project . --frozen --offline python compiler/audit.py tests/language -o target/audit/language.json --cc "$(CC)" $(LANGUAGE_FLAGS)

# Xe 源码由上一代 Xe 可执行文件解析和发射；Python 只引导 seed 并调用系统 cc。
legacy-bootstrap:
	$(if $(wildcard bootstrap/verify.py),,$(error legacy-bootstrap needs bootstrap/verify.py))
	$(UV) run --project . --frozen --offline python bootstrap/verify.py --cc "$(CC)" --language-profile=legacy

legacy-bootstrap-sanitize:
	$(if $(wildcard bootstrap/verify.py),,$(error legacy-bootstrap-sanitize needs bootstrap/verify.py))
	$(UV) run --project . --frozen --offline python bootstrap/verify.py --cc "$(CC)" --sanitize --language-profile=legacy

legacy-bootstrap-test:
	$(UV) run --project . --frozen --offline python -m unittest compiler.tests.test_selfhost -v

clean:
	$(UV) run --project . --frozen --offline python -m compiler clean "$(SOURCE)"
