# 集合类型与并集声明

`set { ... }` 在类型位置描述有限常量值域，不创建运行时集合容器。
整数成员必须用 `值: 类型` 明确机器整数身份；该标注不是类型转换。
`A | B` 与 `A union B` 表示同一并集。

```xe-core
type Small = set {1:i32, 2:i32};
type More = set {3:i32};
type Combined = Small | More;
type Same = More union Small;
type Flags = set {false, true};
type Single = set {unit};
type Empty = set {};

fn main() -> Unit {
    let number: i32 = 42;
    println("{}", number);
}
```

输出 `42`。程序声明了若干编译期值域，没有创建 Small 的运行时值。
当前支持成员同类型且互异的 bool、Unit 或明确机器整数，以及同一载体上的并集。
并集满足交换、结合、幂等；与空域求并集不改变原域。

`set {}` 是空域，与 Never 的空正常值域对应；`set {unit}` 包含一个 Unit 值，二者不同。
Never 用于说明没有正常结果，不是应当存入变量的“空值”。

**当前集合与并集声明不能用于变量、参数、返回值或指针目标。**
这项边界也适用于透明别名，以及可以化简成普通机器类型或空域的声明。

```xe-invalid
let value: Small = 1; // Small 的运行时值用法尚不可用。
```

当前也不支持未标注整数成员、重复或混合类型成员、跨载体并集、集合包含的隐式传值，
以及将并集直接当作 C 风格重叠存储。类型声明本身不决定运行时布局或访问方式。

下一章：[枚举与负载](15-enums.md)。返回[语言手册](../language.md)。
