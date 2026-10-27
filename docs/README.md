# Xe Core 使用文档

**Core Preview 1 · 0.1.0-alpha.1**

这份手册面向使用Xe编写程序的人，正文对应当前默认core编译器。
完整程序标为`xe-core`；局部片段标为`xe-fragment`；错误示例标为`xe-invalid`。
未支持内容集中列出，不混作可用语法。

| 文档 | 内容 |
| --- | --- |
| [快速开始](getting-started.md) | 安装环境，运行第一个程序 |
| [Xe 语言手册](language.md) | 依次学习语法和内存模型 |
| [命令行工具](cli.md) | 查询编译和运行命令 |
| [Core Preview 1 支持范围](support.md) | 判断某项功能是否可用 |
| [版本与兼容](releases.md) | 了解版本、兼容和分发内容 |
| [核心示例](../examples/core/README.md) | 看独立的可运行程序 |
| [依赖任务规划器](../examples/dependency-planner/README.md) | 读一个较完整的项目 |

语言用盒子保存量，用指针定位盒子。先掌握这个模型，再阅读复制、移动与View，
不必先学类型论或阅读编译器源码。

公式使用`$$`块和`$`行内记号，可在支持数学的Markdown预览中显示；
代码本身不依赖公式颜色或编辑器插件。

本版是有限核心的技术预览，尚不作完整内存安全或稳定ABI保证。
[文档范围与阅读方式](documentation.md) · [Xe Core Preview 1 发布契约](../RELEASE.md) · [更新日志](../CHANGELOG.md)
