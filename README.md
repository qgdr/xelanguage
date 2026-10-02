# Xelanguage

Xe 是一门静态类型编译语言，源码扩展名为 `.xe`。它希望用一套能组合、能推断的约定，
清楚表达类型、指针、资源和控制流，让人更容易阅读和修改程序。

## 基本语法颗粒

先认识四件事：`名称: 类型` 标注类型，`()` 调用或分组，`{}` 包围代码块，`;` 结束语句。

```xe
fn add(left: i32, right: i32) -> i32 {
    left + right                         // 块尾不写 ;，这个值就是结果。
}

fn main() {
    let answer: i32 = {
        let first = 20;                  // 类型可以推导。
        add(first, 22)
    };
    let[mut] count = 0;                   // [mut] 明确允许修改绑定。
    count = count + 1;
    println("{} {}", answer, count);      // 输出：42 1
}
```

`i32` 是 32 位有符号整数，`Unit` 表示没有内容的值。
`fn` 定义函数，`->` 标注返回类型；没有返回类型时返回 `Unit`。
`let` 声明不可写绑定，`let[mut]` 声明可写绑定。参数也用 `名称: 类型`，参数绑定不可写。
正常结束的块，没有尾值就是 `Unit`；有尾值就产生该值，所以 `if` 也可以用来计算结果。

## 指针：取地址与解引用成对出现

`value: T`，那么 `value@: T@`；`pointer: T@`，那么 `pointer#: T`。
`@` 取地址，`#` 解引用，都是后缀操作。`[mut]` 修饰指针的写权限，不改变它所指的类型。

```xe
fn increase(pointer: i32@[mut]) {
    pointer# = pointer# + 1;
}

fn main() {
    let[mut] value = 40;
    let writable: i32@[mut] = value@[mut];
    let readable: i32@ = writable;        // 可以去掉写权限，不能反过来增加。
    increase(writable);
    increase(writable);                  // 普通指针可以复制、反复传入。
    println("{}", readable#);            // 输出：42
}
```

指针变量能否重新赋值，与所指数据能否修改是两回事。Xe 的指针不是拥有者，也不会延长
对象的生命期；地址有效性仍需要程序员负责。

## 结构体、方法与资源去向

`.` 访问成员，`::` 访问路径或关联函数；结构体构造用 `.字段 = 值;`，不混用类型冒号。
赋值有三个明确分工：

| 写法 | 含义 |
| --- | --- |
| `target = source` | 复制 `Copy` 值，来源仍可使用 |
| `target << source` | 移动非 `Copy` 值，来源不再拥有它 |
| `source >> target` | 按具体类型复制或移动，适合统一传递 |

```xe
struct Point { x: i32, y: i32, }
impl Copy for Point {}                    // 用户类型明确声明可以复制。

impl Point {
    fn sum(self: Self@) -> i32 { self.x + self.y }
}

fn main() {
    let first = Point { .x = 20; .y = 22; };
    let second = first;                  // 复制 Point，first 仍然存在。

    let original: String << String::from("Xe");
    let text: String << original;        // 转移所有权，不能再读取 original。
    println("{} {}", second.sum(), text@); // 输出：42 Xe
}                                       // text 在正常退出作用域时自动清理。
```

字符串字面量是 `str` 只读文本视图；`String::from` 创建拥有内存的字符串。
普通整数、指针等基础值可以复制；`String`、`Vec` 等资源不能隐式复制，想复制要明确
调用 `clone()` 等接口。用户类型实现 `Copy` 要求成员也能复制，且不能有自定义 `Drop`。
方法按声明的 `self` 类型接收对象；这里 `second.sum()` 自动取得只读地址。
自由函数和打印没有特殊规则：`text@` 保留字符串，按值传入 `text` 就会移动它。

## 泛型：声明未知量，使用时代入

`struct[T]`、`fn[T]` 描述泛型声明；`Holder[i32]`、`wrap[i32]` 选择具体实例。
方括号分别附在“声明种类”和“被使用的名称”上，作用对象不同。

```xe
struct[T] Holder { value: T, }
impl[T] Copy for Holder[T] where T implements Copy {}

fn[T] wrap(value: T) -> Holder[T] {
    Holder[T] { value >> .value; }        // T 能复制就复制，否则移动。
}

fn main() {
    let number = wrap[i32](42);          // 这个实例满足 Copy 条件。
    let text << wrap[String](String::from("Xe"));
    println("{} {}", number.value, text.value@); // 输出：42 Xe
}
```

Trait 描述类型应当具备的能力；`where T implements Copy` 就是一个明确的条件。
泛型按具体类型检查并生成代码，不通过赋值偷偷复制资源。

## 容器、迭代与元组：不猜值的形状

`Array[T, N]` 是固定长度数组，`Vec[T]` 拥有可增长的堆存储。
下标得到元素值，取元素指针要写 `items[index]@`；容器的 `for` 则明确绑定元素指针。
元组用 `tuple[...]`，不让 `{}` 同时表示块和元组。

```xe
fn bounds() -> tuple[i32, i32] { tuple[10, 30] }

fn main() {
    let values: Array[i32, 3] = [10, 20, 30];
    println("{}", values[1]);             // 输出：20

    let[mut] numbers << Vec[i32]::new();
    numbers.push(40);
    numbers.push(42);
    for pointer: i32@ in numbers {
        println("{}", pointer#);         // 依次输出：40、42
    }

    let tuple[low, high] = bounds();      // 明确解包，不与数组赋值混淆。
    println("{} {}", low, high);          // 输出：10 30
}
```

整数范围 `for number in 1..4` 绑定整数值，不包含右端点。
自定义迭代器返回 `Step[T]`：`Item[T]` 产生元素，`Stop` 明确结束，不把元素的 `None` 当结束。

## 管道与结果：产生可能性，再处理分支

`value |> function` 把值交给函数。类型的 `T?` 表示“有 T，或者没有”；表达式的 `?`
则消解这一层可能性。它们是构造与消解的一对操作，不是同一个取值运算。

```xe
fn double(value: i32) -> i32 { value * 2 }
fn positive(value: i32) -> i32? {
    if value > 0 { value } else { None }
}

fn main() {
    let doubled = 21 |> double;
    let fallback = positive(-1)?
        1> double                       // 有值时，把值传给 double。
        2> _ -> 0;                      // 没有值时，明确返回备用值。
    println("{} {}", doubled, fallback); // 输出：42 0
}
```

`1>`、`2>` 后接函数，或者 `参数 -> 正文`；后者是立即执行的分支，不是闭包。
想保留成功值可写 `1> value -> value`。
`T?[E]` 为另一分支附带 E，E 不必是错误；`?[return]` 明确传播失败分支，
`?[panic]` 明确选择失败时终止。

## 枚举匹配：选择与参数绑定分开

枚举用 `[]` 附带负载，`:>` 将选中分支的负载送给处理器。
`?[@]` 明确采用指针匹配：每个负载 T 都作为 T@ 传入，不拿走原对象的资源。

```xe
enum Token { End, Integer[i64], Identifier[String], }

fn numeric_value(token: Token@) -> i64 {
    token ?[@] {
        Token::Integer :> number: i64@ -> number#,
        Token::Identifier :> text: String@ -> 0,
        Token::End :> _ -> 0,
    }
}

fn main() {
    let token << Token::Integer[42];
    let name << Token::Identifier[String::from("Xe")];
    println("{} {}", numeric_value(token@), numeric_value(name@)); // 输出：42 0
}
```

选择器只选择分支，不声明变量；名称和类型写在 `:>` 后。一次只匹配一层，
处理内层再写一个 `?`，不隐式展开嵌套结构。

## 闭包：捕获什么，明确写出来

匿名函数也以 `fn` 开始，捕获项放在其后的 `[]`。捕获值按通常规则复制或移动；
捕获地址则保存一个指向原变量的别名。

```xe
fn main() {
    let base = 40;
    let add << fn[base](value: i32) -> i32 { base + value };
    println("{}", add(2));               // 输出：42

    let[mut] current = 0;
    let advance << fn[current@[mut]]() {
        current = current + 1;          // current 仍是整数别名，不变成指针变量。
    };
    advance();
    advance();
    println("{}", current);              // 输出：2；advance 本身不需要可写绑定。
}
```

没有隐式捕获。闭包保存捕获环境，因此带捕获的闭包使用 `<<`；调用仍是普通的 `f(...)`。
仅修改捕获地址的目标不要求闭包绑定可写；修改自身拥有的环境则需要可写闭包绑定。

## 试运行与实际项目

准备 Python 3.13 和 GCC 13，将上面任一完整示例保存为 `hello.xe`，在仓库根运行：

```sh
./xe doctor --cc gcc
./xe run hello.xe --cc gcc
./xe run examples/args/main.xe --cc gcc -- hello "two words" "你好 Xe" ""
```

编译器没有第三方 Python 运行依赖，也可用 `python3 -m compiler`。
`run` 的 `--` 后原样传给程序；构建产物放在项目的 `target/`。

可以继续阅读这些完整项目：

- [参数打印](examples/args/README.md)：最小命令行程序。
- [Feature Check](examples/feature_check/README.md)：带提示符、颜色和错误恢复的交互工具。
- [Calculator](examples/calculator/README.md)：扫描、优先级解析、定位诊断与文件输入。
- [Source Scan](examples/source_scan/README.md)：多文件模块、容器和文件输出。
- [线程示例](examples/threads/README.md)：共享数据、加锁和作用域解锁。

## 完整规范与实现边界

以上是语言的基本颗粒和主要组合方式，不是完整语法手册。
文件即模块，`use` 导入、声明前的 `pub` 公开；透明类型别名写 `type Name = Type;`。
位运算使用 `bitand`、`bitor`、`bitxor`、`bitnot`、`bitshl`、`bitshr`，不复用移动符号。
整数越界会报错或 panic；`as` 只允许对整个源类型值域都无损的转换，
可失败整数转换用 `T::try_from(value)`。
标准库另有 IO、文件、Box、Shared、Weak、同步与线程，底层接口支持有限的 C FFI。

完整规范从 [doc/00.md](doc/00.md) 开始；专题说明：

- [类型、运算与优先级](doc/02.md)、[模块和公开性](doc/09.md)、[闭包](doc/15.md)。
- [元组和类型别名](doc/23.md)、[迭代协议](doc/26.md)、[智能指针](doc/28.md)、[线程](doc/29.md)。
- [资源与模式](doc/34.md)、[静态 Trait](doc/35.md)、[C 接口](doc/36.md)、[位运算](doc/37.md)。

当前版本为 **1.0.0-rc.1**，语法标识 `xe-1.0`，AST schema 1；验收平台为
**Linux x86_64 / Python 3.13 / GCC 13**。Python stage0 位于 `compiler/`，
将 Xe 检查后生成自包含 C11，再交给系统编译器生成可执行文件。
动态 Trait、关联类型、递归模式、yield、async、完整格式化、LSP 与裸机支持等不在本版承诺中。

Xe 不承诺完全内存安全，也不宣称已经比 Rust 更易学或具备 C/C++ 的全部底层能力。
可识别的指针失效风险会给 warning，仍可编译；没有 warning 也不证明安全。
明确的所有权与自动清理，不等于独占借用检查或自动线程安全。

**子集自举已经验证，完整语言尚未自举。** [bootstrap/compiler.xe](bootstrap/compiler.xe)
能编译自身，三代生成 C 按字节一致，但尚未覆盖 enum、泛型、方法、模块、元组、闭包等完整前端。
日常使用功能更完整的 Python stage0；范围和验证流程见 [bootstrap/README.md](bootstrap/README.md)，
Xe 编译器写作经验见 [第 31 章](doc/31.md)。

## 工具链、开发与发布

统一入口还提供 `check`、`build`、`test`、`fmt`、`doc`、`clean`：

```sh
./xe check examples/feature_check/main.xe
./xe build --manifest-path examples/toolchain --release
./xe fmt --manifest-path examples/toolchain --check
./xe clean --manifest-path examples/toolchain --dry-run
```

`xe.toml` 描述包与本地 path 依赖；格式整理会验证 AST 不变，清理只处理登记产物。
命令、缓存、构建输出和工具边界见 [工具链说明](doc/32.md) 与
[compiler/README.md](compiler/README.md)。目录分层见 [ARCHITECTURE.md](ARCHITECTURE.md)，
贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

开发与候选版验收只使用根目录 uv 环境：

```sh
uv sync --locked --dev
make python-check
make compiler-test CC=gcc
make release-check CC=gcc
```

现行正例在 `tests/language/`，历史样例在 `tests/legacy/`，错误与风险例子分别在
`tests/fails/`、`tests/warnings/`。源码包、校验和及包外冒烟流程见
[RELEASE.md](RELEASE.md)，版本记录见 [CHANGELOG.md](CHANGELOG.md)。
本地验收不代表远端 CI 通过，样例通过也不能证明所有程序完全无误。
项目采用 [Apache-2.0](LICENSE)，归属说明见 [NOTICE](NOTICE)。
