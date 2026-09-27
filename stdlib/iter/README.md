# std::iter 的 stage0 接口

`std::iter::from_fn(callback)` 返回 `FromFn[F]`；F 是具体回调类型。
回调无参数，返回 `T?`，必须能在保留环境时重复调用。
第一次 None 以后不再调用回调，next() 固定返回 None。

适配器保存回调和 done，C 后端生成具体布局，没有动态分派或强制堆分配。
按值回调属于迭代器；指针回调不拥有外部环境。next() 修改进度，要求可写对象或指针。
标准接口登记在 compiler/xe_ast/stdlib_iter.py；当前没有普通模块加载，也没有独立 Xe stdlib 包。
捕获资源随环境拥有者离开作用域释放，不因 next 返回 None 自动销毁外部对象。

完整协议、与 Rust 的区别和运行示例见 [第 26 章](../../doc/26.md)。
yield、map/filter 等通用适配器和 Iterator 关联类型/公共 Trait 尚未实现。
