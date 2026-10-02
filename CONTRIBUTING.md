# 贡献与维护

先阅读 [AGENTS.md](AGENTS.md)、[语言文档](doc/00.md) 与 [发布范围](RELEASE.md)。
新的语法或语义约定先与设计者讨论；不要暗中添加借用、隐式复制或特殊函数规则。

## 开发环境

当前验收 Linux x86_64 / GCC 13 / Python 3.13。编译器运行只需 Python 标准库与系统 C 工具，
开发工具用根目录 uv 锁文件管理，不另建 compiler 环境。

```sh
uv sync --locked --dev
make python-check
make compiler-test CC=gcc
./xe run examples/args/main.xe --cc gcc -- hello "two words" "你好 Xe" ""
```

针对文件可用 `./xe check file.xe`、`./xe ast file.xe`、`./xe run file.xe`；
先查看 `./xe --help` / 子命令帮助。AST-only 不检验所有权或后端能力。

## 一个修改应包含什么

- 解释要解决的问题，以及是否改变已审核的语言含义。
- 保持职责清楚，注释说明约束和原因，不堆积重复逻辑。
- 正例、语法/语义反例及真实构建运行回归；资源变更还要检查析构次序、提前退出和泄漏。
- 更新相应文档/示例和 CHANGELOG；历史记录注明时效，不伪改历史测试数量。

compiler/tests 是自动化测试入口；tests/language 是当前合法样例，
tests/legacy 不参与现行能力判定。风险 warning 样例不应全部执行。
ASan/UBSan/default LeakSanitizer 必须真正运行；沙箱 ptrace 限制使其失败时，
到允许检查的环境复验，不关闭泄漏检查或把环境失败写成通过。

保留用户工作树修改；生成物放 target/，不提交凭据、环境、日志或预编译程序。
清理先用 `./xe clean --dry-run` 核对；它只处理登记产物，不是递归删除目录。

## 发布候选版

```sh
make release-check CC=gcc
# 审查并提交、工作树干净后，制作绑定该提交的正式候选源码包：
uv run --project . --frozen --offline python -m compiler.release package \
    --output-dir target/releases --require-clean
make release-smoke CC=gcc
```

完整步骤见 [RELEASE.md](RELEASE.md)。包从白名单源码产生，不是包含 .git/.vscode/环境的
仓库镜像。提交、标签、push 和上传要有明确授权，不能强推或覆盖标签。
本地验收不证明远端 CI 通过；回归通过不证明语言或程序完全无误。
