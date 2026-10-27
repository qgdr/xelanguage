# 初始化、转移与资源清理

声明建立盒子，初始化放入第一个有效量，转移交出量和必要的清理责任，析构清理仍拥有的资源。
这四件事不同。盒子已经声明，不表示其内容可以读取；盒子离开范围，也不表示无条件清理一个完整对象。

```xe-core
fn choose(ready: bool) -> i32 {
    let number: i32;
    if ready { number = 42; } else { return 0; }
    number
}

fn consume(text: String) -> Unit { println("{}", text@); }

fn main() -> Unit {
    let[mut] text: String << String::from("old");
    text << String::from("new");
    consume(text);
    text << String::from("rebuilt");
    println("{} {}", choose(true), text@);
}
```

输出两行：`new`、`42 rebuilt`。choose 在读取 number 前，每条能到达该处的路径都已初始化；
提前 return 的路径不再到达该读取。main 的 text 移动后可重建，因为其盒子允许修改。

不可变盒子首次初始化后，不能借“已经移空”再次初始化。
互斥分支各完成一次首次初始化可以；某条路径可能已经初始化后再写入不可以。
条件初始化后完全不读取变量，也不会仅因可能未构造而报错；退出时只清理实际已构造的路径。

替换普通局部资源时，先完成右侧量，再按此时真实状态清理旧值，最后安装新值。
如果右侧本身已经合法转交旧值，不重复清理它。右侧提前 return 时不执行尚未发生的替换。
直接同一绑定自转移是错误。

结构体和数组的构造中途退出时，只清理已经成功构造且仍拥有的成员。
正常离开范围、return、break、continue 都清理实际退出范围中的资源，不清理仍在外层存活的拥有者。
当前 panic 直接终止，不保证这些析构，不支持捕获或栈展开。

当前不支持未初始化结构体的逐字段构造、非 Copy 字段单独移出及复杂资源目标的替换。
外层不可变变量在循环内延后初始化的回边分析尚不可用；可将每轮变量声明放入循环体，
或在需要重写时明确声明可变盒子。
更一般的原始指针有效性不由这些拥有权规则自动保证。

下一章：[当前功能边界](19-support-boundaries.md)。返回[语言手册](../language.md)。
