# 独立 Xe 编译器：AST、语义检查和第一版 C 后端

这是当前编译器实现，不使用 LLVM；Python 实现只使用标准库，
构建可执行程序时调用系统 GCC/Clang 兼容的 C 编译器。
与仓库其余工具共用根目录 uv 项目和 .venv；源码按 [Bootstrap Syntax 0.2](../doc/17.md) 解析。

## 从仓库根运行

```sh
make ast
make ast SOURCE=tests/stage999/enum.xe
make ast SOURCE=tests/stage999/maybe_error.xe OUTPUT=target/ast/result.json
make ast-test
make check SOURCE=tests/fails/borrow_match_move.xe
make check-borrows SOURCE=tests/stage999/enum.xe
make compiler-test
make run
make run SOURCE=tests/stage999/struct_methods.xe BACKEND_FLAGS=--check-borrows
make build SOURCE=tests/stage999/struct_move.xe
make emit-c SOURCE=tests/stage999/struct_move.xe

uv run --project . --frozen --offline python compiler/main.py tests/stage999/enum.xe -o target/ast/enum.json
uv run --project . --frozen --offline python compiler/main.py tests/stage999/enum.xe -o -
```

make ast 默认输出 target/ast/<源文件名>.ast.json。-o - 只输出 JSON，不混入状态文字。
统一环境配置为根目录 pyproject.toml / uv.lock，虚拟环境位于根目录 .venv。
首次准备环境运行 uv sync --frozen，之后 make 命令可离线使用已经安装的锁定依赖。
Python 项目不需要第三方依赖；旧版专用的 ply、llvmlite 等依赖已移除。
旧编译器源码已移除并忽略，不影响本目录的入口 compiler/main.py。

Makefile 已固定 --project . --frozen --offline，使用根目录 uv.lock，不更新依赖或访问网络。
环境需满足根项目的 Python 3.13+ 要求；新前端代码仍可在 Python 3.12 下单独测试。
compiler/ 不再含独立项目配置；曾生成的 compiler/.venv 不会再被这些命令使用。

## 模块边界

- source.py：原文、行列、半开范围及 Diagnostic；
- lexer.py：字面量、最长标点、嵌套注释；1> / 2> 不使用全局特殊 token；
- parser.py：声明/类型/块递归解析，表达式按优先级解析；
- ast.py：JSON schema_version = 1 的信封；
- typesys.py：语义类型及类型变量替换，与 AST 分离；
- semantic.py：单文件名称、类型、移动、分支及基础借用检查；
- backend_c.py：语义类型侧表、C 降低、结构体方法及资源清理；
- build.py：生成 C、调用系统编译器和原子发布程序；
- runtime/xe_runtime.h：小型字符串、输出及数值运行库；
- cli.py：UTF-8 文件输入、诊断、原子 JSON 输出；
- tests/：AST 形状、源码范围、失败诊断和命令行验收。

每个 AST 节点有 kind、span 和该节点的字段。start 包含、end 不包含；
offset 是 Unicode 码点位置，line/column 从 1 开始。数值字面量同时保留 raw，避免大整数
在某些 JSON 消费者中失去原文精度。注释保存在 Module.comments，文档注释有独立标记。

表达式 [] 保留 BracketApply；这里无法仅凭源码判断它是索引、枚举负载还是泛型应用。
HandlerBinding 与 AnonymousFunction 分开保存：前者 return 属于外层，后者有独立函数作用域。
比较链保留 ComparisonChain，不误解析成嵌套布尔值比较。

## 已实现与未实现

实现冻结语法的单文件解析和 JSON 输出，以及第一版单文件语义检查。
make ast 仍然只解析；make check 检查名称、类型、初始化和移动，
make check-borrows 再启用基础别名/生命周期检查。两种检查均禁止通过指针移走资源。
前端标准接口仍有未实现的运行部分；第一版 C 后端已运行结构体、方法、String 和 Drop，
尚无完整模块/泛型/Trait 或完整后端覆盖。
支持范围、数据结构和维护方法见 [第 18 章](../doc/18.md)，不将基础借用检查称为完整安全证明。
构建命令、资源清理、运行验收及暂未支持功能见 [第 19 章](../doc/19.md)。

解析报第一个源码错误并停止；语义检查每函数报第一个主错误，继续检查其他函数。
不写部分成功 AST；解析多错误恢复将在后续前端阶段加入。
语法旧写法应当报迁移提示，不静默转换。输出同路径原子替换；失败保留已有产物。
对同名但不同目录输入，请用 -o 指定不同路径，避免默认输出名碰撞。

## 后续实现顺序

1. 模块加载、通用泛型及静态 Trait 能力；
2. 有类型 HIR 和控制流 MIR，显式降低 Maybe、管道及比较链；
3. 确定性 Drop 清理点和更准确的区域/借用检查；
4. C 后端、小运行库和可执行标准库；
5. 用 Xe 编写同等功能前端，完成自举固定点验证。
