.DEFAULT_GOAL := ast

UV ?= uv
ifneq ($(filter emit-c build run,$(MAKECMDGOALS)),)
SOURCE ?= tests/stage999/struct_move.xe
else
SOURCE ?= tests/stage999/maybe_error.xe
endif
OUTPUT ?= target/ast/$(notdir $(SOURCE)).ast.json
CHECK_FLAGS ?=
BACKEND_FLAGS ?=
PROGRAM ?= target/debug/$(basename $(notdir $(SOURCE)))
C_OUTPUT ?= target/c/$(notdir $(SOURCE)).c
CC ?= cc

.PHONY: ast ast-test check check-borrows compiler-test emit-c build run clean

ast:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" -o "$(OUTPUT)"

ast-test:
	$(UV) run --project . --frozen --offline python -m unittest discover -s compiler/tests -v

check:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --check $(CHECK_FLAGS)

check-borrows:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --check --check-borrows $(CHECK_FLAGS)

compiler-test: ast-test

emit-c:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --emit-c -o "$(C_OUTPUT)" $(BACKEND_FLAGS)

build:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --build -o "$(PROGRAM)" --cc "$(CC)" $(BACKEND_FLAGS)

run:
	$(UV) run --project . --frozen --offline python compiler/main.py "$(SOURCE)" --run -o "$(PROGRAM)" --cc "$(CC)" $(BACKEND_FLAGS)

clean:
	find ./tests -name "*.out" -exec rm -f {} +
	find ./tests -name "*.ll" -exec rm -f {} +
	find ./tests -name "*.bc" -exec rm -f {} +
	find ./tests -name "*.ast.json" -exec rm -f {} +
