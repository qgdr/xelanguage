# 项目结构与编译流程

这是可用工具的工程实现，不要求维护者先学习编译器算法。
语言规范入口是 [doc/00.md](doc/00.md)，独立实现约定在 [AGENTS.md](AGENTS.md)，
候选版的实际承诺在 [RELEASE.md](RELEASE.md)。

```text
xe                     命令行入口（也可 python -m compiler）
compiler/              Python stage0 编译器与构建工具
  xe_ast/              源码、AST、语义检查与 C 后端
  runtime/             生成程序所需的 C 运行时
  tests/               编译器自动化回归（Python unittest）
stdlib/                Xe 标准模块及对应的 C 支撑接口
examples/              可构建、可运行的用户项目
tests/                 Xe 正反例、后端与风险样例
  language/            当前语言合法用例
  backend/             执行顺序和资源清理等后端用例
  fails/               应被前端拒绝的程序
  syntax_fails/        专门的非法语法样例
  warnings/            允许编译的指针风险样例，不能一律运行
  unsupported/         明确超出当前实现范围的程序
  legacy/              旧版本设计样例，不作为当前能力证明
bootstrap/             Xe 编写的自举子集与固定点验证
doc/                   分主题规范、说明及标注过的历史记录
.github/workflows/     只验收、不自动发布的 CI
.vscode/               相对路径的共享编辑器配置（源码包不要求）
target/                被忽略的 AST、C、程序、收据及候选包
```

compiler/xe_ast 是已有的内部包名，现在不只负责 AST。保留它可避免发布前进行
没有用户收益的大规模导入改名；它不是用户模块或公开 ABI。

## 从源码到程序

1. CLI/driver 选择输入和目标，读取项目配置；模块加载器定位文件并收集公开/私有声明。
2. source/lexer/parser 将 UTF-8 文本转为带源位置的 AST；AST-only 可独立输出 JSON。
3. semantic 及分职责辅助文件检查名称、类型、泛型实例、Trait、写权限和资源状态。
4. backend_c 及后端辅助模块为通过检查的程序生成 C11，插入正常清理和运行时检查。
5. toolchain 调用系统 C 编译器；artifacts 记录输入与输出，保护旧程序并管理安全清理。

错误在对应层报告；解析成功并不等于程序可以编译。运行库和 stdlib 中的 C 头文件是
实际输入，不是可删的生成物；最终 C 包含所需支撑代码。生成 C 的名字和布局不是公开 ABI。

## 阅读与修改路径

先从 [compiler/README.md](compiler/README.md) 和
[compiler/MAINTAINING.md](compiler/MAINTAINING.md) 找到模块职责。
改语法同时检查 lexer/parser/AST；改资源同时检查语义、后端和清理回归。
统一入口及产物协议见 [doc/32.md](doc/32.md)，贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。
目录归档只整理源样例；旧的被忽略编译产物不纳入源码搬迁或候选包。
