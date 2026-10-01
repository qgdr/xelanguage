# Xe 编写的自举编译器

`compiler.xe` 是一个真实的 Xe 编译器：读源码、分词、构建扁平 AST、检查名称/类型/
写权限/资源传递，再生成 C。它不调用 Python 前端，不复制预存 C，也没有识别自身文件名的捷径。
它已经能够编译自身，但**只支持下面列出的子集，不是完整 Xe 0.9 编译器的替代品**。
子集标识 `xe-selfhost-0.1` 只标记实现范围，不引入或重新冻结语言规则。

## 一条命令验证

从仓库根目录运行，需要根目录 uv 环境和支持 C11 的系统 C 编译器：

```sh
make bootstrap
make bootstrap-test
make bootstrap-sanitize
```

`bootstrap-test` 包含 ASan/UBSan 测试，需要 GCC/Clang 兼容的 sanitizer 环境；目前在
Linux 上验收。保留默认泄漏检测，不通过关闭 LeakSanitizer 绕过运行限制。

构建链是：

```text
Python stage0 ──编译 compiler.xe──> seed-xec
seed-xec     ──编译 compiler.xe──> stage1.c ──cc──> stage1-xec
stage1-xec   ──编译 compiler.xe──> stage2.c ──cc──> stage2-xec
stage2-xec   ──编译 compiler.xe──> stage3.c ──cc──> stage3-xec
```

三次输入是同一文件，没有预处理或源码改写；比较的是未经规范化的 C 文件字节，必须完全
相同。`verify.py` 只在第一步调用 Python 编译器，其余步骤只启动 Xe 可执行文件和系统 cc。
Xe 标准库目前没有进程启动接口，因此外部构建驱动仍使用 Python；运行库也仍有 C 实现。
这不等于工具链完全摆脱 Python/C，更不等于机器码编译器或全语言自举。

产物在 `target/bootstrap/`。`report.json` 保存源码/运行库哈希、C 编译器版本、各代
C 哈希、seed 风险 warning 和 sanitizer 配置。它不声称 ELF 文件字节相同或编译器形式化正确。
固定点还可能稳定地保留错误，因此另用参考编译器对照正例行为，用各代验证反例诊断。

## 用它编译其他程序

```sh
./target/bootstrap/stage3-xec bootstrap/examples/hello.xe target/bootstrap/hello.c
cc -std=c11 -I compiler/runtime target/bootstrap/hello.c -o target/bootstrap/hello
./target/bootstrap/hello

./target/bootstrap/stage3-xec bootstrap/examples/words.xe target/bootstrap/words.c
cc -std=c11 -I compiler/runtime target/bootstrap/words.c -o target/bootstrap/words
./target/bootstrap/words bootstrap/compiler.xe
```

hello 输出 `hello Xe: 42`。words 读取真实文件、保存 ASCII 单词偏移，再逐个打印单词及数量；
UTF-8 字符串和嵌入 NUL 不会使输入被提前截断，但这里不是 Unicode 单词分词器。
`examples/global_counter.xe` 展示模块级 `let[mut]`、跨函数读写、返回全局地址及局部遮蔽，
输出 `global=42 local=100`。

命令接口是 `xec <input.xe> <output.c>`，成功返回 0，用法错误返回 2，编译诊断返回非零。
诊断含路径、行、字节列和 `XE-BOOT-LEX` / `XE-BOOT-COMPILE`；目前每次只报第一个错误。
语法/语义失败不会打开输出，已有 C 文件保持不变。写文件失败尚不保证原子发布；
同字符串输入/输出路径会被拒绝，但硬链接/符号链接别名尚不检测，必须选择独立输出路径。

## 当前支持范围

| 类别 | 已实现的自举子集 |
| --- | --- |
| 词法 | ASCII 标识符、十进制整数、UTF-8 字符串、ASCII 字符/字节、行注释、嵌套块注释 |
| 数据 | bool/i32/i64/usize/u8/char/str/String/File、Unit/Never、普通结构体、显式 Copy |
| 类型组合 | `T@` / `T@[mut]`、Vec[T]、Slice[T]、T? / T?[E] 的类型表示 |
| 调用 | 有类型的命名函数、前向函数引用、递归、内建关联函数与方法 |
| 表达式 | 块尾值、if/else、算术、单次比较、and/or/not 短路、@/#、字段和下标、少量无损 as |
| 语句 | let/let[mut]、`=`/`<<`/`>>`、while、return、break、continue |
| 模块绑定 | 有类型的只读 `let` 和可写 `let[mut]`，Copy 字面量静态初始化；可写绑定支持稳定地址 |
| 资源 | 按值传参/返回移动，普通指针不授予资源所有权，正常退出和语句临时资源清理 |
| 标准接口 | print/println/eprintln、命令行参数、String/str、Vec、File 的下列接口 |

具体库接口：String::new/from、as_str/len/push_str/byte_at；str 的 len/byte_at/
slice_bytes(start..end)；Vec::new、len/push/pop/clear 和索引；Slice 的 len 和索引；
File::open/create、read_to_string/write_all/flush；std::env::args。内建可能失败的结果
可用 `?[panic]` 消解。print 系列要求字面量格式串，只处理 `{}`、转义花括号及基本类型/
字符串（含相应指针），没有 Debug 自动派生或隐式借用。

自定义结构体在使用前声明；函数体在完成顶层扫描后检查，故可以前向调用。
模块级只读绑定统一写 `let NAME: Type = literal;`，例如 `let N_NAME: i32 = 1;`，
模块级可写绑定写 `let[mut] COUNT: i32 = 0;`。两者只支持 Copy 字面量，必须标注类型
并用 `=` 初始化；不执行函数调用或其他运行时初始化，不接收资源绑定或 `<<`。
可写绑定在函数间共享同一静态存储，可用 `COUNT@` / `COUNT@[mut]` 取址并返回；
局部同名绑定优先。只读模块绑定仍不支持取址，可先复制到局部绑定。
旧 `const` 和隐藏 `var`（可写）仅兼容历史源码，不作为新的语言写法；`const` 不接收附件。
内部 `Constant.writable` 记录和生成 C 中的 `static` / `static const` 是实现细节，
不要求 Xe 用户掌握另一种声明关键字。全局可写存储不自动提供线程同步。
`pub` 在单文件声明入口可以解析，但没有模块加载或跨文件可见性检查。
字面量接受上下文类型，不对变量做隐式数值转换；已实现的 as 为同类型、i32→i64、
u8→i32/i64/usize；同类型转换也仅适用于数值，as 不把结构体或指针变成另一种类型。
usize 字面量暂不超过 i64 最大正数，直接拼写 i32 最小负数尚不支持
（测试使用 `-2147483647 - 1`），这些都是实现限制，不是修改 Xe 数值规则。

## 明确不支持的部分

用户枚举与分支管道、链式比较、tuple/Array、类型别名、用户泛型和方法 impl、模块/
包加载、闭包、迭代协议、通用 Trait、自定义 Drop、Box/Shared/Weak/线程、FFI、浮点和
其他整数宽度尚未移植。类型括号分组及指针 `[unsafe]` 附件也未接入这个新前端。
Never 目前仅支持函数返回类型，不支持 Never 参数/字段/组合类型。
没有通用 Maybe 构造/成功值提升、`?[return]` 或模式匹配；支持结果类型不等于已经支持
其完整消解规则。这里没有 AST JSON/check-only 接口，仍可用 Python stage0 的这些工具。

资源检查有意采取保守实现：不能在循环中消耗外层资源后期待下轮重建，也不支持已移动
变量重新初始化；嵌套字段的部分移动只记录根变量与末级字段名，可能拒绝合法程序。
复杂 if 结果需要显式上下文类型。未完成一般指针风险来源分析，不能把本编译器成功接收
程序当成地址有效性证明；同样不能把“缺 warning”写成比 stage0 更安全。

上述功能未完成时应报告不支持，不能悄悄重新解释原语法。维护仍以 `doc/` 的已批准约定
为准；新增语言规则要先讨论。

## 文件与维护入口

- `compiler.xe`：真正的 Xe 前端和 C 发射器；按带标题的源码节定位职责。
- `verify.py`：构建、固定点比较和报告，不参与 Xe 解析或代码生成。
- `examples/`：由新编译器编译的非自编译程序。
- `../compiler/tests/test_selfhost.py`：跨代一致性、参考实现对照、诊断与 sanitizer 验收。
- [第 31 章](../doc/31.md)：结构解释、不变量、Xe 写作方式及实践中的优缺点。

新功能应先加一个独立正例和一个拒绝反例，再修改解析、infer、发射及资源清理，最后跑
自编译链与完整回归。不要为了固定点把参考编译器输出嵌入源码，或降低类型/资源检查。
