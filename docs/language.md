# Xe 语言手册

本手册说明当前默认编译器支持的语言。所有 `xe-core` 代码块都是独立的完整程序；
`xe-fragment` 是需要放入相应上下文的片段，`xe-invalid` 用于说明拒绝原因。

Xe 用统一的存储模型解释程序：**名字找到盒子，盒子保存量，指针也是量，指针指向盒子。**
类型说明能放入什么量；复制、转移和写权限分别决定怎样操作它。

## 从零学习：按顺序阅读

每章的例子独立运行，不要求把不同章节的声明合并到同一个文件。

| 序号 | 文档 | 内容 |
| --- | --- | --- |
| 01 | [开始阅读 Xe 程序](language/01-first-program.md) | 最小程序、名字与类型、分号和块结果 |
| 02 | [基本类型](language/02-basic-types.md) | 整数、bool、str、Unit 与 Never |
| 03 | [绑定、函数与块](language/03-functions-and-control-flow.md) | 显式类型、参数、返回、if、while |
| 04 | [盒子、量与指针](language/04-boxes-and-values.md) | 存储身份、未初始化状态与值传递 |
| 05 | [普通指针](language/05-pointers.md) | 后置 @/#、指针层级和写权限 |
| 06 | [复制、转移与按值传递](language/06-copy-and-move.md) | Copy、=、<<、>>、普通调用和管道 |
| 07 | [字符串与资源](language/07-strings-and-resources.md) | str、String、clone 与自动清理 |
| 08 | [结构体与方法](language/08-structs-and-methods.md) | 字段构造、显式 Copy、Self 与接收者 |
| 09 | [整数运算与短路逻辑](language/09-integer-operations.md) | 运算、比较、短路、溢出与失败 |
| 10 | [数组索引与保留原坐标的视图](language/10-arrays-and-views.md) | ⟦i⟧、⟨范围⟩、View[P] 与原坐标 |
| 11 | [临时数组索引](language/11-temporary-arrays.md) | 临时索引与完整表达式边界 |
| 12 | [资源数组的整体操作](language/12-resource-arrays.md) | 整体构造、传递和清理 |
| 13 | [类型名字与身份](language/13-type-declarations.md) | 透明别名、前向依赖与名义身份 |
| 14 | [集合类型与并集声明](language/14-set-and-union.md) | set、union、\| 与编译期使用边界 |
| 15 | [枚举与负载](language/15-enums.md) | 零或一个负载、显式构造与自动清理 |
| 16 | [分支交付与兜底](language/16-branch-pipelines.md) | :>、else、=>、零输入与正常结果 |
| 17 | [显式泛型函数](language/17-generic-functions.md) | fn[T]、显式实参及参数能力限制 |
| 18 | [初始化、转移与资源清理](language/18-initialization-and-cleanup.md) | 延后初始化、转移、替换和退出清理 |
| 19 | [当前功能边界](language/19-support-boundaries.md) | 尚不支持的功能、错误与警告的含义 |

需要构建和运行命令时，返回[文档首页](README.md)。
