# 复制、转移与按值传递

`=` 复制量，要求类型具备 Copy 能力。`<<` 明确转移量；`>>` 是方向相反的同一操作。
显式转移直接来源绑定后，原绑定不可读取，包括原量是可复制整数的情况。

```xe-core
fn main() -> Unit {
    let original: i32 = 7;
    let copied: i32 = original;
    let moved: i32 << original;
    let result: i32;
    moved >> result;
    println("{} {}", copied, result);
}
```

输出 `7 7`。original 在 `<<` 后失效，moved 在 `>>` 后失效。
两条转移语句没有销毁已经交出的量；新的目标接管它。
对资源量，这同时转交唯一的清理责任。

| 写法 | 来源是否仍可用 |
| --- | --- |
| `target = source` | 可用；只适用于 Copy |
| `target << source`、`source >> target` | 直接来源失效；还须具有移动权限 |
| 普通按值实参、正常返回 | Copy 复制，非 Copy 转移 |
| `value |> f` | 与 `f(value)` 使用同一传值规则 |

对可复制的计算结果，优先用 `=`。`<<` 适合明确表达转移，不应只为了接收函数结果就滥用。
复制指针并不取得目标资源的所有权，所以不能经非拥有指针移出 String 等资源。

```xe-fragment
fn identity(value: i32) -> i32 { value }
let source: i32 = 42;
let called: i32 = identity(source);
let piped: i32 = source |> identity;
// source 仍可读取。
```

管道右侧可以是普通具名函数，或单个输入绑定，例如 `value |> item: i32 -> item`。
`_ -> 正文` 忽略一个输入，不建立可读名字；资源输入仍会在处理体退出时清理。
立即处理体中的 return 返回外层函数，不创建闭包边界。

直接自转移 `text << text;` 和 `text >> text;` 是错误。
当前显式转移先支持直接局部目标；复杂目标、资源字段单独移出和非拥有 Copy 位置的显式移出不支持。
移动后能否重建见[初始化、转移与资源清理](18-initialization-and-cleanup.md)。

下一章：[字符串与资源](07-strings-and-resources.md)。返回[语言手册](../language.md)。
