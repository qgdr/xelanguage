# 字符串与资源

`str` 是非拥有文本量，字符串字面量产生 str。
`String` 拥有字符串存储，不具备 Copy；`String::from(text)` 创建一个新拥有值。
`text.clone()` 通过只读访问创建独立的 String，原值仍可用。

```xe-core
fn consume(text: String) -> Unit {
    println("{}", text@);
}

fn main() -> Unit {
    let text: String << String::from("hello");
    let copied: String << text.clone();
    consume(copied);
    println("{}", text@);
}
```

输出两行 `hello`。consume 接管 copied，正常退出时清理它；main 结束时清理 text。
clone 可能分配和复制内容，不是免费的 `=`。

`println` 不提供特殊的隐式借用：

```xe-fragment
println("{}", text@); // 传指针量，保留 text 的拥有权。
println("{}", text);  // 按普通实参转移 String；之后不能再读取 text。
```

实参依次求值并按类型传递，因此不能把同一个 String 在一次调用中转移两次。
需要两个独立拥有值时，先明确 clone，或传递仍然有效的指针。

正常离开块或函数时，仍由该范围拥有的资源自动清理。
已经转交的资源不会再次清理；未成功构造的对象不会当作完整资源清理。
panic 当前直接终止，不保证执行这些清理。

当前字符串接口包括 `String::from(str)`、零额外参数的 `String.clone()`，以及可接受文本值的 println。
其他字符串方法、通用 Clone 接口和字符串转义尚不可用。

下一章：[结构体与方法](08-structs-and-methods.md)。返回[语言手册](../language.md)。
