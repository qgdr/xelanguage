# C 后端运行验收

这里的示例经过 AST、语义检查、C 生成、系统 C 编译器和实际执行。
输出及退出码断言位于 compiler/tests/test_backend.py，不是只看“编译成功”。

    make run SOURCE=tests/backend/struct_control.xe BACKEND_FLAGS=--check-safety
    make run SOURCE=tests/backend/struct_partial_move.xe BACKEND_FLAGS=--check-safety
    make run SOURCE=tests/backend/evaluation_order.xe BACKEND_FLAGS=--check-safety
    make run SOURCE=tests/backend/enum_pipeline.xe BACKEND_FLAGS=--check-safety
    make run SOURCE=tests/backend/enum_resources.xe BACKEND_FLAGS=--check-safety
    make run SOURCE=tests/backend/generic_instances.xe
    make run SOURCE=tests/backend/tuples.xe
    make run SOURCE=tests/backend/type_aliases.xe
    make compiler-test

覆盖 return/break/continue 清理、条件移动、字段部分移动和实际求值顺序。
已有 language/struct_create.xe、struct_methods.xe、block_drop.xe 等同样参加运行验收。

enum_pipeline.xe 展示枚举载荷、借用匹配和拥有管道；enum_resources.xe 验证嵌套
堆字符串、通配分支及忽略载荷的清理。两者还经过 ASan/UBSan 内存与未定义行为检测。
管道与枚举专项运行测试位于 compiler/tests/test_backend_branches.py；
language/pipe.xe 和 enum.xe 也实际执行；检查始终开启，旧旗标不改变规则。

compiler/tests/test_backend_results.py 覆盖 Maybe/传播/panic、数值转换、Array/Slice、
范围端点和 File。examples/calculator 是真实读取文件的优先级解释器，运行 make demo。
0.9 沿用既有约定：Copy 值必须使用 =，不可复制值使用 <<；>> 同时支持普通值和资源。
用户结构体/枚举必须显式 impl Copy；T@ / T@[mut] 都复制地址，允许别名，不复制对象。

generic_instances.xe 验证同一函数和结构体的整数/String 实例、Copy 约束、
泛型枚举的资源载荷匹配以及显式只读指针代入；输出为 `42 hello payload 7`。
泛型实例的 C 执行和资源清理由 compiler/tests/test_generic_backend.py 验证，
类型附件、所有权、缓存与 unsafe 传播的交互见 test_generic_integration.py。
test_pointer_weakening.py 另行验证浅层 T@[mut] -> T@、嵌套转换拒绝与真实地址读写。

tuples.xe 展示 tuple 类型/值/单元素、let 解包、已有变量赋值、通用 >> 和资源 _。
type_aliases.xe 展示前向别名链、元组/函数/String 的透明别名；Text 仍与 String 一样使用 <<。
两者均已通过五阶段执行审核；专门的内存检查和求值/Drop 次数验收位于
compiler/tests/test_tuple_backend.py 与 test_tuple_alias_integration.py。
