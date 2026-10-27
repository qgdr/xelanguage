# 示例程序

这里的程序使用当前默认core。目录和文件使用小写英文、短横线，项目入口统一为`main.xe`。

| 示例 | 内容 |
| --- | --- |
| [核心示例](core/README.md) | 按主题独立运行，观察语法、资源和原坐标视图 |
| [依赖任务规划器](dependency-planner/README.md) | 用有限核心组织一个完整的拓扑排序与任务计时程序 |

从仓库或源码包根目录运行：

```sh
./xe run examples/core/main.xe --cc gcc
./xe run examples/dependency-planner/main.xe --cc gcc
```

每个示例说明职责、运行命令和预期输出。核心例子不需要旧语言兼容选项。
历史开发程序不放在这个公开示例目录中。

[语言手册](../docs/language.md) · [支持范围](../docs/support.md)
