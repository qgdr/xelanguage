# 闭包与迭代器

从仓库根目录执行 `make iterator-demo`。它不读取输入，输出：

```text
struct: 0 1 2
closure: 1 2 (outer 0)
from_fn: 0 1 2
pointer: 1 2 (outer 2)
```

main.xe 展示结构体 next、普通 f() 重复调用、拥有捕获和普通指针捕获。
from_fn 每次重新执行回调，用捕获字段保存进度；回调返回 `Step[T]`，`Item` 产出元素、`Stop` 结束。
它不使用尚未实现的 yield。
规则见 [闭包](../../doc/15.md) 和 [迭代器](../../doc/26.md)。
