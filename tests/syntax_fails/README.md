# 专门的语法失败用例

这里的 .xe 必须在词法或 AST 解析阶段报错，而不是依赖类型检查。
覆盖缺分号、缺表达式/类型/逗号、未结束的块/字符串/注释、非法字符、
旧模式箭头及缺少结果通道。

```sh
make ast SOURCE=tests/syntax_fails/missing_semicolon.xe
make compiler-test
```

失败退出码为 1，诊断包含文件、行列、源码和错误标记。解析失败不能创建或覆盖 AST。
tests/fails 则混合了已有迁移语法反例和语义反例；语义反例必须先成功解析，
然后由 make check 或 make check-safety 拒绝。预期编号登记在
compiler/tests/test_semantic.py，不能把任意错误当成符合预期。
