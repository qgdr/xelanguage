# stage999

这里保存目标语言的设计样例，其中部分语法尚未由当前 Python 编译器实现。

当一个功能具有实现、成功测试、失败测试和清晰诊断之后，应把对应样例移动到正常阶段目录，
不再把它留在 `stage999`。

- pointer_unchecked.xe / mutable_unchecked.xe：保留历史文件名，已迁移为有写权限的普通指针与声明。
- borrow.xe / pointer_alias.xe：普通指针读写、地址别名和重复调用，没有独占/再次借用要求。
- generic_holder.xe / generic_function.xe：泛型声明、推导/显式代入、字段转送与具体实例执行。
- channel_expression.xe / option.xe：分支函数调用、参数绑定及 None 简写。
- enum.xe / match_ownership.xe：统一分支、只读借用及按值取得所有权。
- maybe_error.xe：成功直接返回，错误明确构造。
- block_drop.xe / block_drop_control.xe：作用域自动析构、移出值不重复释放及跳转清理。
- branch_pipeline.xe：共享负载指针、数字通道及普通比较。
- match_multiple.xe：多负载处理参数附件。
- anonymous_function.xe：fn 匿名函数和返回已有函数。
- closure_capture.xe：拥有捕获、默认重复只读调用和分支返回闭包，已能编译运行。

全部新样例按统一的检查策略验收；通过语义检查不表示已有后端运行支持。
tuple_pointer.xe 展示 tuple[...] 元组、str 描述符指针、无损转换及显式 Copy。
0.9 的解包与透明别名完整示例见 ../backend/tuples.xe 与 ../backend/type_aliases.xe；
别名不会改变 Copy、资源所有权或指针权限。

使用 make audit 按阶段重新生成 target/audit/stage999.json，不靠过时的静态清单判断能力。
Maybe、Array/Slice、文件结果、无捕获/捕获函数值与具体泛型实例示例已能生成和运行；
仍缺通用 Trait 分派与泛型类型的 Copy/Drop 实现。
闭包迭代示例见 ../../examples/iterators；生成器采用 from_fn，不宣称已支持 yield。
result.xe 已有可执行入口：仓库根读取 README，隔离审核目录验证不存在文件时的恢复。
