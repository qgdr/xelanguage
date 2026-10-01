# stage0 共享拥有、锁与线程

`xe_sync.h` 使用 C11 原子计数和 POSIX pthread。它处理控制块、锁和线程；
T 的类型布局与析构由 C 后端生成。数据对象不会因描述符移动而搬动，
Guard 会保持锁状态存活，最后一个 Shared 析构 T，Weak 不能复活已释放对象。

公开接口、错误返回、线程归属、自动等待和风险边界见 [第 29 章](../../doc/29.md)。
当前使用简短内建类型名，没有 std::sync / std::thread 模块路径登记。
运行 `make thread-demo`；资源与并发回归运行 `python -m unittest compiler.tests.test_sync`。
构建自动增加 -pthread。panic 不展开栈，非法自等待或不可恢复系统清理失败会终止进程。

计数线程安全不代表用户数据线程安全；Weak 升级不读取已析构的数据。
不要把 relaxed 的计数增加误改成非原子读写；最后一次计数减少建立 acquire/release 顺序。
强计数为零期间的一个隐含弱计数在 T 析构完成后才释放，防止控制块提前 free。
锁的 refs 包含句柄、等待者和 Guard；解锁后再释放 Guard 的 refs。
失败创建的线程没有开始执行，环境归调用者清理；成功创建后只有线程入口拥有捕获环境。
线程结果由 join 转出或自动等待后的 discard 清理，不能两者都清理。
