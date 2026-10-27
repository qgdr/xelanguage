# Xelanguage · Core Preview 1

Xe 是一门静态类型编译语言，源码扩展名为 `.xe`。当前版本为 **0.1.0-alpha.1**。
编译器将已支持的核心语法转换为 C11，再生成本机可执行文件。

Xe 用“盒子、量和指向盒子的指针”解释存储：变量找到盒子，赋值操作盒子中的量。
设计重点是规则少、类型层级稳定、执行过程可推导。

**[开始使用](docs/getting-started.md) · [语言手册](docs/language.md) · [支持范围](docs/support.md) · [命令行](docs/cli.md)**

## 基本语法

`名字: 类型`标注类型；`()`调用或分组；`{}`包围代码块；`;`结束语句。
块末表达式不写分号，就产生这个块的结果。每个局部变量、函数参数和返回值都显式标注类型。

```xe-core
fn add(left: i32, right: i32) -> i32 { left + right }
fn main() -> Unit {
    let answer: i32 = add(20, 22);
    println("{}", answer);
}
```

输出`42`。`Unit`表示正常完成而没有有用数据，它有唯一的值`unit`。
`let[mut]`允许修改已建立盒子的内容；普通`let`的内容不可重赋值。

## 复制与转移分开写

`=`只复制具有Copy能力的量。`<<`转移量及其资源责任，`>>`表示同一操作的相反书写方向。
显式转移之后不能继续使用来源绑定；想得到独立资源时调用`clone()`。

`str`用于字符串字面量，`String`拥有文本存储；下面的`String::from`建立新资源。

```xe-core
fn main() -> Unit {
    let original: String << String::from("hello");
    let copied: String << original.clone();
    let moved: String << original;
    println("{} {}", copied@, moved@);
}
```

输出`hello hello`。`x@`产生指向x盒子的指针量；`p#`访问指针所指盒子。
普通函数参数、返回和`value |> function`按类型选择复制或转交；打印也遵守普通传值规则。

## 索引找到盒子，View产生指针量

`values⟦i⟧`索引数组元素盒子；`⟨范围⟩`限制坐标域。范围左闭右开，`..=`则包含右端点。
`[@]`构造只读地址映射，`[@[mut]]`请求可写地址映射；View保留来源坐标。

```xe-core
fn main() -> Unit {
    let[mut] values: Array[i32,4] = [10,20,30,40];
    let view: View[i32@[mut]] = values⟨1..3⟩[@[mut]];
    view⟦1⟧# = 99;
    println("{} {}", values⟦1⟧, view⟦1⟧#);
}
```

输出`99 99`。View没有复制元素，不拥有元素，也不延长来源寿命。
View中可用的坐标是1、2；已知越界编译时报错，动态越界运行时终止。

## 用结构体组织数据，用枚举列出可能性

字段构造写`.字段 = 值;`。结构体需要显式实现Copy，编译器检查其字段是否都可复制。
枚举变体可以没有负载，或携带一个整体负载；分支必须穷尽。

```xe-core
struct Pair { left: i32, right: i32, }
impl Copy for Pair {}
impl Pair {
    fn sum(self: Self@) -> i32 { self.left + self.right }
}
enum Reply { Number[i32], End, }
fn identity(value: i32) -> i32 { value }
fn main() -> Unit {
    let pair: Pair = Pair { .left = 20; .right = 22; };
    let reply: Reply << Reply::Number[pair.sum()];
    let value: i32 = reply ? {
        Reply::Number :> number: i32 -> number,
        Reply::End :> () -> 0,
    };
    let answer: i32 = value |> identity;
    println("{}", answer);
}
```

输出`42`。`:>`把活动负载交给处理体；`() ->`明示零输入。`_ ->`只忽略一个输入。
处理体的`return`退出外层函数；它没有偷偷创建闭包或新的函数返回边界。

## 运行一个程序

当前验收平台为Linux x86_64、Python 3.13和GCC 13。编译器运行不需要第三方Python包。

```sh
./xe --version
./xe doctor --cc gcc
./xe run examples/core/main.xe --cc gcc
./xe build examples/core/structs.xe --cc gcc -o target/structs
```

从源码包或仓库根目录运行以上命令。安装环境和输出位置见[快速开始](docs/getting-started.md)。

较完整的实例是[依赖任务规划器](examples/dependency-planner/README.md)：
用固定数组、结构体、原坐标View和枚举实现拓扑排序、循环检测及最早完成时间。

## 版本范围

这是用于试写程序和反馈问题的核心预览版。模块、动态容器、闭包、迭代协议、Trait系统、
类型转换、浮点、位运算以及`T?`结果简写尚未进入本版。
资源数组支持整体传递与清理，资源元素访问和View跨函数接口仍有限制。

普通指针允许别名。已有类型、初始化、移动与词法来源检查不构成完整内存安全保证；
warning仍可编译。panic采用Abort，不保证执行析构。详情见[支持与边界](docs/support.md)。

预览期可能有不兼容修正，变化记录在[更新日志](CHANGELOG.md)。
当前发布契约见[RELEASE](RELEASE.md)，许可证为[Apache-2.0](LICENSE)。
