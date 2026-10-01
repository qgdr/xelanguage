"""进程参数是标准库能力，不改变 Xe main 的签名或增加语言语法。

只读切片及其 str 元素指向进程入口保留的参数存储，可复制，不需要转移或
释放原操作系统字符串。C 入口在 Xe main（含其 Drop）结束后清理描述符表。
不向 prelude 注入 args 名字，以免常见局部名称被标准库含义占用。
"""
from .stdlib import StandardFunction
from .typesys import IO_ERROR, STR, Type, maybe

ENV_ARGS_RESULT = maybe(Type("Slice", (STR,)), IO_ERROR)
ENV_FUNCTIONS = {"std::env::args": StandardFunction((), ENV_ARGS_RESULT)}


def env_function(name: str) -> StandardFunction | None:
    return ENV_FUNCTIONS.get(name)
