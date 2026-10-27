# 结构体与方法

`struct` 声明独立的名义类型，字段声明仍写 `名字: 类型`。
构造时用 `.字段 = 值;` 复制，或用 `.字段 << 值;` 明确转移；不要把冒号用于字段初始化。
构造必须恰好初始化全部字段，按书写顺序求值。

```xe-core
struct Point {
    x: i32,
    y: i32,
}

impl Copy for Point {}

impl Point {
    fn sum(self: Self@) -> i32 { self.x + self.y }

    fn shift(self: Self@[mut], amount: i32) -> Unit {
        self.x = self.x + amount;
        self.y = self.y + amount;
    }
}

fn main() -> Unit {
    let[mut] point: Point = Point { .x = 20; .y = 22; };
    let copied: Point = point;
    let pointer: Point@[mut] = point@[mut];
    pointer.shift(1);
    println("{} {}", copied.sum(), point.sum());
}
```

输出 `42 44`。`impl Copy for Point {}` 是显式标记；只有字段都可复制时才允许这样声明。
即使字段全是整数，结构体也不会自动得到 Copy。

`impl Point` 定义方法；`Self` 表示当前实现的类型，`self` 是接收者参数。

| 接收者 | 行为 |
| --- | --- |
| `self: Self@` | 只读访问对象 |
| `self: Self@[mut]` | 经可写指针修改对象 |
| `self: Self` | 按值接收：Copy 复制，非 Copy 转交整体拥有权 |

方法调用可按声明自动取接收者地址。自由函数调用不提供这种适配。
指针不能调用会取得非 Copy 对象所有权的方法；Copy 接收者可以复制所指值。
不含 self 的关联函数用 `Type::function(...)` 调用。

`object.field` 找到字段盒子；`p.field` 允许跟随一层普通指针。
如果字段本身保存另一个指针，后续 `.field` 会先读取那个指针量，再跟随其目标。
这不推广成任意表达式的自动解引用。

资源字段随整个对象自动清理。当前支持完整构造、整体转交和局部整体替换；
不支持逐字段构造未初始化对象、单独移出非 Copy 字段、自定义析构或一般 Trait。
字段返回指针时，字段盒子的地址与字段内保存的指针量仍须区分，不能用后者的来源冒充对象自身存储。

下一章：[整数运算与短路逻辑](09-integer-operations.md)。返回[语言手册](../language.md)。
