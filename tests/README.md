# Xe 源码测试目录

这里保存可阅读的 Xe 输入程序；自动化断言在 `compiler/tests/` 的 Python unittest 中。
现行规则从 [语言规范入口](../doc/00.md) 阅读，候选版承诺以 [发布契约](../RELEASE.md) 为准。

- [language](language/README.md)：现行语言的合法功能样例，原 `stage999` 已迁到这里。
- [backend](backend/README.md)：求值顺序、资源清理等实际 C 编译执行样例。
- [fails](fails/README.md)：必须被拒绝的程序，包括语义错误及少量明确的旧语法迁移反例。
- [syntax_fails](syntax_fails/README.md)：专门在词法或解析阶段失败的输入；分类与 fails 不严格互斥。
- [warnings](warnings/README.md)：合法但有指针风险的输入，可编译，不统一执行可能悬垂的地址。
- [unsupported](unsupported/README.md)：用于说明当前能力边界，不把未实现能力说成语言本身非法。
- [legacy](legacy/README.md)：早期阶段设计档案，不要求通过当前解析、语义或执行测试。

`make ast` 只验证语法；`make check` 验证名称、类型、所有权与写权限。
风险 warning 不阻断编译，类型/权限/资源错误才阻断。旧检查旗标不关闭检查，
也不建立 Rust 式独占借用；`T@` 是普通指针。

```sh
make check SOURCE=tests/language/pointer_alias.xe
make run SOURCE=tests/backend/struct_control.xe
make audit  # 写 target/audit/language.json；风险样例默认只编译、不运行
make compiler-test
```

每次增加功能，同时增加成功、边界、失败及可理解诊断的断言。
语义反例必须先成功解析，再核对预期错误编号；不能把崩溃、另一阶段的错误或
空目录 glob 当成测试成功。能生成 AST 不等于语义合法，能生成 C 不等于行为正确。

目录整理只搬迁人工源程序与 README。旧 `.ast.json`、`.ll`、`.out` 等被忽略生成物
不搬迁、不纳入候选源码包；它们不是现行规范，也不是需要恢复的编译器输入。
