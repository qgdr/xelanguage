# Xe 多线程计数器

两个线程各累加 1000 次。Shared 保持计数器存活，Mutex 保护数据，
MutexGuard 在每次循环退出时自动解锁，Thread.join 等待并取得工作结果。

```sh
make thread-demo
```

输出：`completed=2000 counter=2000`。

普通指针不是跨线程安全保证，不要让 guard.ptr_mut() 的地址离开持锁作用域。
先释放锁再等待需要这把锁的线程，避免死锁。
当前实现依赖 POSIX pthread；完整规则见 [第 29 章](../../doc/29.md)。

Linux 且 C 编译器支持 ThreadSanitizer 时，可以另行检查本例中的数据竞争：

```sh
make emit-c SOURCE=examples/threads/main.xe C_OUTPUT=target/c/threads.c
cc -std=c11 -pthread -g -fsanitize=thread -no-pie target/c/threads.c -o target/c/threads-tsan
./target/c/threads-tsan
```

这不能和 ASan 同时启用，也不能证明所有程序都没有数据竞争。
