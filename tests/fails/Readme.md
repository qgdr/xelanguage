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

borrow_move_resource.xe、borrow_match_move.xe：两种模式均禁止借指针移走资源。
pointer_owned_match.xe：新式指针匹配必须显式写 ?[@]。
channel_missing_parameter.xe：禁止无参数箭头，提示改为 _ -> expression 或零参数函数。
none_error_result.xe：None 不能代替具体错误负载。
borrow_alias.xe 等借用安全诊断在 --check-borrows 模式验收。

branch_value_target.xe：分支目标必须可调用，不能直接写值。
branch_call_target.xe：不再接受 foo(_) 负载占位调用。
borrow_scalar_parameter.xe：共享匹配的整数负载也是指针，类型注解不改变传递方式。
closure_without_fn.xe：脱离管道的 x -> 正文不是闭包表达式，必须使用 fn。
closure_capture_copy.xe：拥有捕获闭包是移动类型，不允许用 = 复制。
