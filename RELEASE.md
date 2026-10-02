# Xe 1.0.0-rc.1 发布契约

本轮是应用开发候选版，不是已经上线的稳定 1.0。
语法标识 `xe-1.0`，AST JSON schema 1；Python 元数据 `1.0.0rc1` 与
CLI/候选包 `1.0.0-rc.1` 表示同一版本。许可证为 [Apache-2.0](LICENSE)，
归属说明见 [NOTICE](NOTICE)，标准文本来源为 [Apache 官方许可证](https://www.apache.org/licenses/LICENSE-2.0)。

## 当前承诺的平台与工具

- Linux x86_64，基线为 Ubuntu 24.04 / GCC 13 / Python 3.13。
- Python stage0 为默认前端，生成自包含 C11，再由系统 GCC 构建可执行程序。
  线程接口使用 POSIX/pthread。其他 Python/GCC 版本与 Clang 可以尝试，但不冒称已验收。
- 编译器运行无第三方 Python 依赖；uv 管理根环境和锁定开发工具，不创建 compiler 子环境。
- `bootstrap/` 验证一个能编译自身的 Xe 子集，不替代完整 stage0；完整 Xe 自举不是本版承诺。

源码包包含原始编译器、运行库、Xe 标准模块、文档和正反例，不捆绑 Python、GCC、
uv、开发工具、虚拟环境或预编译二进制。用户仍需准备上述系统工具。
包含 .python-version 和维护文档；.vscode 是仓库中的可选编辑器配置，不进入源码候选包。

## 已闭环的语言核心

| 范围 | 本候选版支持 |
| --- | --- |
| 类型与数据 | 基本数值、bool/char、str/String、结构体/枚举、Array/Vec/Slice、tuple、透明类型别名、函数值 |
| 资源与指针 | 显式 Copy、= 复制、<< 移动、>> 按类型传递、正常控制流 Drop、部分移动、合法位置资源替换、T@ / T@[mut] 普通指针与浅层只读降级 |
| 抽象 | 具体泛型实例化、静态 Trait 契约/默认方法/约束、条件泛型 Copy/Drop、显式捕获闭包与方法式调用 |
| 控制与迭代 | 块尾值、if/while/for、比较链、普通/分支管道、T? / T?[E]、传播与 panic、一层模式过滤/组合、Step[T] 与 from_fn |
| 模块与全局 | 文件模块、pub/私有、导入别名、本地 path 依赖、静态 Copy 初值的模块 let 与 let[mut]、稳定静态地址 |
| IO 与系统接口 | 标准 print/println/readline、参数/文件/终端接口、Box/Shared/Weak、Mutex/Guard、Thread、单词位运算、有限 extern C |

这些是范围摘要，不覆盖章节中的每一个未来设想。准确的组合边界见
[指针与泛型](doc/22.md)、[闭包](doc/15.md)、[模块与容器](doc/27.md)、
[智能指针与线程](doc/28.md)、[线程实现](doc/29.md)、[资源与模式](doc/34.md)、
[静态 Trait](doc/35.md)、[C ABI](doc/36.md)、[位运算](doc/37.md)。

必须知道的边界：

- 普通指针允许别名，不证明地址存活或无数据竞争。warning 不阻止编译，不能把
  warning 样例全部当成安全程序运行。类型、写权限与资源所有权错误仍必须拒绝。
- 泛型正文按实际实例化检查；`xe check` 不证明未使用泛型的所有可能实例合法。
  AST-only 解析不证明类型正确，也不证明后端支持。
- C FFI 仅限已审核的标量、对应指针和兼容无捕获函数回调；不保证 Xe 聚合布局、
  C 可变参数或 C 导出库。生成 C 的内部符号与布局不是公开 ABI。
- 全局仅允许静态 Copy 初值，不执行顶层函数，不支持动态/资源全局或自动同步。
- 格式字符串必须是字面量；只支持匿名 `{}`、指针 `{:p}` 与 `{{`/`}}`。
  `{}` 输出已实现的基本值/字符串/标准状态值，指针可自动读取一层；
  用户结构体/容器没有隐式 Display/Debug 协议，内建 format 字符串构造未实现。
- 用户类型须显式 impl Copy；不隐式 clone。panic 直接终止，不承诺执行 Drop。
  基础运行时错误有 Xe 文件/行/列，但没有完整回溯、调试器映射或 C 错误捕获。
- 当前手动调用 `object.drop()` 只是执行钩子正文，不消费对象，也不取消作用域自动
  Drop，不能将它当作“提前释放”接口；是否限制手动钩子仍需设计者审核。

动态 Trait、关联类型/父 Trait、重叠特化、常量泛型、泛型别名、递归模式/守卫、
独立泛型闭包、动态闭包共同类型、yield/async、完整 Debug/format、完整 C ABI、
布局/对齐/volatile/原子机器操作、裸机/嵌入式、交叉编译和全语言自举不在此范围。
注册表/Git 依赖、xe.lock、模块增量编译、完整格式化、LSP、多错误恢复也未承诺。
Map/Set/Iterator/Formatter 的历史内建占位名不能当成已经实现的类型使用；
自定义模块仍可定义普通类型，不保留同名能力优先权。

## 兼容与诊断约定

RC 用于验证上述范围，修正 bug 仍可能使以前误接收的错误程序被拒绝；不静默改变
已经批准的运算符含义。发现真正的设计冲突仍先讨论，再写迁移说明。
转为稳定 1.0 后，1.x 的目标是保持范围内合法源码的含义与公开标准接口兼容；
不得把未来目标能力误当成兼容承诺。修复错误、风险诊断精度及实现优化可能改变
诊断文字或生成 C，应记录在 CHANGELOG。

AST 信封的 schema 1、kind 与半开 span 含义继续保留，syntax_version 升级为 xe-1.0。
诊断的 code/severity/file/span 与 CLI 退出码便于工具使用；完整协议见
[工具链文档](doc/32.md)。不要匹配中文报错全文，也不要依赖生成 C 内部名字。
基础运行时位置不改动 AST，不偷偷给变量换类型。

## 安装与试用

解压可信源码包后，在包目录运行：

```sh
# 只使用编译器：无第三方运行依赖。
python3 --version       # 应为 Python 3.13+
gcc --version
./xe --version
./xe doctor --cc gcc
./xe run examples/args/main.xe --cc gcc -- hello "two words" "你好 Xe" ""
./xe run examples/feature_check/main.xe --cc gcc -- check
```

也可用 `python3 -m compiler` 启动。需要开发检查时，再运行
`uv sync --locked --dev`；后续 Makefile 使用冻结锁和离线已安装工具。
不会自动改 PATH、shell 配置或系统 Python。`doctor` 只报告环境，不证明其他平台兼容。

## 本地发布验收

先准备 uv 与锁定开发环境；运行时源码包冒烟本身不需要 uv 或仓库 .venv。

```sh
uv sync --locked --dev
make release-check CC=gcc
```

统一入口顺序执行：锁文件一致性、Pyright/Ruff、全量 unittest，再打包和干净解包
冒烟。全量回归包含真正的 C 编译/运行、错误程序、ASan/UBSan/default LeakSanitizer、
标准库/模块/线程和三代自举固定点；不通过关闭泄漏检测来“通过”验收。
Linux 受 ptrace 限制的容器/沙箱可能使 LeakSanitizer 自己失败，需要在允许它运行的
环境复验，不能把该失败解释成测试成功。

打包与冒烟可单独运行，但不能代替全量门禁：

```sh
make release-package
make release-smoke CC=gcc
(cd target/releases && sha256sum -c xelanguage-1.0.0-rc.1-source.tar.gz.sha256)
```

生成 `target/releases/xelanguage-1.0.0-rc.1-source.tar.gz`、`.sha256` 和外部
`xelanguage-1.0.0-rc.1-manifest.json`。包内也保存清单：逐文件 SHA-256、大小/权限、
版本、AST 标识、Git HEAD 与工作树是否有未提交修改。mtime/owner/gzip 时间归一化，
相同源码快照及 Git 元数据产生相同包字节，不声称本机二进制可跨平台字节复现。
默认包括新增但未提交的白名单源码，防止这轮新功能漏包；不会冒充 HEAD 已包含这些变化。

最终用于标签的候选包必须来自已提交的干净源码：

```sh
make release-package RELEASE_FLAGS=--require-clean
make release-smoke CC=gcc
```

`--require-clean` 不仅检查 Git 状态，还逐文件与 HEAD 比较；忽略规则或
assume-unchanged 不能把额外/修改过的源码伪装成正式快照。清单记录
`source_snapshot: git-head` 及对应提交；开发快照则明确标记 working-tree。

白名单排除 .git、.venv、target、常见密钥路径和本地配置，拒绝符号链接与不安全
压缩包路径；它不是任意源码内容的秘密扫描。不要同时编辑源码和制作最终候选包。
哈希检验完整性，不认证发布者；smoke 会执行包里的编译器，不是安全沙箱，只验收可信包。

冒烟在新的临时目录解包，清除 Python 路径、激活环境及用户 site 的影响，使用基础
Python，从包外工作目录实际运行入口、检查正反例、构建并运行参数示例、C FFI 和
多模块文件扫描。它证明“包不依赖本仓库环境/构建产物”，不是全新 OS 安装验证。
`.github/workflows/release-check.yml` 对 main、PR 和版本标签使用同一入口，要求干净提交；
本地通过不代表远程 CI 已通过。

## 转为公开稳定版前

1. 审核并提交经过验收的源码，在干净提交上制作候选包并核对该提交的 GitHub CI。
2. 以用户试用反馈检验候选范围；若发现 bug，先修复、补回归、更新 CHANGELOG 再生成下一 RC。
3. 确认转稳定版后统一更新 VERSION/PACKAGE_VERSION、pyproject/uv.lock 和说明，
   重新在干净提交上完整验收及打包，不只重命名旧 tarball。
4. 明确批准版本标签和外部发布，再发布与验收清单一致的包/哈希，并保留源码提交。

候选包只标记 local-candidate；工具不创建标签、不 push、不上传、不调用发行服务。
维护者按明确授权创建 `v1.0.0-rc.1` 标签并同步 GitHub 的 main/标签；这不等于
已上传发行附件或已宣布稳定 1.0。标签不能覆盖，远端分支不能强推。
Apache-2.0 许可已经确认，候选身份不增加与许可证冲突的使用限制。
