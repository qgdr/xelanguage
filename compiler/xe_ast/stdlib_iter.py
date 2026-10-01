"""最小拉取式迭代器接口：保存一个回调，重复调用它获得 Step[T]。

FromFn[F] 的 F 是具体回调类型（可为匿名闭包），不是元素类型。
不需要动态分派或堆分配；后端布局只有 callback 与 done 两个字段。
这不是 yield 状态机：每次调用从回调正文的开头运行，状态由捕获保存。
"""
from .typesys import Type

FROM_FN = "std::iter::from_fn"


def callback_type(iterator: Type) -> Type:
    return iterator.args[0]


def callback_signature(iterator: Type) -> Type:
    callback = callback_type(iterator)
    return callback.args[0] if callback.name == "ptr" else callback


def next_result(iterator: Type) -> Type:
    return callback_signature(iterator).args[-1]
