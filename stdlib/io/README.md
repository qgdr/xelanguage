# 标准输入输出

stage0 提供 `print`、`println`、`readline`，也接受完全相同的 `std::io::print`、
`std::io::println`、`std::io::readline`。简短名称属于当前预先声明的接口。
完整路径仍由同一接口表解析，不表示 `use` 或普通文件模块加载已经实现。

| 接口 | 行为 |
| --- | --- |
| `print("format", ...)` | 格式化写 stdout，不追加换行；返回 Unit |
| `println("format", ...)` | 格式化写 stdout，追加一个换行；返回 Unit |
| `readline()` | 从 stdin 读取下一行，返回 `String??[io::Error]` |

上表的 `...` 描述多个格式化实参，没有新增可变参数声明语法。
输出的格式字符串仍要求编译期字符串字面量，参数数量与支持类型由编译器检查。
没有隐式借用：`str` 可以复制，保留 `String` 时明确传 `text@`，按值传入则转交其所有权。

## 行、EOF 与失败

`String??[io::Error]` 从左到右组成：内层 `String?` 是“有行或没有下一行”，
外层 `?[io::Error]` 是“读操作成功或失败”。这两个问题必须分别处理：

| 返回内容 | 含义 |
| --- | --- |
| 外层 `Yes`，内层 `Yes[text]` | 读到一行，text 是拥有的 String；空字符串也属于这一项 |
| 外层 `Yes`，内层 `None` | 正常到达 EOF，没有下一行 |
| 外层 `No[error]` | IO 读取/刷新失败或输入不符合 UTF-8 |

读取前刷新 stdout，让没有换行的提示文字也能出现在等待输入之前。
去掉行末 LF 及其前面的 CR，因而 LF 与 CRLF 都作为行结束；EOF 前没有换行的最后一行
仍完整返回，单独的尾 CR 也保留。输入采用长度表示，NUL 不会截断 String。
输入字节须通过 UTF-8 验证，不合法时返回错误，不能静默损坏字符。

`readline` 不会自动把 EOF 或失败改为空字符串，也不隐式 panic。
返回的 String 由调用者拥有，必须转移接收并按一般规则析构；无需使用悬垂视图保存输入。

## 当前实现位置

实现位于 [xe_io.h](xe_io.h)，后端将已登记的调用降低为这份 C 标准库接口。
生成的 C 包含必要实现，仍可作为独立 C 源文件构建；没有新增外部 Xe 模块链接步骤。
接口登记位于 `compiler/xe_ast/stdlib_io.py`，类型/格式化/所有权由语义检查器验证。

实际 stdin/stdout、空行、EOF 尾行、CRLF、UTF-8、NUL、错误和内存清理由
`compiler/tests/test_stdlib_io.py` 分别验收。完整使用说明见 [第 24 章](../../doc/24.md)。
