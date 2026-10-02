# 调用已有 C 函数

`main.xe` 声明外部函数，`functions.c` 提供实现。Xe 检查声明的类型和参数权限；
构建时用 `--link-input` 明确加入实现文件，不在导入模块时执行它。

在仓库根运行：

```sh
./xe run examples/ffi/main.xe --link-input examples/ffi/functions.c
```

输出 `sum=42 after=41`。也可以传 `.o` 或 `.a`，多个输入重复写 `--link-input`。
C 指针不自动获得所有权或延长对象生命期；C 实现的正确性由其作者负责。
当前 ABI 不接受 Xe 的 String、str、tuple、结果枚举等聚合及其指针。
详见 [有限 C 接口](../../doc/36.md)。
