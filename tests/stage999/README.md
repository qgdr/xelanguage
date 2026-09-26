# stage999

这里保存目标语言的设计样例，其中部分语法尚未由当前 Python 编译器实现。

当一个功能具有实现、成功测试、失败测试和清晰诊断之后，应把对应样例移动到正常阶段目录，
不再把它留在 `stage999`。

- pointer_unchecked.xe：默认模式、无修饰地址及指针写入。
- borrow.xe：检查模式的共享、可写借用。
- channel_expression.xe / option.xe：分支函数调用、参数绑定及 None 简写。
- enum.xe / match_ownership.xe：统一分支、只读借用及按值取得所有权。
- maybe_error.xe：成功直接返回，错误明确构造。
- block_drop.xe / block_drop_control.xe：作用域自动析构、移出值不重复释放及跳转清理。
- branch_pipeline.xe：共享负载指针、数字通道及普通比较。
- match_multiple.xe：多负载处理参数附件。
- anonymous_function.xe：fn 匿名函数和返回已有函数。
- closure_capture.xe：拥有捕获、单次调用和分支返回闭包（后续闭包实现阶段）。

除 pointer_unchecked.xe 外，新样例均按检查模式验收；仅用共享借用的样例也可在默认
模式运行，但不承诺相同的内存安全保证。这里是设计验收约定，尚未新增自动化执行器。
