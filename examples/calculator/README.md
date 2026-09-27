# Xe Calculator：读得懂的表达式解释器

这是真正编译运行的项目：约 330 行 Xe，包含扫描器、Token 枚举、优先级解析、
常量名称、算术检查、递归深度限制、错误位置和文件输入。实现位于 [main.xe](main.xe)。
它是以后用 Xe 实现编译器前端的练习基础，但本身不是编译器，更不是自举完成证明。

## 运行

从仓库根目录运行：

```sh
make demo
make emit-c SOURCE=examples/calculator/main.xe
make build SOURCE=examples/calculator/main.xe PROGRAM=target/debug/calculator
```

修改 [input.calc](input.calc)，再次运行。每行一条算式，例如：

```text
answer * (7 - 2)
(9 + 3) / 4
8 / 0
```

分别输出 210、3，以及指向除号的除零诊断；错误的算式不会阻止读取下一行。
支持整数、名称 answer/year、空白、一元 +/-、四则运算和括号。
除法向零截断；名称区分大小写。当前使用固定文件路径，尚无命令行参数 API。

计算器将数值限制在 ±1,000,000,000,000，递归深度限制为 64。
这是项目自己的限制，不是 Xe 的整数范围。先检查乘法，再计算，避免溢出后才判断。
诊断位置是从 0 开始的 UTF-8 字节下标；非 ASCII 算式字符报告错误。

## 先理解这些约定

| 写法 | 在本项目中做什么 |
| --- | --- |
| `name: Type` | 声明名称具有何种类型 |
| `let value = expression;` | 建立只读绑定，复制可复制值 |
| `let[mut] index = 0;` | 明确允许重新赋值 |
| `let text: String << ...;` | 接收缓冲区所有权，退出作用域时回收 |
| `self: Self@[mut]` | 方法可以修改调用者对象，不拿走该对象 |
| `impl Copy for Lexer;` | 明确允许复制扫描状态；用户结构体不默认 Copy |
| `Token::Number[42]` | 创建 Number 变体，附带整数 42；不是函数调用 |
| `Token::Number :> value -> ...` | 选中变体，将负载绑定为 value，再执行正文 |
| `T?[ParseError]` | 成功带 T，失败带 ParseError |
| `operation()?[return]` | 成功继续，失败从当前函数返回相同错误 |
| `operation()?[panic]` | 成功继续，失败明确终止程序 |
| 块尾没有 `;` 的表达式 | 作为这个块的结果 |

str 是地址和长度组成的只读视图，复制视图不复制字符；拥有缓冲区的是 String。
File 和 String 在 run_file 内创建，退出时自动关闭/回收；line 只在 text 活着时使用。
扫描器保存位置而非字符串内部指针，减少需要维护的地址有效性关系。
T@ / T@[mut] 是可复制、允许别名的普通指针，不宣称内存安全；本项目仍主动避免悬垂地址。

真实闭包必须写 fn；这里 `:> value -> ...` 是参数绑定，不是闭包。
其中 return 返回外层解析函数，break 退出外层解析循环，不会多出一个函数作用域。
1> 成功通道、2> 失败通道采用同一套处理规则。

## 推荐阅读顺序

1. show / evaluate：输入、成功/失败、完整表达式检查。
2. Token / Lexer.next：字符如何成为语法单元。
3. Parser.primary / unary / product / sum：优先级来自清晰的调用层级。
4. within_limit / multiply：产品范围与溢出前检查。
5. run_file：拥有者、视图、文件失败传播与逐行恢复。

Parser 不保存 source，所有方法显式传入 source，读者不用猜隐藏的数据来源。
sum 先调用 product，product 先调用 unary，因此乘法自然比加法优先。

## 验证与限制

compiler/tests/test_calculator_project.py 真正调用 C 编译器并运行程序，
使用 AddressSanitizer/UndefinedBehaviorSanitizer 检查内存和算术路径。
测试覆盖正确结果、未知字符/名称、括号遗漏、除零、超限、过深递归和后续表达式恢复，
以及生成算式与独立整数结果对照。

本项目只使用目前真正可以运行的单文件功能。模块、通用泛型、捕获闭包执行和动态容器
仍未完成，不能从项目能运行推导整门语言已经达到 Rust/OCaml 的成熟度。

下一步练习可以增加 %、更多命名常量、逐行统计。实现后同时增加正常与错误测试，
不要为了演示简洁而删除范围、除零或深度检查。
