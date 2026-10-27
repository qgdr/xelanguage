# Xe Core Preview 1 发布契约

版本 **0.1.0-alpha.1**，语法标识 **xe-core-0.1**，AST JSON schema **1**。
Python项目元数据使用等价的PEP 440版本`0.1.0a1`。
本契约描述当前核心预览版的有限范围，许可证为[Apache-2.0](LICENSE)。

## 支持平台

验收基线为Linux x86_64、Python 3.13、GCC 13。前端使用Python，后端生成C11。
其他平台和编译器可尝试，本版不作已验收承诺。
编译器运行无第三方Python依赖；源码包不捆绑解释器、C编译器或虚拟环境。

## 发布范围

[语言手册](docs/language.md)只描述当前默认core支持的形式。
[支持表](docs/support.md)是范围边界；示例从[核心程序](examples/core/README.md)开始。
更早玩具语言的兼容实现不属于本版语言承诺，不能用它的可运行能力扩充core范围。

普通指针不保证目标始终有效；warning仍可构建。Abort不保证析构。
类型检查、来源分析与资源清理的有限保证不能合并为全语言内存安全声明。
本版没有完整语言自举或生产编译器正确性证明。

## 安装与验收

解包后在包目录运行：

```sh
./xe --version
./xe doctor --cc gcc
./xe check examples/core/main.xe
./xe run examples/core/structs.xe --cc gcc
```

打包验收从包外目录使用独立Python解释器，检查AST、诊断、C生成、系统构建与实际运行。
安全案例保留ASan、UBSan和默认泄漏检测。具体本地验收结果不冒充已发布远端版本。

## 兼容政策

预览版用于明确规则和收集反馈，源码或诊断可能随修正变化；变化写入[CHANGELOG](CHANGELOG.md)。
AST信封提供schema_version和syntax_version供工具识别；生成C的内部名字、布局和缓存格式不是稳定ABI。
不要依赖中文诊断全文；工具应读取JSON的code、severity和span。

## 分发内容

源码包提供编译器、运行库、公开手册、示例和必要测试；不包含本地设计讨论、AI协作指令、
内部审计、证明工作目录、开发环境、密钥或生成物。
公开文档清单见[文档范围](docs/documentation.md)，使用方法见[快速开始](docs/getting-started.md)。
