# 标准库的 stage0 实现

这里保存 Xe 标准库已接入当前 Python/C 工具链的实现。第一步提供标准输入输出：
`print`、`println`、`readline`，接口和输入边界见 [io/README.md](io/README.md)。

当前 IO 实现是 [io/xe_io.h](io/xe_io.h) 中的 C 代码，利用已有的小运行库处理
String、UTF-8 和输出。它是可替换的 stage0 实现，尚不是用 Xe 编写的完整标准库。
新增可执行接口不改变 `xe-bootstrap-0.9` 语法版本。

`std::iter::from_fn` 把保存状态的回调包装成拉取迭代器，具体布局由 C 后端生成，
接口见 [iter/README.md](iter/README.md)。首次 None 后不再执行回调，尚不支持 yield。

进程环境接口 `std::env::args()` 已能读取实际命令行参数，返回只读的字符串切片，
包含程序名并保留空参数和参数内的空格。合同见 [env/README.md](env/README.md)，
最小参数打印工具见 [examples/args](../examples/args/README.md)，完整说明见 [第 25 章](../doc/25.md)。

编译器声明了简短 prelude 名称和 `std::io::` 完整路径：

```xe
fn main() {
    print("hello");
    std::io::println(", {}", "Xe");
}
```

这些名字由接口登记表连接到标准库实现；不是源码模块已经被加载。
当前不需要、也不能假定 `use std::io;` 已有模块加载支持。目录中的其他库分层目标
见 [第 13 章](../doc/13.md)，当前可执行 IO 合同见 [第 24 章](../doc/24.md)。

读取示例是 [tests/backend/readline.xe](../tests/backend/readline.xe)：它区分空行、EOF
和 IO 错误；输入结束时退出，适合带输入运行，也适合自动审核。
标准库扩展应同时登记名称、类型与所有权规则，补实际 C 编译运行测试；不能只增加一个
被编译器忽略的 `.xe` 声明文件，也不能用未知名称绕过类型检查。
