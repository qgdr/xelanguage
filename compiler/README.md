# 独立 Xe 编译器：AST、语义检查和第一版 C 后端

这是当前编译器实现，不使用 LLVM；Python 实现只使用标准库，
构建可执行程序时调用系统 GCC/Clang 兼容的 C 编译器。
与仓库其余工具共用根目录 uv 项目和 .venv；源码按 [Xe 1.0 冻结范围](../doc/17.md) 解析。
当前版本为 `1.0.0-rc.1`；发布平台、能力边界、兼容承诺和验收见 [RELEASE](../RELEASE.md)。
不熟悉编译器实现时先读 [维护指南](MAINTAINING.md)，再按诊断阶段定位模块。
规范与实际功能的最新逐项核对见 [第 30 章](../doc/30.md)，冻结语法不代表全部后端功能已完成。
最新资源替换与一层模式、静态 Trait/泛型 Copy/Drop、有限 C ABI、单词位运算的边界分别
见 [34](../doc/34.md)、[35](../doc/35.md)、[36](../doc/36.md)、[37](../doc/37.md)。
模块只读 `let` 也有静态地址；两种全局绑定仍仅支持静态 Copy 初值，不执行顶层函数。

## 统一工具入口（推荐）

先在仓库根运行 `uv sync --frozen`。`./xe` 自动使用根目录 `.venv`，不另建环境，
不改变调用者工作目录；从其他项目用仓库入口的绝对路径也可运行。
Xe 自举子集暂时保留独立，不替换本文的 stage0。

```sh
./xe --help
./xe doctor
./xe ast tests/language/enum.xe -o -
./xe check examples/feature_check/main.xe --message-format=json
./xe emit-c tests/language/struct_methods.xe
./xe run examples/args/main.xe -- hello "two words" "你好 Xe" ""
./xe build --manifest-path examples/toolchain --release
./xe run --manifest-path examples/toolchain --bin smoke
./xe test examples/toolchain/src/bin/smoke.xe --sanitize
./xe fmt --manifest-path examples/toolchain --check
./xe lint examples/feature_check/main.xe
./xe doc --manifest-path examples/toolchain
./xe clean --manifest-path examples/toolchain --dry-run
./xe test --compiler
```

有清单时 build/run 默认选择 src/main.xe，doc 优先 src/lib.xe，--bin name 选择
src/bin/name.xe。缓存始终重做 Xe 检查，只复用核对内容和工具身份后的系统 C 编译结果。
编译失败保留旧程序；可读 C 和收据保留在本项目 target，clean 只删除登记且未修改的产物。
旧入口的默认输出位置不变；无清单的显式文件由统一入口输出到其所在目录的 target。

fmt 目前只保守整理已有行的缩进/尾空白，写入前验证 AST 和注释不变。
test 要求明确提供带 main 的 Xe 文件，--compiler 才运行 Python 回归；没有新增测试语法
或自动执行风险样例。doc 输出 Markdown/JSON API 清单，不执行文档示例。
完整接口、JSON 流与退出码、缓存边界、安全清理见 [第 32 章](../doc/32.md)。
也可使用 `python -m compiler` 或 `make xe XE_ARGS='...'`；`make toolchain-test`
运行工具链专项回归，`make toolchain-demo` 运行可直接阅读的多文件示例。

## 从仓库根运行

```sh
make ast
make ast SOURCE=tests/language/enum.xe
make ast SOURCE=tests/language/maybe_error.xe OUTPUT=target/ast/result.json
make ast-test
make check SOURCE=tests/fails/borrow_match_move.xe
make check SOURCE=tests/warnings/return_local_pointer.xe CHECK_FLAGS="--diagnostic-format json"
make check-safety SOURCE=tests/language/enum.xe
make compiler-test
make run
make run SOURCE=tests/language/struct_methods.xe BACKEND_FLAGS=--check-safety
make run SOURCE=tests/backend/enum_pipeline.xe BACKEND_FLAGS=--check-safety
make run SOURCE=tests/backend/enum_resources.xe BACKEND_FLAGS=--check-safety
make run SOURCE=tests/backend/generic_instances.xe
make run SOURCE=tests/backend/tuples.xe
make run SOURCE=tests/backend/type_aliases.xe
make run SOURCE=tests/backend/readline.xe
make run SOURCE=examples/args/main.xe ARGS='hello "two words" "你好 Xe" ""'
make build SOURCE=tests/language/struct_move.xe
make emit-c SOURCE=tests/language/struct_move.xe
make demo
make feature-check
make feature-run
make stdlib-test
make audit

uv run --project . --frozen --offline python compiler/main.py tests/language/enum.xe -o target/ast/enum.json
uv run --project . --frozen --offline python compiler/main.py tests/language/enum.xe -o -
```

make ast 默认输出 target/ast/<源文件名>.ast.json。-o - 只输出 JSON，不混入状态文字。
统一环境配置为根目录 pyproject.toml / uv.lock，虚拟环境位于根目录 .venv。
首次准备环境运行 uv sync --frozen，之后 make 命令可离线使用已经安装的锁定依赖。
编译器运行只使用标准库；开发检查用 Pyright/Ruff，版本由根目录 uv.lock 锁定。
旧版专用的 ply、llvmlite 等依赖已移除。
旧编译器源码已移除并忽略，不影响本目录的入口 compiler/main.py。

Makefile 已固定 --project . --frozen --offline，使用根目录 uv.lock，不更新依赖或访问网络。
环境需满足根项目的 Python 3.13+ 要求；候选版官方验收使用 Python 3.13。
compiler/ 不再含独立项目配置；曾生成的 compiler/.venv 不会再被这些命令使用。

## VS Code 与 Python 检查

请打开仓库根目录作为工作区。`.vscode/settings.json` 指向根 `.venv/bin/python`，
Pylance 使用 standard 检查；Pyright 的 Python 版本、扫描范围和根环境均在
pyproject.toml 中规定，Ruff 也优先读取同一项目配置，不依赖个人编辑器的默认规则。

```sh
uv sync --frozen
make python-check
```

Pyright 的命令行工具需要 Node.js；当前开发环境已提供 Node，编译器本身不需要 Node。
只运行编译器且不需要开发检查时，可以用 `uv sync --frozen --no-dev`。
Makefile 的 uv run 默认会使用开发组，因此运行这些目标仍需提前安装开发依赖。

如果 VS Code 曾经选过 `/usr/bin/python3`，改默认设置不一定清除其已有的选择：
按 Ctrl+Shift+P，运行 **Python: Select Interpreter**，选根 `.venv/bin/python`；
必要时再运行 **Developer: Reload Window**。这只影响本工作区，不改用户全局设置。

静态检查不会代替运行测试。Python AST 本来就是异构 dict/list：相应边界用 Any 表达
真实的数据形状，不把所有字典推断成同一种值；确定不会返回的诊断函数标 NoReturn。
容器/并发辅助类只混入对应宿主，用 TYPE_CHECKING/cast 告诉检查器宿主接口，
不增加运行时循环导入，也不更改 Xe 的类型、指针或所有权规则。

## 模块边界

统一入口与语言实现分开，避免 CLI 中重复实现语义：

- toolchain.py / __main__.py：统一子命令、参数转发、诊断和退出码；
- project.py：定位 xe.toml、项目入口与产物名，仍由原加载器处理模块；
- driver.py：生成 C、计算构建指纹、复用或重建外部 C 编译结果；
- artifacts.py：产物收据、哈希及保守清理；
- formatting.py：保留词法内容并验证 AST 的格式变换；
- documentation.py：可达公开 API、源码位置与文档注释。

以下语言模块位于 xe_ast/，运行库与测试另列：

- source.py：原文、行列、半开范围及 Diagnostic；
- lexer.py：字面量、最长标点、嵌套注释；1> / 2> 不使用全局特殊 token；
- parser.py：声明/类型/块递归解析，表达式按优先级解析；
- ast.py：JSON schema_version = 1 的信封；
- typesys.py：语义类型及类型变量替换，与 AST 分离；
- modules.py：可达文件加载、导入/可见性、本地依赖、内部名称及多文件来源；
- semantic.py：名称、类型、移动、分支、写权限和指针风险分析；
- backend_containers.py：Vec 具体布局/清理和文本 Step 迭代；
- backend_c.py：语义类型侧表、C 降低、结构体方法及资源清理；
- build.py：生成 C、调用系统编译器和原子发布程序；
- runtime/xe_runtime.h：小型字符串、文件及数值运行库，包含标准 IO 实现；
- ../stdlib/io/xe_io.h：stage0 标准输入输出的 C 实现；
- stdlib_io.py：prelude / std::io:: 的共同接口登记，固定名称、参数与返回类型；
- stdlib.py / stdlib_env.py：共用签名结构与 std::env::args 的进程参数接口登记；
- ../stdlib/env/xe_env.h：保存 argc/argv、UTF-8 参数视图、缓存及入口清理；
- cli.py：UTF-8 文件输入、诊断、原子 JSON 输出；
- tests/：AST 形状、源码范围、失败诊断和命令行验收。
- audit.py：可信任示例的分层编译/运行审核，输出机器可读能力报告。

审核默认跳过含 XE-PTR 风险 warning 的程序执行，但仍生成 C 并尝试系统编译，报告
warning_not_run，避免自动解引用悬垂地址。仅在审查源码并接受风险后使用 audit.py 的
--run-warnings；审核不是安全沙箱，没有 warning 也不证明程序安全。

语义 JSON 的 diagnostics 包含 severity（error / warning）；只有 error 使检查退出码为 1。
inferred_types 是独立的类型推导侧表，可以看到 i32@[unsafe] 等自动注记。str、Slice 和用户
组合类型中的地址风险用 unsafe: true 标出，不伪造新名义类型，也不改写原始 AST。
make build / make emit-c 同样输出 warning，但仍生成产物；make run 会实际执行，请先审查风险。

每个 AST 节点有 kind、span 和该节点的字段。start 包含、end 不包含；
offset 是 Unicode 码点位置，line/column 从 1 开始。数值字面量同时保留 raw，避免大整数
在某些 JSON 消费者中失去原文精度。注释保存在 Module.comments，文档注释有独立标记。

表达式 [] 保留 BracketApply；这里无法仅凭源码判断它是索引、枚举负载还是泛型应用。
HandlerBinding 与 AnonymousFunction 分开保存：前者 return 属于外层，后者有独立函数作用域。
比较链保留 ComparisonChain，不误解析成嵌套布尔值比较。

## 已实现与未实现

实现冻结语法的单文件解析和 JSON 输出；语义检查与构建递归加载可达模块。
make ast 仍然只解析；make check 检查名称、类型、初始化、移动和写权限，并报告可识别的指针风险。
make check-safety 为兼容别名。禁止通过指针移走资源。
可变声明使用 let[mut]，参数统一写 name: Type，绑定只读。
需要局部修改时在函数体建立 let[mut]；修改所指对象使用 T@[mut]。
--check-borrows / make check-borrows 保留为旧别名；var 隐藏别名仍归一为 mutable: true AST。
前端标准接口仍有未实现的运行部分；第一版 C 后端已运行结构体、方法、String、Drop，
普通管道、非泛型枚举载荷构造和拥有/借用分支匹配，
以及 tuple[...] 元组、str@ 描述符指针、显式用户 Copy、无损 as 和整数 try_from。
Maybe 的通道/传播/显式 panic、Array/Slice 基础运行与 File 读取已有端到端测试。
具名函数值、无捕获 fn、函数参数/返回和管道目标已降低为 C 函数指针。
捕获闭包生成具体环境和隐藏函数；f() 默认只读/可写访问，移出环境资源才消耗闭包。
泛型 callback、嵌套环境、重复调用和退出清理已真实执行验收，见第 15 章。
自定义 `next() -> Step[T]` 与返回 `Step[T]` 的 `std::iter::from_fn` 支持拥有/可写指针 `for`；`Step::Item[value]` 交付元素，`Step::Stop` 结束，运行 `make iterator-demo`。
闭包动态类型、公共 Call Trait 和 yield 暂停恢复尚未实现，见第 26 章。
泛型函数可推导或显式代入，跨模块具体结构体/枚举实例已有 C 布局与资源清理。
泛型实例按具体类型重新检查和缓存；尚无完整 Trait 或完整后端覆盖。
tuple[...] 支持新绑定与已有变量的浅层解包，拥有资源被 _ 忽略时仍清理。
顶层透明 type 别名支持前向引用与链，循环给定位诊断，不产生新的 C 布局或 Copy 能力。
标准 IO 提供 print/println/readline 与 std::io:: 完整路径；readline 返回拥有的
String??[io::Error]，区分空行、EOF 和 IO 失败。C 实现位于 stdlib/io，支持标准接口 use 导入。
Vec[T]、UTF-8 bytes/chars、String::new/push_char 与 File::create/write_all/flush 已运行验收。
Box[T] 的可失败 new、普通指针 ptr/ptr_mut、消耗 into_value 和递归资源清理已实现，
见 [第 28 章](../doc/28.md)。warning 不阻止正常构建或运行；Box 移动后的指针风险
目前可能保守误报，不改变堆对象地址稳定的实际行为。
Shared/Weak、Mutex/MutexGuard 和 Thread 已支持 C 生成和 POSIX 实际运行；
构建自动为并发接口添加 -pthread。运行 make thread-demo；第 29 章说明线程归属、
自动等待、创建失败清理，以及不能宣称完整竞争检查的边界。
本地模块与 path 依赖示例运行 make source-scan；完整边界见 [第 27 章](../doc/27.md)。
readline 的固定签名支持函数值；异构格式化输出目前只支持直接调用或固定签名包装函数。
make feature-check 执行 Xe 编写的 16 组功能检查；make feature-run 可交互输入 help/check/echo/quit。
std::env::args 返回 Slice[str]?[io::Error]，成功视图只读且在整个 Xe main 执行期间有效。
生成的 C 入口接收 argc/argv，但 Xe main 仍不需要参数。编译器 --run 的 -- 后参数原样
传入目标程序；Feature Check 有参数时直接执行子命令，无参数时继续交互，见第 25 章。
T@ 与 T@[mut] 都是允许别名的普通指针，不是独占借用；没有“再次借用”的调用要求。
T@[mut] 可在初始化、赋值、传参和返回时浅层降为 T@；逆向以及内层指针、
容器参数和函数签名的整体转换均拒绝，unsafe 风险在转换后继续传播。
可识别的悬垂风险给 warning 并传播 unsafe，不阻断编译；所有权和只读写入错误仍拒绝。
支持范围、数据结构和维护方法见 [第 18 章](../doc/18.md)，不宣称内存安全。
构建命令、资源清理、运行验收及暂未支持功能见 [第 19 章](../doc/19.md)。
指针降级、泛型实例缓存和具体后端边界见 [第 22 章](../doc/22.md)。
元组解包与透明类型别名见 [第 23 章](../doc/23.md)，旧花括号元组应迁移到 tuple[...]。
IO 合同、输入边界与 Xe 命令行例子见 [第 24 章](../doc/24.md)。

解析报第一个源码错误并停止；语义检查每函数报第一个主错误，继续检查其他函数。
不写部分成功 AST；解析多错误恢复将在后续前端阶段加入。
语法旧写法应当报迁移提示，不静默转换。输出同路径原子替换；失败保留已有产物。
对同名但不同目录输入，请用 -o 指定不同路径，避免默认输出名碰撞。

## 后续工作建议

首个 Xe 前端与自编译阶段链已在 [bootstrap/](../bootstrap/README.md) 实现。
`make bootstrap` 验收三代未经规范化的 C 字节相同；`make bootstrap-test` 验证跨代
正反例和 sanitizer。它是独立的受限子集，还不是此处 Python stage0 的替代品；
没有接管本文的 AST JSON、多文件加载与完整检查命令。
自举项目暂缓扩展，继续保留现有源码与阶段链；范围见 [第 31 章](../doc/31.md)。
原版工具链下一步可完善全量格式化、源码调试映射和语言服务器，但不先发明未经审核的
测试发现、依赖锁或包配置规则；当前已实现的工具边界见第 32 章。
静态 Trait、条件泛型 Copy/Drop 和合法资源位置的替换清理已经实现；范围见第 34—35 章。
当前仍需逐步完善风险 warning 的精度，它不构成地址安全证明。
阶段一致性回归把未实现占位能力提前报告；运行时错误提供基础 Xe 文件/行/列定位，
但没有完整栈回溯或调试器映射。发布包和 CI 统一使用 `make release-check`。
HIR/MIR 可在降低维护复杂度时逐步引入，但不是开始自举的先决条件；
不为此先重写解析算法或实现机器码优化器。详细步骤与待审核接口见第 30 章。
