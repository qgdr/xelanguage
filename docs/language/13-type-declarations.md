# 类型名字与身份

`type A = T;` 为类型表达式取一个透明名字，不创建新的类型身份。
使用 A 的已支持类型位置，可以替换为 T；复制、转移和权限仍遵守 T 的原有规则。

```xe-core
type Amount = Units;
type Units = i32;

fn identity(value: Amount) -> Units { value }

fn main() -> Unit {
    let number: Amount = 42;
    println("{}", identity(number));
}
```

输出 `42`。非循环别名可以前向引用，不要求按依赖手动排序。
别名在其声明作用域解释，不捕获某个调用函数的类型参数或 Self。
循环透明别名当前不支持。

struct 和 enum 声明则创建独立的名义身份：两个字段恰好相同的结构体，仍是不同类型。
别名展开到名义类型为止，不会穿透到其字段或某种相同的底层表示。

```xe-fragment
struct Position { value: i32, }
struct Distance { value: i32, }
type Location = Position;
// Location 与 Position 同一类型；Distance 是不同的名义类型。
```

拥有类型之间的透明命名，不增加 Copy 能力；指针别名也不增加写权限或目标生命期。
用户类型的 Copy 仍须按[结构体与方法](08-structs-and-methods.md)显式声明。

`newtype`、泛型别名和新的类型构造／转换接口当前不可用。
View 类型别名也尚不支持；局部视图请直接标注完整的 `View[P]`。
集合与并集声明有独立的使用边界，见[集合类型与并集声明](14-set-and-union.md)。

下一章：[集合类型与并集声明](14-set-and-union.md)。返回[语言手册](../language.md)。
