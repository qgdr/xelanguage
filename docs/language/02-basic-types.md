# 基本类型

类型标注说明一个盒子可以保存什么量。当前的整数类型包括
`i8/i16/i32/i64`、`u8/u16/u32/u64`、`isize/usize`。
`i` 表示有符号，`u` 表示无符号；固定宽度类型后面的数字表示位数。
`isize/usize` 使用当前目标的指针宽度，`usize` 用于数组坐标。

```xe-core
fn ready(value: bool) -> Unit {
    if value {
        println("ready");
    }
}

fn stop() -> Never {
    panic("stopped");
}

fn main() -> Unit {
    let count: i32 = 42;
    let enabled: bool = true;
    let text: str = "hello";
    let completed: Unit = unit;
    ready(enabled);
    println("{} {} {}", count, enabled, text);
}
```

输出两行：`ready`、`42 true hello`。stop 没有被调用。

`bool` 只有 `true` 和 `false`。`str` 是非拥有文本量；字符串字面量产生 str。
`Unit` 有一个值 `unit`，可以表示一次操作正常完成。
`Never` 没有正常结果，例如上面的 stop 会终止执行。它不是可以装入普通变量的空值，
当前只支持将它用于函数返回类型及相应的类型别名。

不同机器整数类型保持不同身份。u8 变量不能仅因当前数值较小就直接当作 i32 使用。
当前尚不提供数值转换接口，也不为缺少整数上下文的表达式猜测类型。

```xe-invalid
let small: u8 = 7;
let wide: i32 = small; // 类型不匹配。
let test: bool = 1 < 2; // 两个字面量没有明确的机器整数类型来源。
```

当前整数源码使用十进制；浮点、字符、字节字面量和字符串转义尚不支持。
字符串可以直接包含 Unicode 文本。

下一章：[绑定、函数与块](03-functions-and-control-flow.md)。返回[语言手册](../language.md)。
