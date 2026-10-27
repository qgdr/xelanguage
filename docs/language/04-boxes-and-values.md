# 盒子、量与指针

名字用来找到盒子，盒子保存量。`let a: T` 建立一个能保存 T 量的盒子；
未初始化只表示盒子尚无可读内容，不表示里面有一个特殊的值或类型。
盒子的身份唯一，复制和转移操作针对其中的量，不复制盒子身份。

```xe-core
fn main() -> Unit {
    let[mut] original: i32 = 10;
    let copied: i32 = original;
    let pointer: i32@[mut] = original@[mut];
    pointer# = 20;
    println("{} {}", original, copied);
}
```

输出 `20 10`。copied 有自己的盒子，保存了一个独立的整数 10。
pointer 的盒子保存一个指针量；该指针指向 original 的盒子。
`pointer# = 20` 修改 original 的内容，没有修改 copied。

同一个表达式可以处于不同使用位置：名字 original 能找到盒子；
在可复制的量上下文读取它，就取得其中的 i32 量。
`original@` 则明确产生盒子的地址量。没有额外的源码“盒子类型”。

函数参数也遵守同样的规则：`fn f(value: T) -> U` 的 value 是函数自己的 T 盒子；
正常返回的是 U 量，由调用方接收，不是返回一个能随意延长存活时间的盒子。

指针本身也是量。复制指针只复制指向关系，不复制目标、不取得目标所有权，
也不延长目标存活时间。
对普通量直接写 `@` 不会隐式建立一个匿名盒子；临时数组索引有明确的独立边界，
见[临时数组索引](11-temporary-arrays.md)。

下一章：[普通指针](05-pointers.md)。返回[语言手册](../language.md)。
