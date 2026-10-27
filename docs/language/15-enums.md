# 枚举与负载

`enum` 定义带变体标签的独立名义类型。每个变体当前有零个或一个整体负载。
零负载构造写 `Enum::End`；一个负载写 `Enum::Value[value]`，方括号表示附带内容，不是函数调用。

```xe-core
enum Message {
    Text[String],
    Number[i32],
    End,
}

fn describe(message: Message) -> i32 {
    message ? {
        Message::Text :> text: String -> {
            println("{}", text@);
            1
        },
        Message::Number :> number: i32 -> number,
        Message::End :> () -> 0,
    }
}

fn main() -> Unit {
    let message: Message << Message::Text[String::from("hello")];
    let result: i32 = describe(message);
    println("{} {}", result, describe(Message::Number[42]));
}
```

输出两行：`hello`、`1 42`。describe 按值接收并转交 Message；Text 分支接管 String。
分支结束后，仍由该处理体拥有的 text 自动清理。
编译器只清理当前活动变体仍拥有的负载，不会同时清理其他变体。

枚举当前不提供 Copy，即使某次构造的变体没有资源，也用转移规则处理整个枚举量。
没有分流的枚举在其拥有范围退出时，同样自动清理活动负载；用户不能为 enum 自定义析构。

当前负载可使用基本值、String 或其他非泛型枚举。枚举不能为空，按值布局必须有限；
不支持指针、数组、结构体负载、泛型枚举或同一变体多个独立负载。
变体选择器不再重复写负载附件，输入绑定在 `:>` 之后说明。

零负载并不等于“有一个 Unit 负载”。前者处理头为 `() ->`，后者接收一个实际的 unit 量。
`None` 不是当前公开的独立类型；T?、T?[E] 和编号通道分流当前也不可用。

下一章：[分支交付与兜底](16-branch-pipelines.md)。返回[语言手册](../language.md)。
