# C 后端运行验收

这里的示例经过 AST、语义检查、C 生成、系统 C 编译器和实际执行。
输出及退出码断言位于 compiler/tests/test_backend.py，不是只看“编译成功”。

    make run SOURCE=tests/backend/struct_control.xe BACKEND_FLAGS=--check-borrows
    make run SOURCE=tests/backend/struct_partial_move.xe BACKEND_FLAGS=--check-borrows
    make run SOURCE=tests/backend/evaluation_order.xe BACKEND_FLAGS=--check-borrows
    make compiler-test

覆盖 return/break/continue 清理、条件移动、字段部分移动和实际求值顺序。
已有 stage999/struct_create.xe、struct_methods.xe、block_drop.xe 等同样参加运行验收。
