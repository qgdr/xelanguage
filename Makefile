.DEFAULT_GOAL := ast

UV ?= uv
SOURCE ?= tests/stage999/maybe_error.xe
OUTPUT ?= target/ast/$(notdir $(SOURCE)).ast.json
CHECK_FLAGS ?=

.PHONY: ast ast-test check check-borrows compiler-test clean

ast:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" -o "$(OUTPUT)"

ast-test:
	$(UV) run --project . --frozen --offline python -m unittest discover -s compiler/tests -v

check:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --check $(CHECK_FLAGS)

check-borrows:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --check --check-borrows $(CHECK_FLAGS)

compiler-test: ast-test

clean:
	find ./tests -name "*.out" -exec rm -f {} +
	find ./tests -name "*.ll" -exec rm -f {} +
	find ./tests -name "*.bc" -exec rm -f {} +
	find ./tests -name "*.ast.json" -exec rm -f {} +
