# Xe Feature Check

这是用 Xe 编写的命令行功能验收程序。`check` 会运行 16 个已编译的检查，
比较结果并输出 `PASS` / `FAIL` 和统计。它不读取、解析或类型检查任意 Xe
源码文件；检查其他文件使用 `./xe check 文件.xe`，旧入口也仍可用。

统一工具入口可以直接构建、缓存并运行，`--` 后是程序自己的参数：

```sh
./xe run examples/feature_check/main.xe -- check
./xe run examples/feature_check/main.xe -- echo "你好 Xe" ""
./xe run examples/feature_check/main.xe
```

最后一行进入交互模式，保留终端提示和颜色；新入口默认产物位于本例的 target，
不改变下方旧 Makefile 入口的 target/debug/xe-feature-check 路径。

从仓库根目录构建并运行：

```sh
make feature-tool
./target/debug/xe-feature-check check
./target/debug/xe-feature-check --help
./target/debug/xe-feature-check echo "你好，Xe!" "带 空格的参数" ""
```

Shell 引号只负责把含空格的文本组成一个参数，不会传入 Xe。最后的 `""`
是一个有效的空参数；`echo` 用一个空格连接参数，因此输出保留这个空参数
对应的分隔空格。UTF-8 中文和 emoji 同样可以直接传入。

`make feature-check` 会直接运行 `check` 参数模式，适合修改编译器后的快速验收。
`make feature-run ARGS='echo "你好 Xe"'` 可以构建后运行指定命令。
也可以通过编译器构建后转发参数：

```sh
uv run --project . --frozen --offline python compiler/main.py \
    examples/feature_check/main.xe --run -- check
```

参数模式一次执行一个命令，不读取 stdin，不打印交互欢迎信息：

| 命令 | 行为 |
| --- | --- |
| `help` / `--help` / `-h` | 显示参数用法。 |
| `check` | 执行所有检查；正常输出 16 行 `PASS` 和 `summary: 16 passed, 0 failed`。 |
| `echo [text ...]` | 用单个空格连接后续参数并换行；没有文本参数则输出空行。 |
| `quit` | 立即正常退出，不输出文本。 |

除 `echo` 外的命令不接受额外参数。未知命令或多余参数会在 stderr 输出用法，
返回 `2`，不会默默忽略输入。所有参数先通过 UTF-8 检查；无效参数返回 `3`。

不传参数时保留交互模式，也可以用 `make feature-run` 启动。
此时命令逐行从标准输入读取：

| 命令 | 行为 |
| --- | --- |
| `help` | 显示命令说明。 |
| `check` | 执行所有检查；正常输出 16 行 `PASS` 和 `summary: 16 passed, 0 failed`。 |
| `echo` | 再读一行并原样打印内容，支持空行和 UTF-8 文本。 |
| `quit` | 退出。 |

空命令行会被跳过；标准输入到达 EOF 时正常退出，包括等待 `echo` 内容时。
Linux 终端中可以使用 Ctrl-D 结束输入。所有命令需要精确拼写，不会自动去掉空格。
交互模式中输错命令只会在 stderr 提醒，随后继续等待输入，不会退出，也不会
让之后正常的 `quit` 变成失败；参数模式仍对未知命令返回 `2`。
例如可通过管道重复验收和回显中文：

```sh
printf 'help\ncheck\necho\n你好，Xe!\nquit\n' | ./target/debug/xe-feature-check
```

退出码：正常运行和全部通过为 `0`；有功能检查失败为 `1`；参数模式用法错误为 `2`；
参数或标准输入读取失败为 `3`。错误说明输出到 stderr。
无效 UTF-8 也作为读取失败处理，而不会伪装成 EOF。检查失败之后还可以
继续输入命令，但随后 `quit` 或 EOF 不会把退出码重新改回成功。

## 终端与脚本输出

stdin 和 stdout 都连接终端时，交互模式显示 `xe> `，`echo` 的第二次读取显示
`text> `。提示不追加换行，且在等待输入前刷新；Ctrl-D 结束输入时补一个换行。
输入或输出被重定向时不显示提示，因此管道和保存结果不会混入交互文本。

支持颜色的终端中，标题和提示为青色，`PASS` 为绿色，`FAIL` 及错误为红色，
汇总按检查结果染色，每次都会复位。stdout 和 stderr 分别判断自己的终端状态：
例如 stdout 写入文件时保持纯文本，而仍连接终端的 stderr 可以显示红色错误。
`echo` 正文始终原样输出，不加颜色。参数模式的检查也可在终端上显示颜色，但
捕获或重定向 stdout 时仍是原来的纯文本输出。

设置非空 `NO_COLOR` 或 `TERM=dumb` 可以禁止颜色；它们不会禁止正常的交互提示。

```sh
NO_COLOR=1 ./target/debug/xe-feature-check
./target/debug/xe-feature-check check > result.txt
```

检查覆盖整数/浮点运算、比较链、短路与求值顺序、结构体及 Copy 方法、
资源移动及 Drop、泛型、元组解包和透明类型别名、拥有/指针枚举匹配、
Maybe 通道和有检查的整数转换、数组/切片/范围循环、函数值及管道、
String 和 UTF-8 字节边界，以及可写指针降为只读参数。资源用例还验证
忽略解包成员立即析构、作用域析构和数组资源替换。

这些是语言实现的正向运行验收，不能代替编译器全部测试，也不验证任意
指针程序的内存安全。这个示例只使用仍有效的地址；测试在真实 C 后端上
编译运行，并启用 AddressSanitizer 和 UndefinedBehaviorSanitizer。

```sh
uv run --project . --frozen --offline python -m unittest compiler.tests.test_feature_check_project
```

工具通过 `std::env::args()` 取得 `Slice[str]?[io::Error]`，成功时是含程序名
的只读参数切片。`args[0]` 是启动时的程序名称，`args[1]` 才是用户命令。
这不是逐行解析字符串，参数边界已经由启动程序的 Shell 或系统确定。
各个 `str` 视图在进程存活期间有效；工具只读它们，不取得它们的资源所有权。

测试还会把一项检查的预期值故意改错，确认交互和参数两种模式都输出 `FAIL`
和非零退出码；对中文、空参数、无效 UTF-8、底层读取错误及重复执行的资源清理
也有回归检查。参数模式的测试会让 stdin 保持打开，验证程序不等待交互输入。
真实 PTY 测试先关闭终端自身的输入回显，再验证提示刷新、未知命令恢复、颜色复位、
Ctrl-D 和 stdout/stderr 分别重定向；终端体验不只依赖生成 C 的文本断言。
