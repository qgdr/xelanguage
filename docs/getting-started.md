# 快速开始

[文档首页](README.md) · [语言手册](language.md)

## 准备环境

验收环境为Linux x86_64、Python 3.13和GCC 13。编译器运行无第三方Python依赖。
解压源码包后进入其根目录；从Git仓库试用时也在仓库根目录执行：

```sh
python3 --version
gcc --version
./xe --version
./xe doctor --cc gcc
```

Python须为3.13或更新版本；其他版本未作为本版验收环境。
如果仓库已安装根`.venv`，启动器优先使用它；源码包不包含这个环境。

## 第一个程序

将下面的完整程序保存为`hello.xe`：

```xe-core
fn main() -> Unit {
    let answer: i32 = 42;
    println("hello, Xe: {}", answer);
}
```

运行：

```sh
./xe check hello.xe
./xe run hello.xe --cc gcc
```

输出`hello, Xe: 42`。`check`只检查；`run`先构建，再执行程序。
所有局部变量、函数参数和返回类型都明确写出，便于阅读和定位错误。

## 生成可执行文件

```sh
./xe build hello.xe --cc gcc -o target/hello
./target/hello
./xe emit-c hello.xe -o target/hello.c
./xe ast hello.xe -o target/hello.ast.json
```

生成的C是实现产物，程序仍以Xe源码规定的行为为准。
如果输出文件已存在，构建失败不会用失败产物覆盖先前有效文件。

## 接下来

先按[阅读顺序](language.md)理解盒子、量、指针以及复制/转移。
[examples/core](../examples/core/README.md)提供当前语言的独立样例。
编译器会拒绝超出当前支持范围的形式；遇到这类诊断时先查[支持表](support.md)，
不要通过切换旧语言兼容模式猜测新核心的含义。
