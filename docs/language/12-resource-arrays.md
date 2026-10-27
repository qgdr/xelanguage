# 资源数组的整体操作

Array 拥有全部元素。元素不是 Copy 时，整个数组也不能用 `=` 复制；
它可以被完整构造、整体转交、作为函数结果返回，并自动清理仍拥有的资源。

```xe-core
fn pack() -> Array[String,2] {
    [String::from("left"), String::from("right")]
}

fn consume(values: Array[String,2]) -> Unit {
    println("received");
}

fn main() -> Unit {
    let values: Array[String,2] << pack();
    consume(values);
    println("done");
}
```

输出两行：`received`、`done`。consume 接管数组，退出时清理其中仍拥有的两个 String。
values 已整体转交，main 不会再次清理同一批资源。

数组构造按元素书写顺序求值。如果中途 return，只清理此前成功构造且尚未转交的元素，
不访问后续未构造元素。可变局部数组可以整体替换：先完成新值，再清理旧的活跃资源，最后安装新值。

当前资源数组只支持这些整体操作，不提供元素索引、元素视图、单个资源元素移出或逐元素覆盖。
这与 Copy 元素数组的访问范围不同，不能通过一个资源指针取得元素所有权。
不要使用某种未定义的解包或选择器试图取走一部分数组。

具体的 Copy 数组也可以作为显式泛型函数的一个完整类型实参；
资源数组同样可以作为完整实参类型传给[显式泛型函数](17-generic-functions.md)。
这不表示当前支持 `Array[T,N]` 形式的抽象元素或长度参数。

下一章：[类型名字与身份](13-type-declarations.md)。返回[语言手册](../language.md)。
