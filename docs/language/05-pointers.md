# 普通指针

后置 `@` 取地址，后置 `#` 沿指针找到目标盒子。
如果 a 是 T 盒子，`a@` 产生 `T@` 指针量；如果 p 保存 `T@`，`p#` 找到 T 盒子。
两种操作各处理一层，不自动增加或消除层级。

```xe-core
fn main() -> Unit {
    let[mut] number: i32 = 41;
    let writable: i32@[mut] = number@[mut];
    let readonly: i32@ = writable;
    writable# = 42;
    println("{} {}", number, readonly#);
}
```

输出 `42 42`。可写指针可复制为只读指针；readonly 不能用来改写目标。
这不会撤销 writable 自己的写权限，也不声明独占访问。

必须区分两个可变层级：

| 声明 | 可修改什么 |
| --- | --- |
| `let[mut] p: T@` | p 盒子中的指针量可替换；目标只读 |
| `let p: T@[mut]` | p 自身不可替换；可以经 p 修改目标 |
| `let[mut] p: T@[mut]` | 两者都允许 |

`p@` 取得指针盒子的地址，因此类型为 `T@@`。`p#@` 则重新取得 T 目标盒子的地址。
只读到可写的升级不允许；取可写地址需要来源本来就有写权限。

```xe-invalid
let number: i32 = 42;
let pointer: i32@[mut] = number@[mut]; // number 的盒子不可写。
```

普通指针允许别名，不是拥有者。已知的短来源保存到长存活位置会被拒绝；
未能确认的来源可能产生警告，某些组合当前不支持。
`[unsafe]` 可以作为指针风险注记，不改变地址的实际有效性，也不会使错误操作自动合法。

下一章：[复制、转移与按值传递](06-copy-and-move.md)。返回[语言手册](../language.md)。
