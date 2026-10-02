# 现行语言功能样例

本目录从旧 `tests/stage999` 迁移，名称不再暗示“遥远未来的目标阶段”。
这里的程序按现行 Xe 规则验收解析和语义；完整运行行为由相关后端测试与 audit 分层核对。
目录中的成功用例不是全语言支持证明，最新承诺见 [发布契约](../../RELEASE.md)。

- `pointer_permissions.xe` / `mutable_permissions.xe`：写权限显式声明，检查始终开启。
- `pointer_alias.xe` / `borrow.xe`：普通指针读写、地址别名及重复调用，不引入独占借用。
- `string_resource.xe` / `slice_view.xe` / `string_view.xe`：拥有字符串与非拥有视图分工。
- `generic_holder.xe` / `generic_function.xe`：关键字后的泛型声明、名称后的具体代入及字段转送。
- `trait_dispatch.xe` / `trait_copy_drop.xe`：静态 Trait、显式 Copy 和自定义 Drop。
- `struct_create.xe` / `struct_methods.xe` / `struct_move.xe`：字段初始化、接收者和资源转交。
- `enum.xe` / `match_ownership.xe` / `match_multiple.xe`：一层选择、负载处理与拥有/指针匹配。
- `maybe_error.xe` / `channel_expression.xe` / `option.xe`：Maybe、数字通道及上下文 None。
- `block_drop.xe` / `block_drop_control.xe`：作用域析构、资源移出和提前退出清理。
- `anonymous_function.xe` / `closure_capture.xe` / `closure_borrow_alias.xe`：函数值、显式捕获与正文别名。
- `index_values.xe` / `tuple_pointer.xe`：下标结果是 T，显式 @ 才是 T@；元组与视图描述符。
- `box.xe` / `shared.xe`：拥有堆对象、明确增加共享拥有者和弱升级。
- `iterator_step.xe`：`Step[i32?]` 的 `Item[Maybe::None]` 是元素，只有 `Stop` 结束迭代。
- `module_readonly.xe` / `word_bits.xe`：静态只读对象地址及单词位运算。

旧 `borrow` / `option` 文件名保留用于查找设计沿革，不代表 Rust 借用或另有 Option 类型。
`mutable_unchecked` / `pointer_unchecked` 已改名为 permissions，避免暗示可以关闭检查。
类型别名和元组解包的完整用例见 [backend](../backend/README.md)，
线程与状态迭代示例见 [threads](../../examples/threads/README.md)、[iterators](../../examples/iterators/README.md)。

```sh
make check SOURCE=tests/language/enum.xe
make run SOURCE=tests/language/struct_methods.xe
make audit  # 重新生成 target/audit/language.json，不依赖历史审核数字
```

audit 对可识别指针风险保留 warning，默认编译但不运行；不要为了全绿执行悬垂程序。
`result.xe` 从仓库根运行时读取 README；隔离审核目录会走文件不存在的恢复路径。
新增用例须登记真实预期行为与必要反例；静态 Trait、条件 Copy 和泛型 Drop 已实现，
不能把旧目录说明当作这些能力尚未实现的证据。
