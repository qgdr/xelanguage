# Xe Feature Check

这是用 Xe 编写的交互式功能验收程序。`check` 会运行 16 个已编译的检查，
比较结果并输出 `PASS` / `FAIL` 和统计。它不读取、解析或类型检查任意 Xe
源码文件；编译其他文件仍使用 `python3 -m compiler.xe_ast check 文件.xe`。

从仓库根目录构建并运行：

```sh
python3 -m compiler.xe_ast build examples/feature_check/main.xe -o build/feature_check
./build/feature_check
```

命令逐行输入，没有命令行参数：

| 命令 | 行为 |
| --- | --- |
| `help` | 显示命令说明。 |
| `check` | 执行所有检查；正常输出 16 行 `PASS` 和 `summary: 16 passed, 0 failed`。 |
| `echo` | 再读一行并原样打印内容，支持空行和 UTF-8 文本。 |
| `quit` | 退出。 |

空命令行会被跳过；标准输入到达 EOF 时正常退出，包括等待 `echo` 内容时。
Linux 终端中可以使用 Ctrl-D 结束输入。所有命令需要精确拼写，不会自动去掉空格。
例如可通过管道重复验收和回显中文：

```sh
printf 'help\ncheck\necho\n你好，Xe!\nquit\n' | ./build/feature_check
```

退出码：正常运行和全部通过为 `0`；有功能检查失败为 `1`；未知命令为 `2`；
标准输入读取失败为 `3`。未知命令和读取失败的说明输出到 stderr。

检查覆盖整数/浮点运算、比较链、短路与求值顺序、结构体及 Copy 方法、
资源移动及 Drop、泛型、元组解包和透明类型别名、拥有/指针枚举匹配、
Maybe 通道和有检查的整数转换、数组/切片/范围循环、函数值及管道、
String 和 UTF-8 字节边界，以及可写指针降为只读参数。资源用例还验证
忽略解包成员立即析构、作用域析构和数组资源替换。

这些是语言实现的正向运行验收，不能代替编译器全部测试，也不验证任意
指针程序的内存安全。这个示例只使用仍有效的地址；测试在真实 C 后端上
编译运行，并启用 AddressSanitizer 和 UndefinedBehaviorSanitizer。

```sh
python3 -m unittest compiler.tests.test_feature_check_project
```
