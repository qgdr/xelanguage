# 分支交付与兜底

`value ? { ... }` 对枚举逐变体分流。`:>` 把选中变体的负载交给右侧处理器，
不是把右侧名字当成一个要返回的普通变量。
右侧可以是签名匹配的具名函数，或具有明确输入头的立即处理体。

```xe-core
enum Token { Code[i32], Count[i32], End, }

fn number(token: Token) -> i32 {
    token ? {
        Token::End :> () -> 0,
        else :> value: i32 -> value,
    }
}

fn relay(token: Token) -> Token {
    token ? {
        else => original: Token -> original,
    }
}

fn main() -> Unit {
    let returned: Token << relay(Token::Count[7]);
    println("{} {} {}", number(Token::Code[42]), number(Token::End), number(returned));
}
```

输出 `42 0 7`。number 的 else 合并剩余 Code 和 Count，它们都交付一个 i32 负载。
relay 的 `else =>` 则交付完整 Token，不先拆出其负载。
`else :>[whole]` 是整值交付的兼容长写；新代码使用 `else =>`。

| 输入 | 立即处理头 |
| --- | --- |
| 没有负载 | `() -> 正文` |
| 一个负载 | `name: T -> 正文`，类型可来自已知负载 |
| 忽略一个负载 | `_ -> 正文`，或明确写 `_: T -> 正文` |

`_` 不绑定名字，但不省略转移或清理。资源交给内部拥有槽，在处理体退出时清理，
而不是在正文开始前提前销毁。零输入不能用 `_` 冒充一个输入。

普通 `else :>` 的剩余变体必须全部零负载，或全部具有一个精确同类型负载。
最多一个 else，必须最后，且确实还有未覆盖的变体；覆盖完整后的多余 else 是错误。
没有 else 时须逐变体覆盖全部可能情况，不能重复。

正常完成的各处理体结果需要相同类型。立即体中的 `return` 返回最近的外层函数，
不是返回某个隐式闭包；已提前退出的路径不参与后续正常结果汇合。
`break`、`continue` 使用普通循环目标，只清理实际退出的范围。

当前不支持指针匹配、嵌套负载选择器、组合模式、守卫和编号通道。
不要把 `_` 用作变体选择器；兜底使用 else。

下一章：[显式泛型函数](17-generic-functions.md)。返回[语言手册](../language.md)。
