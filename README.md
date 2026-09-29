# Xelanguage

Xelanguage（`.xe`）是一门静态类型编译语言。它使用后置类型、块表达式、结构体、枚举、
Trait 和显式所有权，同时把“人类容易阅读”和“工具能够可靠修改”作为同等重要的目标。

项目目标是实现一门可用并最终能够自举的语言。编译器实现优先考虑正确性、友好诊断、
可维护性和可重复构建，不要求从头实现解析算法、优化器、寄存器分配器或垃圾回收器。

## 当前状态

`doc/` 描述目标语言规范；当前编译器位于 `compiler/`，实现 AST、语义检查和第一版
C 后端，尚未覆盖完整规范。旧版 `excompiler/` 和根目录 `main.py` 已移除并加入忽略规则；
需要查阅旧实现时可从 Git 历史恢复。

`tests/stage9xx` 保存目标语法样例，`tests/fails` 保存必须失败的程序。
`tests/warnings` 保存指针风险程序：应当 warning 但仍可编译，不能统一执行。

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
- `str` 是 UTF-8 的地址与长度视图；`str@` 借用该视图描述符，`.data()` 返回 `u8@`。
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
- 函数参数不会隐式借用；移动类型按值传入会被移动，需要保留时显式传入 `value@`，
  `println` 和 `format` 也不例外。
- `Type::function()` 访问关联函数，`object.method()` 访问方法；枚举变体用 `[]` 附带负载。
- 语句以 `;` 结束；块末尾不带 `;` 的表达式是块的值。

## 工具链

独立 AST 前端位于 compiler/。运行 make ast 生成
target/ast/ 下的 JSON；make check 检查单文件类型、所有权和写权限，并报告指针风险，
make check-safety 为兼容别名，make compiler-test 运行全部回归测试。实现边界见 [第 18 章](doc/18.md)。详细命令见
[compiler/README.md](compiler/README.md)；下方 xe 命令仍是后续目标工具接口。

第一版 C 后端已能运行结构体、方法、管道、枚举、Maybe、数组/切片、文件读取和错误传播：
make run 默认运行 struct_move.xe。
例如 make run SOURCE=tests/stage999/struct_methods.xe BACKEND_FLAGS=--check-safety。
枚举与资源管道示例：make run SOURCE=tests/backend/enum_pipeline.xe BACKEND_FLAGS=--check-safety。
泛型具体实例示例：`make run SOURCE=tests/backend/generic_instances.xe`。
标准 IO 已提供 `print`、`println` 和 `readline`（也可写 `std::io::` 完整路径）：
`make run SOURCE=tests/backend/readline.xe` 区分读到一行、空行、EOF 与 IO 错误。
当前库由 [stdlib/io](stdlib/io/README.md) 中的 C 实现支撑，没有假定普通 use 模块加载已完成。
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

包配置写在 `xe.toml`，依赖版本固定在 `xe.lock`。对象文件、接口缓存和链接细节统一放在
`target/`，普通使用者不需要手动管理。

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
