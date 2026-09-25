# Xelanguage

Xelanguage（`.xe`）是一门静态类型编译语言。它使用后置类型、块表达式、结构体、枚举、
Trait 和显式所有权，同时把“人类容易阅读”和“工具能够可靠修改”作为同等重要的目标。

项目目标是实现一门可用并最终能够自举的语言。编译器实现优先考虑正确性、友好诊断、
可维护性和可重复构建，不要求从头实现解析算法、优化器、寄存器分配器或垃圾回收器。

## 当前状态

`doc/` 描述目标语言规范；现有 Python 编译器只实现了其中很小一部分。旧编译器代码可以
作为实验参考，但不会反过来限制语言设计。

`tests/stage9xx` 保存目标语法样例，`tests/fails` 保存应该产生诊断的程序。

## 语法速览

```xe
fn add(left: i32, right: i32) -> i32 {
    left + right
}

fn main() {
    let answer: i32 = add(20, 22); // let 是不可重新赋值的绑定
    var count: i32 = 0;            // var 允许重新赋值
    count = count + 1;

    let text: String << String::from("hello");
    let view: str = text.as_str();

    println("{}: {}", count, view);
}
```

- `name: Type` 中的 `:` 只表示名称与类型。
- `=` 表示复制，仅适用于 `Copy` 类型。
- `<<` 和 `>>` 表示值传递；资源类型的源在传递后失效。
- `T@` 是共享只读借用，`T@[mut]` 是独占可写借用，`#` 解引用。
- `str` 本身是 UTF-8 的地址与长度视图，禁止写成 `str@`。
- `T?` 是 `T?[None]` 的简写；`T?[E]` 是带错误负载的 `Maybe`。未修饰的 `?` 必须处理
  `1>` 与 `2>`，`?[return]` 传播失败。
- 函数参数不会隐式借用；移动类型按值传入会被移动，需要保留时显式传入 `value@`，
  `println` 和 `format` 也不例外。
- `Type::function()` 访问关联函数，`object.method()` 访问方法；枚举变体用 `[]` 附带负载。
- 语句以 `;` 结束；块末尾不带 `;` 的表达式是块的值。

## 工具链

目标工具统一使用 `xe` 命令：

```sh
xe check
xe build
xe run
xe test
xe fmt
xe doc
```

包配置写在 `xe.toml`，依赖版本固定在 `xe.lock`。对象文件、接口缓存和链接细节统一放在
`target/`，普通使用者不需要手动管理。

完整规范从 [`doc/00.md`](doc/00.md) 开始阅读。
