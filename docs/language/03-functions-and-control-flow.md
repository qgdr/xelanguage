# 绑定、函数与块

`let name: T` 建立一个 T 盒子。普通 let 的盒子内容不能在初始化后改写；
`let[mut]` 允许改写内容，盒子身份不因此改变。
参数在被调用函数内有自己的盒子，普通参数绑定不可重写。

```xe-core
fn choose(ready: bool) -> i32 {
    if ready { 42 } else { 0 }
}

fn main() -> Unit {
    let[mut] count: i32 = 0;
    let[mut] total: i32 = 0;
    while count < 3 {
        total = total + count;
        count = count + 1;
    }
    let answer: i32 = { choose(true) };
    println("{} {} {}", count, total, answer);
}
```

输出 `3 3 42`。`let answer: i32 = choose(true);` 也可以，外层块不是必需的。
if 两条正常完成路径的结果类型需要一致；while 本身用于执行循环。
循环体内支持 `break;` 和 `continue;`，分别退出最近的循环或开始下一轮。
当前不提供循环标签和带值 break。

`return value;` 直接返回当前函数；函数体的末尾表达式也可以产生正常返回值。
块内的名字只在该块及其内部可见。当前不支持通过重新声明同名局部名字进行遮蔽。

```xe-fragment
fn answer() -> i32 {
    return 42;
}
```

子表达式和函数实参按书写顺序求值；短路布尔运算会跳过不需要的右侧。
每个正常完成的实参按其类型复制或转交，详见[复制、转移与按值传递](06-copy-and-move.md)。
无初值声明和移动后的重建在[初始化、转移与资源清理](18-initialization-and-cleanup.md)说明。

下一章：[盒子、量与指针](04-boxes-and-values.md)。返回[语言手册](../language.md)。
