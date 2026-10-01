# Xelanguage

Xelanguage（`.xe`）是一门静态类型编译语言。它使用后置类型、块表达式、结构体、枚举、
普通指针和显式所有权，同时把“人类容易阅读”和“工具能够可靠修改”作为同等重要的目标。
Trait 属于目标语言，当前主要实现 Copy、Drop 和 Copy 泛型约束，尚无通用 Trait 分派。

项目目标是实现一门可用并最终能够自举的语言。编译器实现优先考虑正确性、友好诊断、
可维护性和可重复构建，不要求从头实现解析算法、优化器、寄存器分配器或垃圾回收器。

## 当前状态

`doc/` 描述目标语言规范；当前编译器位于 `compiler/`，实现 AST、语义检查和第一版
C 后端，尚未覆盖完整规范。旧版 `excompiler/` 和根目录 `main.py` 已移除并加入忽略规则；
需要查阅旧实现时可从 Git 历史恢复。

`tests/stage9xx` 保存目标语法样例，`tests/fails` 保存必须失败的程序。
`tests/warnings` 保存指针风险程序：应当 warning 但仍可编译，不能统一执行。

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

这些是可解释、可验证的设计优势，不是“所有人都比学 Rust 更快”的结论。
Xe 主动放弃 Rust 式独占借用证明，降低这部分概念负担，同时把地址有效性和数据竞争的
责任交给程序员：风险 warning 不阻止编译，也不证明没有其他风险。
方法自动取地址、地址捕获别名和容器 for 的元素指针是已经明确约定的便利规则，
仍需要学习；`<<` / `>>` 不对称，方括号也有多种上下文含义，不能称为没有学习成本。

## 功能闭环与自举距离

当前已能完成“Xe 源码 → 类型/所有权检查 → C → 可执行程序”的应用开发链：
Calculator 实现扫描与优先级解析，Source Scan 展示多文件、容器和文件输出，
Feature Check 展示交互式命令行，线程示例展示共享数据与作用域解锁。
这些程序验证了功能组合，但 Calculator 是算术解释器，不是 Xe 编译器。

**已有核心足以开始编写 Xe 版自举编译器；尚不能宣称已经完成自举，或完整规范全部可用。**
首版可以用枚举表示 Token/AST、Vec 加整数 ID 保存节点、普通函数组织编译阶段，
并输出 C；不必为了开始自举先实现 yield、动态 Trait 或 LLVM。
通用 Trait、泛型 Copy/Drop、组合/元组/负载过滤匹配、extern ABI、一般常量表达式、
Debug 格式化及完整标准库仍有缺口；部分非 String 资源的下标/指针原位替换也尚未接入后端。
当前 Xe 库也没有运行子进程的接口，不能独立调用系统
C 编译器；第一版可以明确采用外部构建驱动，后续补进程或平台接口。

真正的验收是：Python stage0 编译 Xe 编译器 → Xe 编译器编译同一份源码 → 再编译并
比较规范化产物及测试行为。**现在还缺 Xe 编译器源码和这条验证链。**
使用少量 C 运行库和系统 C 编译器并不妨碍自举。

目标是借鉴 OCaml 的类型化数据结构与表达式组合，减少 Rust 式地址证明的学习负担，
同时保留底层数据与资源控制。当前还没有证据证明达到 OCaml 的编译器开发体验或
C/C++ 的系统编程覆盖：FFI、布局/对齐控制、位操作及底层平台接口仍需补齐或审核设计。
最新逐项审查、待商议边界和自举步骤见 [第 30 章：语言核心与自举能力审核](doc/30.md)。

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
- 泛型声明写 `struct[T] Holder`、`fn[T] wrap`；使用写 `Holder[i32]`、`wrap[i32](10)`。
  前者声明未知量，后者代入具体值；未知 T 的字段初始化可写 `value >> .value;`。
- `T?` 是 `T?[None]` 的简写；`T?[E]` 是 `Yes[T] | No[E]`，E 可以是任意类型。
  只有 API 显式写 `E: Error` 约束时才要求 E 实现相应特性；目前尚无预定义 `Error` 特性。
  未修饰的 `?` 必须处理 `1>` 与 `2>`；`?[return]` 传播 No 分支，`?[panic]` 明确选择终止。
- `as T` 仅做无损转换；可失败整数转换写 `T::try_from(value)`。
- 函数普通参数不会隐式取地址；移动类型按值传入会被移动，需要保留时显式传入 `value@`，
  `println` 和 `format` 也不例外。
- `Type::function()` 访问关联函数，`object.method()` 访问方法；枚举变体用 `[]` 附带负载。
- 语句以 `;` 结束；块末尾不带 `;` 的表达式是块的值。

## 工具链

独立 AST 前端位于 compiler/。运行 make ast 生成
target/ast/ 下的 JSON；make check 加载可达模块，检查类型、所有权和写权限，并报告指针风险，
make check-safety 为兼容别名，make compiler-test 运行全部回归测试。实现边界见 [第 18 章](doc/18.md)。详细命令见
[compiler/README.md](compiler/README.md)；下方 xe 命令仍是后续目标工具接口。

第一版 C 后端已能运行结构体、方法、管道、枚举、Maybe、数组/切片、文件读取和错误传播：
make run 默认运行 struct_move.xe。
例如 make run SOURCE=tests/stage999/struct_methods.xe BACKEND_FLAGS=--check-safety。
枚举与资源管道示例：make run SOURCE=tests/backend/enum_pipeline.xe BACKEND_FLAGS=--check-safety。
泛型具体实例示例：`make run SOURCE=tests/backend/generic_instances.xe`。
标准 IO 已提供 `print`、`println` 和 `readline`（也可写 `std::io::` 完整路径）：
`make run SOURCE=tests/backend/readline.xe` 区分读到一行、空行、EOF 与 IO 错误。
当前库由 [stdlib/io](stdlib/io/README.md) 中的 C 实现支撑；use、分组导入及本地 path 依赖已接入。
自举基础库已支持 Vec[T]、bytes/chars、String 构造、文件写入与 flush。
`Box[T]::new(value)` 返回 `Box[T]?[AllocError]`；`into_value()` 消耗 Box 并取出 T。
运行 `make run SOURCE=tests/stage999/box.xe`，完整契约和指针风险边界见 [第 28 章](doc/28.md)。
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
`make audit` 逐例执行 AST/语义/C/编译/运行，写入 target/audit/stage999.json。
报告证明这些样例通过相应阶段，不证明规范全部实现或已完成自举。

目标工具统一使用 `xe` 命令：

```sh
xe check
xe build
xe run
xe test
xe fmt
xe doc
```

包配置写在 `xe.toml`；本地 path 依赖已实现，注册表和 `xe.lock` 尚未实现。目标是把依赖版本
固定在锁文件中。对象文件、接口缓存和链接细节统一放在
`target/`，普通使用者不需要手动管理。
公开接口统一在声明前写 `pub`；默认私有，公开类型不自动公开其字段或方法。
已确认的模块可见性与例子见 [第 09 章](doc/09.md)。

完整规范从 [`doc/00.md`](doc/00.md) 开始阅读。

当前可固定的前端契约已标记为 [Bootstrap Syntax 0.9](doc/17.md)，其余语义按
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
