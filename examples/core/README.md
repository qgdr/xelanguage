# 核心示例

这些程序使用当前默认 core。每个文件都是独立程序，从仓库或源码包根目录运行。

| 文件 | 展示内容 |
| --- | --- |
| [main.xe](main.xe) | 显式类型、函数与基本输出 |
| [type-declarations.xe](type-declarations.xe) | 编译期 set/union 声明，未创建这些域的运行值 |
| [generics.xe](generics.xe) | 最小显式泛型函数 |
| [access.xe](access.xe) | 数组盒子索引与保留原坐标的View |
| [structs.xe](structs.xe) | 结构体、Copy、方法和资源整值传递 |
| [enums.xe](enums.xe) | 枚举负载与穷尽分支 |
| [else.xe](else.xe) | 共同负载else与完整对象交付 |
| [pipelines.xe](pipelines.xe) | 普通管道与按类型传值 |
| [discard.xe](discard.xe) | 忽略一个输入及资源清理 |
| [resource-arrays.xe](resource-arrays.xe) | 资源数组整体传递、重建和清理 |

```sh
./xe run examples/core/main.xe --cc gcc
./xe run examples/core/structs.xe --cc gcc
./xe run examples/core/resource-arrays.xe --cc gcc --sanitize
```

其他文件使用同样的运行命令，将文件名换成表中的名称。正常运行的标准输出如下，表格内的换行与实际输出一致。

| 文件 | 预期输出 |
| --- | --- |
| [main.xe](main.xe) | `42` |
| [type-declarations.xe](type-declarations.xe) | `42` |
| [generics.xe](generics.xe) | `42 owned 7` |
| [access.xe](access.xe) | `40 99 99` |
| [structs.xe](structs.xe) | `field 22`<br>`field 20`<br>`42 44 21`<br>`left right`<br>`finish left right`<br>`done` |
| [enums.xe](enums.xe) | `text: hello`<br>`1`<br>`42 0` |
| [else.xe](else.xe) | `42 7 0 1`<br>`0 0`<br>`99` |
| [pipelines.xe](pipelines.xe) | `42 42 42 0 43` |
| [discard.xe](discard.xe) | `handling pipe`<br>`handling text`<br>`42`<br>`before return`<br>`done` |
| [resource-arrays.xe](resource-arrays.xe) | `pair`<br>`pair`<br>`done` |

[语言手册](../../docs/language.md) · [支持范围](../../docs/support.md)
