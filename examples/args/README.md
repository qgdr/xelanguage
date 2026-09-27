# 最小参数打印工具

唯一功能是读取命令行参数，每个参数打印一行。不读取标准输入，不拆分参数内容，
不包含子命令或语言功能检查。

```sh
make build SOURCE=examples/args/main.xe PROGRAM=target/debug/xe-args
./target/debug/xe-args hello "two words" "你好 Xe" ""
```

输出第一行是程序名，随后是 `hello`、`two words`、`你好 Xe`，最后一行为空行。
`""` 是一个真实存在的空参数，不等于没有传参数。

也可以直接构建并运行：

```sh
make run SOURCE=examples/args/main.xe ARGS='hello "two words" "你好 Xe" ""'
```

这时第一行是运行器传入的可执行文件绝对路径。没有其他参数时仍打印程序名。
参数不能表示为 UTF-8 或分配失败时，错误写入 stderr，返回退出码 `1`；正常返回 `0`。
接口和完整示例说明见 [第 25 章](../../doc/25.md)。
