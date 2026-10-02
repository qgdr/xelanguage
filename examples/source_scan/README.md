# Source Scan：多模块源码扫描练习

读取一个 UTF-8 文件，打印字节/标量/行/单词数量，并将 ASCII 单词逐行写入报告。
这不是完整 lexer：字符串与注释中的单词也会被收集。

```sh
make source-scan
make source-scan INPUT=tests/language/iterator_step.xe REPORT=target/debug/words.txt
./target/debug/source-scan path/to/input.xe path/to/report.txt
```

工具要求输入、输出是独立文件；相同路径字符串会报错，不打开输出。不同路径别名
（例如符号链接）仍可能指向同一文件，不要用作输出。File::create 会截断旧报告。
参数数目错误退出 2，读写失败退出 1，成功退出 0；不会因为普通 IO 失败隐式 panic。

- `src/main.xe`：参数、读写结果分支、整体流程；
- `src/scan.xe`：字节扫描、Vec[Word]、字节/字符迭代；
- `src/report.xe`：通过只读普通指针观察容器元素，用 String 构造独立报告。

扫描结果保存字节下标而不是原文内部指针；报告复制文字并拥有自己的内存。
每个容器/文件在作用域结束时自动释放。模块加载和基础库合同见 [第 27 章](../../doc/27.md)。
