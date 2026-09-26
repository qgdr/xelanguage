# 合法语法但暂未支持的能力

这些文件不能当成“语言设计上不合法”的程序。它们必须成功生成 AST，
但当前语义阶段报告明确的能力边界，不伪装成已支持。

trait_dispatch.xe 验证自定义 Trait 声明、方法签名及实现体的 AST；
当前通用 Trait 检查/分派未完成，make check-borrows 报 XE-SEM-0001。
已支持的 Copy/Drop 正例见 tests/stage999/trait_copy_drop.xe，
违例见 tests/fails/trait_*.xe。
