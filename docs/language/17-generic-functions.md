# 显式泛型函数

`fn[T]` 在函数声明上引入类型参数，调用时用 `function[Type](...)` 明确代入。
参数 T 表示一个完整类型，不是默认可复制、可计算或可格式化的占位整数。

```xe-core
fn[T] identity(value: T) -> T { value }

fn[U] relay(value: U) -> U { identity[U](value) }

fn main() -> Unit {
    let number: i32 = relay[i32](42);
    let text: String << identity[String](String::from("owned"));
    let source: i32 = 7;
    let pointer: i32@ = identity[i32@](source@);
    println("{} {} {}", number, text@, pointer#);
}
```

输出 `42 owned 7`。同一模板既能交还整数，也能交还 String 或指针量。
具体按值调用仍按实际类型复制或转移；指针不因此取得目标所有权。

所有模板都必须对其允许的类型成立，未调用的坏模板也会报错。
没有能力约束的 T 不能被默认当作 Copy：

```xe-invalid
fn[T] duplicate(value: T) -> T {
    let copied: T = value; // 没有证明T可以复制。
    copied
}
```

可以把已拥有的 T 明确转移到局部 T 盒子，再按值返回。
显式转移在具体 Copy 和非 Copy 实例中都消费指定直接来源；不要把它与普通参数的按类型传值混淆。
函数体也不能从 `T@` 指针中直接取得一个不具备 Copy 保证的 T 所有权。

当前只支持具名函数、显式且完整的类型实参，以及当前可用的具体值类型。
不提供类型实参推断、泛型结构体／枚举、Trait 约束、泛型函数值或泛型管道目标。
具体 Array 类型可以作为一个 T；这不同时开放含抽象 T 元素的 Array 模板。

下一章：[初始化、转移与资源清理](18-initialization-and-cleanup.md)。返回[语言手册](../language.md)。
