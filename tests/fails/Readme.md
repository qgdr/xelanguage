# 失败测试

本目录中的每个 `.xe` 文件都必须编译失败。完整测试工具不应只检查退出码，还应检查：

- 稳定的错误代码；
- 主错误源码范围；
- 关键说明文字；
- 必要时给出的关联源码位置；
- 建议修改能够实际应用，而不是泛泛提示。

失败测试不得因为编译器崩溃、断言或 Python 异常而“通过”。

`format_move.xe` 专门固定一条一致性规则：格式化调用不会替调用者隐式借用移动值，诊断应
建议根据意图改成 `value@`，或者不再使用已经移动的值。

borrow_move_resource.xe、borrow_match_move.xe：禁止借指针移走资源。
pointer_owned_match.xe：新式指针匹配必须显式写 ?[@]。
channel_missing_parameter.xe：禁止无参数箭头，提示改为 _ -> expression 或零参数函数。
none_error_result.xe：None 不能代替具体错误负载。
旧 borrow_alias.xe 已迁移到 stage999/pointer_alias.xe：普通指针别名合法。
旧 semantic_return_local_borrow.xe 已迁移到 warnings/return_local_pointer.xe：风险 warning 不阻断编译。
str_pointer.xe 验证只读 str@ 不能修改描述符。
struct_copy_not_declared.xe 验证用户类型没有显式 Copy 时不能使用 =。
pointer_weakened_write.xe 验证可写指针降为只读后不能修改所指内容（XE-MUT-0001）。
generic_resource_copy.xe 验证具体 String 实例不能用 = 复制资源（XE-OWN-0001）。
generic_unresolved_enum.xe 验证枚举构造必须有具体类型附件或上下文（XE-GENERIC-0001）。
type_alias_cycle.xe 验证透明别名循环有明确引用链诊断（XE-TYPE-0007）。
type_alias_resource_copy.xe 验证 String 的别名不能凭空获得 Copy（XE-OWN-0001）。
tuple_unpack_borrow_resource.xe 验证解包不能从非拥有指针盗取资源（XE-MOVE-0002）。
tuple_unpack_size.xe 验证解包成员数必须与源元组一致（XE-TYPE-0001）。

branch_value_target.xe：分支目标必须可调用，不能直接写值。
branch_call_target.xe：不再接受 foo(_) 负载占位调用。
borrow_scalar_parameter.xe：共享匹配的整数负载也是指针，类型注解不改变传递方式。
closure_without_fn.xe：脱离管道的 x -> 正文不是闭包表达式，必须使用 fn。
closure_capture_copy.xe：拥有捕获闭包是移动类型，不允许用 = 复制。
