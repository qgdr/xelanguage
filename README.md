# Xelanguage

Xelanguage（`.xe`）是一门静态类型编译语言。它使用后置类型、块表达式、结构体、枚举、
普通指针和显式所有权，同时把“人类容易阅读”和“工具能够可靠修改”作为同等重要的目标。
静态 Trait 支持契约检查、默认方法与泛型约束；具体实例生成普通函数调用，不需要虚表。

项目目标是实现一门可用并最终能够自举的语言。编译器实现优先考虑正确性、友好诊断、
可维护性和可重复构建，不要求从头实现解析算法、优化器、寄存器分配器或垃圾回收器。

## 当前状态

当前候选版本为 **1.0.0-rc.1**，语法契约标识为 `xe-1.0`，AST JSON schema 仍为 1。
本轮准备的是面向 **Linux x86_64 + Python 3.13 + GCC 13** 的应用开发版本，
不是全语言自举、嵌入式或内存安全版本。支持范围与兼容承诺见
[发布说明](RELEASE.md)，版本变化见 [CHANGELOG](CHANGELOG.md)。
项目采用 [Apache-2.0](LICENSE)，归属说明见 [NOTICE](NOTICE)。

`doc/` 描述目标语言规范；Python stage0 位于 `compiler/`，实现 AST、语义检查和第一版
C 后端，尚未覆盖完整规范。`bootstrap/compiler.xe` 已实现能编译自身的 Xe 编译器子集，
尚未替代功能更完整的 stage0。旧版 `excompiler/` 和根目录 `main.py` 已移除并加入忽略规则；
需要查阅旧实现时可从 Git 历史恢复。

自举编译器暂作为独立项目保留；日常开发使用原版 Python stage0。
仓库根已提供统一入口 `./xe`，共用根目录 `.venv`，支持检查、构建、运行、测试、
格式整理、API 文档和安全清理。完整命令与当前边界见 [工具链说明](doc/32.md)。

`tests/language` 保存现行语法正例，`tests/legacy` 归档早期阶段样例；`tests/fails` 保存必须失败的程序。
`tests/warnings` 保存指针风险程序：应当 warning 但仍可编译，不能统一执行。

项目目录与编译流程见 [ARCHITECTURE.md](ARCHITECTURE.md)，
开发和贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 快速开始

准备 Python 3.13 和 GCC 13，在仓库或解压源码包目录运行：

```sh
./xe --version
./xe doctor --cc gcc
./xe run examples/args/main.xe --cc gcc -- hello "two words" "你好 Xe" ""
```

编译器本身没有第三方 Python 运行依赖。也可用 `python3 -m compiler`；
只有需要开发检查时才安装 uv 并执行 `uv sync --locked --dev`。
完整试用、候选版边界和源码包验证见 [RELEASE.md](RELEASE.md)。

## 核心特征与优势

Xe 的特色不是把符号换一遍，而是让类型、访问权限、资源和控制流尽可能遵守一组
能组合的约定。以下优势已经能在当前编译器和可执行示例中观察到：

- **值与类型有可推导的对应。** `a: T`、`a@: T@`、`p: T@`、`p#: T` 分别表示
  值、取地址、指针和解引用。`T@[mut]` 明确提供写权限，普通指针可以复制和形成别名；
  取得地址不取得资源所有权。变量、字段、参数都用 `name: Type`，不靠名字大小写猜含义。
- **资源去向可见，正常退出自动清理。** `=` 复制，`<<` 移动不可复制值，`>>` 按类型
  传递；用户类型显式声明 Copy。函数实参、返回值和捕获也按同一套复制/移动规则处理。
  文件、容器和堆对象在作用域退出时自动 drop，包含提前 return、break、continue 和错误传播。
  不隐式 clone，`println` 也不偷偷替用户保留资源。
- **错误和普通分支使用同一个模型。** `T?` 与 `T?[E]` 通过枚举表达不同可能性，
  E 不限定为错误；`?` 消解当前一层。`|>`、`1>`、`2>`、`:>` 都把输入送给处理函数或
  显式参数绑定，不让裸函数名同时表示“调用函数”和“返回函数”。迭代用
  `Step::Item` / `Step::Stop`，元素的 None 不会被误当成结束。
- **少一些需要猜测的语法。** `()` 用于调用与分组，`tuple[...]` 明示元组，`{...}`
  不靠有无逗号变成另一类值；`.field = ...;` 明示结构体字段初始化。
  泛型声明 `fn[T] name` 引入未知量，应用 `name[i32]` 代入具体类型。
  闭包必须写 `fn` 并列出捕获项，调用仍写 `f(...)`。
- **数据表示与抽象可以逐步选择。** Array 内联保存元素，Vec 拥有可增长堆缓冲区，
  str/Slice 是非拥有视图；Box、Shared、Weak 分开表达唯一拥有、共同拥有和观察存活。
  泛型按具体类型生成代码，闭包环境保存实际捕获字段，没有强制追踪式 GC 或隐式深复制。
  这让数据结构和释放时机更容易解释，但当前实现不保证最优布局或零成本。
- **实现与工具便于核对。** AST JSON、带源码位置和编号的诊断、可读 C 输出、
  正反例测试及逐阶段审核，把“解析成功”“检查成功”“真正运行”分开。
  文件模块无隐藏顶层执行，pub 接口和本地依赖可直接从源码查看。
- **底层操作也明确表达意图。** 整数使用 `bitand` / `bitor` / `bitxor` / `bitnot` 和
  `bitshl` / `bitshr`，不让所有权符号兼任移位；移位次数和左移溢出会检查。
  有限 `extern "C"` 支持标量/指针接口，可显式链接已有 C 源码、对象与库。

这些是可解释、可验证的设计优势，不是“所有人都比学 Rust 更快”的结论。
Xe 主动放弃 Rust 式独占借用证明，降低这部分概念负担，同时把地址有效性和数据竞争的
责任交给程序员：风险 warning 不阻止编译，也不证明没有其他风险。
方法自动取地址、地址捕获别名和容器 for 的元素指针是已经明确约定的便利规则，
仍需要学习；`<<` / `>>` 不对称，方括号也有多种上下文含义，不能称为没有学习成本。

实际写编译器时，**拥有者集中管理缓冲区，小记录显式 Copy，节点通过 ID 连接**是一套
好理解的 Xe 写法：Token 用 str 查看原文，Vec 保存 AST，整数 ID 不随 Vec 扩容失效；
`Program@` / `Program@[mut]` 直接说明函数能否修改状态，块尾表达式返回解析结果，
String/Vec/File 正常退出自动清理。编译器源码和实践中的优缺点见
[第 31 章](doc/31.md)。这些是可复现的经验，不是最优性能或完整地址安全的证明。

## 功能闭环与自举距离

当前已能完成“Xe 源码 → 类型/所有权检查 → C → 可执行程序”的应用开发链：
Calculator 实现扫描与优先级解析，Source Scan 展示多文件、容器和文件输出，
Feature Check 展示交互式命令行，线程示例展示共享数据与作用域解锁。
这些程序验证了功能组合，但 Calculator 是算术解释器，不是 Xe 编译器。

**第一个 Xe 子集自举编译器已经完成并验证；完整 Xe 语言尚未自举。**
[`bootstrap/compiler.xe`](bootstrap/compiler.xe) 实现分词、扁平 AST、类型/写权限/
资源检查和 C 发射。`make bootstrap` 先用 Python stage0 编译 seed，然后由 Xe
可执行文件连续三次编译同一份源码；三代生成的 C 按原始字节完全相同，不做规范化。
`make bootstrap-test` 另验收正反例及实际程序，`make bootstrap-sanitize` 验收
ASan/UBSan 自编译。源码、运行库、工具版本和产物哈希写入 `target/bootstrap/report.json`。

这个新编译器能编译自身和文件扫描工具，但尚无 enum/分支、用户泛型、方法 impl、
模块、tuple、闭包等前端支持；仍用命名整数标签表示 AST 种类，不能冒充完整规范实现。
支持范围、保守资源规则及运行命令见 [自举编译器说明](bootstrap/README.md)。
现有 Python stage0 功能更多，仍是日常编译入口，且也没有覆盖全部目标语言：
动态 Trait、关联类型、编译期值参数、动态初始化、Debug 格式化及完整标准库仍有缺口。
静态 Trait、条件泛型 Copy/Drop、一层组合/元组/负载过滤、有限 extern C ABI、静态标量
运算和合法资源位置的原位替换已经接通检查与执行；范围见
[资源与模式](doc/34.md)、[静态 Trait](doc/35.md)、[C 接口](doc/36.md)、[位运算](doc/37.md)。
当前 Xe 库也没有运行子进程的接口，不能独立调用系统
C 编译器；第一版可以明确采用外部构建驱动，后续补进程或平台接口。

模块级 `let` / `let[mut]` 都有静态存储，前者能用 `@` 取只读地址，后者还能取得可写地址；
支持跨函数/模块访问、稳定地址和静态
Copy 聚合初值；[全局计数器例子](examples/globals/README.md) 可用
`./xe run examples/globals/main.xe` 运行。全局资源及动态初始化尚未实现，不自动提供
线程同步。面向 1.0 的编译器核心缺口与建议优先级见 [第 33 章](doc/33.md)。

使用 C 运行库、系统 C 编译器和外部构建驱动不妨碍上述子集自举；固定点也不证明
编译器没有错误，必须继续与参考实现对照行为并完善完整回归。

目标是借鉴 OCaml 的类型化数据结构与表达式组合，减少 Rust 式地址证明的学习负担，
同时保留底层数据与资源控制。当前还没有证据证明达到 OCaml 的编译器开发体验或
C/C++ 的系统编程覆盖：聚合 ABI、布局/对齐控制、volatile、指针机器操作及裸机平台
接口仍需补齐或审核设计；当前不宣称已经能用于嵌入式。
全语言审查与待商议边界见 [第 30 章](doc/30.md)，最新自举结果与写作经验见 [第 31 章](doc/31.md)。

## 语法速览

```xe
fn add(left: i32, right: i32) -> i32 {
    left + right
}

fn main() {
    let answer: i32 = add(20, 22);

    let text: String << String::from("hello");
    let view: str = text.as_str();

    println("{}: {}", answer, view);
}
```

- `name: Type` 中的 `:` 标注值的类型；`fn[T: Trait]` 中标注类型参数的能力约束。
- 当前始终检查类型、所有权和写权限；可变绑定写 `let[mut] count: i32 = 0;`。
  参数统一写 `count: i32`，绑定只读。
  修改调用者对象用 `count: i32@[mut]`，函数内部重新赋值则建立局部 `let[mut]` 绑定。
- `=` 表示复制，仅适用于 `Copy` 类型。
- `<<` 只转移不可复制的值；资源类型的源在传递后失效。
  `>>` 保留通用传递：普通值复制、资源移动，不是 `<<` 的严格反向操作。
- `T@` 是只读普通指针，`T@[mut]` 是可写普通指针；两者都 Copy，允许别名和重复传参。
  可写指针可以隐式降为只读指针，反向不行；只读指针不能修改所指内容。
  `#` 解引用但不授予资源所有权。风险跟着 `[unsafe]` 指针注记传播；编译器能检测的
  悬垂等风险给 warning 并自动标注，仍可编译，不宣称内存安全；不再有 RawPtr 类型。
- `str` 是 UTF-8 的地址与长度视图；`str@` 指向该视图描述符，`.data()` 返回 `u8@`。
- `Array[T, N]` 内联存储元素，`Vec[T]` 拥有可增长的堆缓冲区。
  两者的 `a[i]` 都是元素 T，只有 `a[i]@` 才是 T@；资源不能直接从下标移出。
  `Box[T]` 拥有堆上的一个 T，通过 `ptr()` / `ptr_mut()` 明确取得普通指针。
- 元组值写 `tuple[10, 20]`，类型写 `tuple[i32, i32]`；单元素写 `tuple[10]`。
  `let tuple[x, y] = point;` 创建新变量，`tuple[x, y] = point;` 写入已有变量。
  `()` 用于调用与分组，包括类型分组 `(fn(i32) -> i32)?`。
- `type handler = fn(i32) -> i32;` 为类型创建透明别名，别名沿用原类型的 Copy 和资源规则。
- 用户结构体/枚举必须显式 `impl Copy for Type;`，否则不能使用 `=`。
  有 Drop 或含不可复制字段时禁止实现 Copy；基础类型和普通指针可直接复制。
  泛型可显式声明条件 Copy，Drop 也按具体实例检查并生成，见 [Trait 与泛型资源](doc/35.md)。
- 泛型声明写 `struct[T] Holder`、`fn[T] wrap`；使用写 `Holder[i32]`、`wrap[i32](10)`。
  前者声明未知量，后者代入具体值；未知 T 的字段初始化可写 `value >> .value;`。
- `T?` 是 `T?[None]` 的简写；`T?[E]` 是 `Yes[T] | No[E]`，E 可以是任意类型。
  只有 API 显式写 `E: Error` 约束时才要求 E 实现相应特性；目前尚无预定义 `Error` 特性。
  未修饰的 `?` 必须处理 `1>` 与 `2>`；`?[return]` 传播 No 分支，`?[panic]` 明确选择终止。
- `as T` 仅做无损转换；可失败整数转换写 `T::try_from(value)`。
- 位运算写 `a bitand b`、`a bitor b`、`a bitxor b`、`bitnot a`；移位写
  `a bitshl count` / `a bitshr count`，越界次数或左移溢出报错或 panic，见 [第 37 章](doc/37.md)。
- 函数普通参数不会隐式取地址；移动类型按值传入会被移动，需要保留时显式传入 `value@`，
  `println` 和 `format` 也不例外。
- `Type::function()` 访问关联函数，`object.method()` 访问方法；枚举变体用 `[]` 附带负载。
- 语句以 `;` 结束；块末尾不带 `;` 的表达式是块的值。

## 工具链

首次准备环境运行 `uv sync --frozen`，之后在仓库根直接运行：

```sh
./xe doctor
./xe check examples/feature_check/main.xe
./xe run examples/feature_check/main.xe -- check
./xe run examples/args/main.xe -- "two words" "你好 Xe" ""
./xe build --manifest-path examples/toolchain --release
./xe test examples/toolchain/src/bin/smoke.xe
./xe fmt --manifest-path examples/toolchain --check
./xe doc --manifest-path examples/toolchain
```

`run` 的 `--` 后原样传给程序，交互输入和调用者工作目录保持不变。`xe` 是仓库内的
可执行入口，不要求全局安装；也可用 `make xe XE_ARGS='check 文件.xe'` 或
`uv run --project . --frozen --offline python -m compiler ...`。
进入有 `xe.toml` 的项目后，`xe build/run` 默认选择 `src/main.xe`，`--bin name`
选择 `src/bin/name.xe`。没有清单的显式文件在其所在目录的 `target/` 下生成产物。

构建缓存核对输入、生成 C、编译器和选项，只省略系统 C 编译，仍重新检查 Xe 并报告 warning。
`--rebuild` 强制重建；`--sanitize` 启用 ASan/UBSan，保留默认泄漏检测。
`fmt` 是验证 AST 不变的保守缩进整理器；`test` 目前运行显式指定的普通 main 程序，
不擅自执行错误/指针风险样例；`doc` 生成公开 API 的 Markdown/JSON，不执行文档代码。
`clean --dry-run` 可预览；只清理登记且未手动修改的本项目产物，不递归删 target，
也不处理自举产物。示例工程见 [examples/toolchain](examples/toolchain/README.md)。

旧 Makefile 命令和 `compiler/main.py` 继续兼容，原默认输出路径不变。
`make ast` 生成 `target/ast/` 下的 JSON；`make check` 加载可达模块，检查类型、
所有权和写权限，并报告指针风险。`make compiler-test` 或 `./xe test --compiler`
运行全部回归。实现边界见 [第 18 章](doc/18.md)，详细用法见
[compiler/README.md](compiler/README.md) 与 [第 32 章](doc/32.md)。

第一版 C 后端已能运行结构体、方法、管道、枚举、Maybe、数组/切片、文件读取和错误传播：
make run 默认运行 struct_move.xe。
例如 make run SOURCE=tests/language/struct_methods.xe BACKEND_FLAGS=--check-safety。
枚举与资源管道示例：make run SOURCE=tests/backend/enum_pipeline.xe BACKEND_FLAGS=--check-safety。
泛型具体实例示例：`make run SOURCE=tests/backend/generic_instances.xe`。
标准 IO 已提供 `print`、`println` 和 `readline`（也可写 `std::io::` 完整路径）：
`make run SOURCE=tests/backend/readline.xe` 区分读到一行、空行、EOF 与 IO 错误。
当前库由 [stdlib/io](stdlib/io/README.md) 中的 C 实现支撑；use、分组导入及本地 path 依赖已接入。
自举基础库已支持 Vec[T]、bytes/chars、String 构造、文件写入与 flush。
`Box[T]::new(value)` 返回 `Box[T]?[AllocError]`；`into_value()` 消耗 Box 并取出 T。
运行 `make run SOURCE=tests/language/box.xe`，完整契约和指针风险边界见 [第 28 章](doc/28.md)。
Shared/Weak、Mutex/MutexGuard 和 Thread 已能运行；`share()` 增加共同拥有者，
Guard 自动解锁，Thread 析构自动等待。`make thread-demo` 运行两个线程的同步计数器，
接口和 POSIX 实现边界见 [第 29 章](doc/29.md)。不宣称普通指针程序完全线程安全。
`make source-scan` 运行真实多文件 [源码扫描工具](examples/source_scan/README.md)，
读入文件、保存单词位置并写出报告；接口和边界见 [第 27 章](doc/27.md)。
用 Xe 编写的 [Feature Check](examples/feature_check/README.md) 工具提供 `help/check/echo/quit`：
`make feature-run` 交互运行，`make feature-check` 自动执行 16 组功能检查，
`make stdlib-test` 验证 IO 边界、所有权和工具行为。
`std::env::args()` 提供实际命令行参数；最小的 [参数打印工具](examples/args/README.md)
只读取参数并逐项打印，示例代码与运行方法见 [第 25 章](doc/25.md)。
Feature Check 也能直接执行 `./target/debug/xe-feature-check check` 或 `echo "你好 Xe"`。
make emit-c 输出可读 C，make build 生成可执行文件；实际运行边界见 [第 19 章](doc/19.md)。

实际项目：[Xe Calculator](examples/calculator/README.md)，用约 330 行 Xe 实现扫描器、
优先级解析、算术检查、定位诊断和文件输入。运行 `make demo`；修改 input.calc 即可试验。
`make audit` 逐例执行 AST/语义/C/编译/运行，写入 target/audit/language.json。
audit 报告只证明样例通过相应阶段；自举有独立构建链，也不证明完整规范全部实现。

包配置写在 `xe.toml`；本地 path 依赖已实现，注册表和 `xe.lock` 尚未实现。
当前统一工具把程序、C、AST、文档和构建收据放在本项目 `target/`；
模块对象/接口缓存、增量语义分析、交叉编译、LSP 和自动单元/文档测试尚未实现。
新增语言或测试发现规则仍需先审核，不将工具的默认设置冒充语言规范。
公开接口统一在声明前写 `pub`；默认私有，公开类型不自动公开其字段或方法。
已确认的模块可见性与例子见 [第 09 章](doc/09.md)。

完整规范从 [`doc/00.md`](doc/00.md) 开始阅读。

当前可固定的前端契约已标记为 [Xe 1.0 冻结范围](doc/17.md)，其余语义按
RESERVED / PROVISIONAL / DEFERRED 分阶段实现。

`1> handle` 将成功负载传给 handle，`2> _ -> 0` 忽略失败并返回备用值。
枚举分支写 `Token::Integer :> number: i64@ -> number#`，指针匹配使用 `?[@]`。
每个 `?` 只处理当前一层枚举；内层再次显式匹配，不自动展开递归模式。
管道之后只接可调用目标或参数绑定，真正的匿名函数/闭包必须以 fn 开头，显式捕获用
`fn[value](x: i32) -> i32 { value + x }`。捕获环境与方法式 f() 已能运行：读/写调用保留自身，
移出捕获资源才消耗环境。机制见 [第 15 章](doc/15.md)。
`next(self: Self@[mut]) -> Step[T]` 对象可用于 for；`Step::Item[value]` 产生元素，`Step::Stop` 结束迭代。`std::iter::from_fn` 保存返回 `Step[T]` 的可重复回调。
运行 `make iterator-demo`，边界与清理规则见 [第 26 章](doc/26.md)；yield 尚未实现。
优先级见第 02 章；本轮确认的设计与迁移理由见 [第 21 章](doc/21.md)。
指针权限转换和泛型实例化的实现边界见 [第 22 章](doc/22.md)。
tuple 元组、解包和透明类型别名的 0.9 迁移见 [第 23 章](doc/23.md)。
标准 IO 与用 Xe 编写的命令行功能工具见 [第 24 章](doc/24.md)。

## 候选版验收

```sh
uv sync --locked --dev
make release-check CC=gcc
```

这个入口检查版本/锁文件、Python 静态检查、全部编译器回归（包含 sanitizer 和三代
自举固定点），再生成源代码包、SHA-256 与清单，并在临时目录从解压包实际编译运行。
候选包位于 `target/releases/`；只打包源码，不打包 `.venv`、密钥或已有构建产物。
提交后的正式候选包使用 `make release-package RELEASE_FLAGS=--require-clean`，
逐文件与 HEAD 核对，记录提交和干净快照；再运行 `make release-smoke CC=gcc`。
验收不创建 Git 标签，不推送，不调用外部发布接口。CI 配置执行同一个入口；
本地通过不能代替 GitHub 上该提交的 CI 结果。流程见 [发布说明](RELEASE.md)。
