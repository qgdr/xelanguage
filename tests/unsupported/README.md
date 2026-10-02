# 合法语法但暂未支持的能力

这些文件不能当成“语言设计上不合法”的程序。它们必须成功生成 AST，
但当前语义阶段报告明确的能力边界，不伪装成已支持。

静态 Trait 的声明、方法契约、泛型约束和直接分派已实现；trait_dispatch.xe
已迁移到 ../language/trait_dispatch.xe。
已支持的 Copy/Drop 正例见 tests/language/trait_copy_drop.xe，
违例见 tests/fails/trait_*.xe。

泛型 Holder 的具体实例、显式函数代入和字段转送已可编译运行，原 generic_holder.xe
已迁移到 ../language/generic_holder.xe。条件 Copy 与泛型 Drop 已实现，
动态 Trait 对象、关联类型和重叠特化不在当前范围，详见 ../../doc/35.md。
