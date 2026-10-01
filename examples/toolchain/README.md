# 原版编译器的统一工具链示例

这是普通 Xe 项目，不使用自举编译器。所有命令从仓库根运行：

```sh
./xe check --manifest-path examples/toolchain
./xe build --manifest-path examples/toolchain
./xe run --manifest-path examples/toolchain
./xe run --manifest-path examples/toolchain --bin smoke
./xe test examples/toolchain/src/bin/smoke.xe
./xe fmt --manifest-path examples/toolchain --check
./xe doc --manifest-path examples/toolchain
./xe clean --manifest-path examples/toolchain --dry-run
```

默认程序输出 `square(7) = 49`，smoke 输出 `PASS square`。构建产物放在本项目的
`target/debug/`，公开 API 清单放在 `target/doc/index.md`。重复 build 会复用未变的程序，
但仍检查源码；`--release` 使用 `target/release/`，`--rebuild` 强制系统 C 编译。

从项目目录运行，无须反复传入清单：

```sh
cd examples/toolchain
../../xe run
../../xe run --bin smoke
../../xe doc
```

`src/math.xe` 保存公共计算函数；`src/lib.xe` 公开再导出；main 与 smoke 都显式使用
同一模块。smoke 是普通 `main() -> i32` 程序，0 表示通过，非零表示失败，没有新增
`#[test]`、assert 关键字或隐式测试函数发现。完整行为与限制见 [第 32 章](../../doc/32.md)。
