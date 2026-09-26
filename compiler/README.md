# 独立 Xe AST 前端

这是新实现，不导入旧 excompiler，不使用 LLVM，前端自身只使用 Python 标准库。
与仓库其余工具共用根目录 uv 项目和 .venv；源码按 [Bootstrap Syntax 0.1](../doc/17.md) 解析。

## 从仓库根运行

```sh
make ast
make ast SOURCE=tests/stage999/enum.xe
make ast SOURCE=tests/stage999/maybe_error.xe OUTPUT=target/ast/result.json
make ast-test

uv run --project . --frozen --offline python compiler/main.py tests/stage999/enum.xe -o target/ast/enum.json
uv run --project . --frozen --offline python compiler/main.py tests/stage999/enum.xe -o -
```

make ast 默认输出 target/ast/<源文件名>.ast.json。-o - 只输出 JSON，不混入状态文字。
统一环境配置为根目录 pyproject.toml / uv.lock，虚拟环境位于根目录 .venv。
首次准备环境运行 uv sync --frozen，之后 make 命令可离线使用已经安装的锁定依赖。
根目录现有的历史编译器依赖继续保留，但新前端不导入它们。

Makefile 已固定 --project . --frozen --offline，使用根目录 uv.lock，不更新依赖或访问网络。
环境需满足根项目的 Python 3.13+ 要求；新前端代码仍可在 Python 3.12 下单独测试。
compiler/ 不再含独立项目配置；曾生成的 compiler/.venv 不会再被这些命令使用。

## 模块边界

- source.py：原文、行列、半开范围及 Diagnostic；
- lexer.py：字面量、最长标点、嵌套注释；1> / 2> 不使用全局特殊 token；
- parser.py：声明/类型/块递归解析，表达式按优先级解析；
- ast.py：JSON schema_version = 1 的信封；
- cli.py：UTF-8 文件输入、诊断、原子 JSON 输出；
- tests/：AST 形状、源码范围、失败诊断和命令行验收。

每个 AST 节点有 kind、span 和该节点的字段。start 包含、end 不包含；
offset 是 Unicode 码点位置，line/column 从 1 开始。数值字面量同时保留 raw，避免大整数
在某些 JSON 消费者中失去原文精度。注释保存在 Module.comments，文档注释有独立标记。

表达式 [] 保留 BracketApply；这里无法仅凭源码判断它是索引、枚举负载还是泛型应用。
HandlerBinding 与 AnonymousFunction 分开保存：前者 return 属于外层，后者有独立函数作用域。
比较链保留 ComparisonChain，不误解析成嵌套布尔值比较。

## 已实现与未实现

实现冻结语法的单文件解析和 JSON 输出；也保留待实现的借用修饰及捕获语法的 AST。
没有名称解析、泛型实例化、类型检查、所有权/生命周期检查、Drop 插入、运行或机器码生成。
因此语义错误样例能生成 AST 并不意味着合法；不应把这个阶段叫完整的 xe check。

当前报第一个源码错误并停止，不写部分成功 AST；多错误恢复将在后续前端阶段加入。
语法旧写法应当报迁移提示，不静默转换。输出同路径原子替换；失败保留已有产物。
对同名但不同目录输入，请用 -o 指定不同路径，避免默认输出名碰撞。

## 后续实现顺序

1. 名称与模块解析；
2. 类型与 Maybe 返回简写降低；
3. 初始化、移动及确定性 Drop；
4. 可选借用检查；
5. HIR/MIR 与 C 后端；
6. 用 Xe 编写同等功能前端，完成自举固定点验证。
