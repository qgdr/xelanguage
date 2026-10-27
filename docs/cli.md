# 命令行工具

[文档首页](README.md) · [快速开始](getting-started.md)

当前使用单个`.xe`入口文件。下面命令以`examples/core/main.xe`为例；
命令行工具中的项目或兼容选项，不扩大本版的模块、可见性或FFI语法范围。

| 命令 | 作用 |
| --- | --- |
| `./xe --version` | 显示编译器版本与语法标识 |
| `./xe doctor --cc gcc` | 检查解释器及C工具链 |
| `./xe ast 文件 -o 输出` | 输出源码AST JSON，尚不代表程序已通过语义检查 |
| `./xe check 文件` | 检查名字、类型、初始化、转移及已接入的来源规则 |
| `./xe emit-c 文件 -o 输出.c` | 检查并生成C源码 |
| `./xe build 文件 --cc gcc -o 程序` | 生成本机可执行文件 |
| `./xe run 文件 --cc gcc` | 构建后执行 |

```sh
./xe check examples/core/main.xe
./xe ast examples/core/main.xe -o target/main.ast.json
./xe emit-c examples/core/main.xe -o target/main.c
./xe build examples/core/main.xe --cc gcc -o target/main
./xe run examples/core/main.xe --cc gcc --release
```

`--release`采用优化构建，整数溢出和边界失败规则仍相同。
`--rebuild`跳过可执行文件缓存；`--sanitize`对安全程序启用ASan/UBSan，并保留默认泄漏检测。
`run --timeout 秒数`限制这次执行时间。生成物默认保存在`target/`。

## 诊断与退出码

`--message-format=json`用于读取结构化诊断；文本是默认格式。
`check`无诊断时将JSON报告写入stdout，有诊断时写入stderr。
只有warning时仍返回0；调用工具应同时读取诊断的severity，不能仅看退出码。

检查错误返回1，命令行用法错误通常返回2。运行时panic报告错误并终止；
本版不进行栈展开析构。细节见[支持与失败边界](support.md)。

JSON AST采用schema_version=1；syntax_version用于区分语法版本。
诊断提供code、severity和源码span。生成C的名字与布局不是公开稳定接口。

## 其他工具

`fmt`只做保守的缩进整理；`fmt --check`检查是否需要整理，不承诺完整规范化格式。
`lint`包含语义检查；`doc`可生成声明清单，但不代表未支持的pub或模块语法已经开放。
`clean --dry-run`列出工具记录的可清理产物；`clean`不会任意递归删除项目目录。
完整选项可用`./xe 命令 --help`查询。
