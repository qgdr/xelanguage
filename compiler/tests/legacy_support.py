"""显式选择玩具编译器基线的测试支架，不替新核心语法提供隐式兼容。

旧样例验证旧实现，不能因同仓库的测试通过而升级为下一版设计。上下文只在
一次测试/fixture 调用期间有效，退出后恢复；不依赖环境变量，也不检测调用栈。
子进程无法继承这个 Python 上下文，旧 CLI 测试仍必须显式传 legacy 选项。
"""
import unittest
from functools import wraps

from compiler.xe_ast.language_policy import language_profile


class LegacyTestCase(unittest.TestCase):
    """所有采用旧源码语法的测试都必须在基类声明中显式选择此契约。"""

    def run(self, result=None):
        # unittest 在 run 内调用实例 setUp/test/tearDown/cleanups；无论测试
        # 成功、失败或抛异常，上下文管理器都会恢复调用者原来的 profile。
        with language_profile("legacy"):
            return super().run(result)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # unittest 的类级 fixture 在实例 run 以外调用；只包装本类真正覆盖的
        # hook，不把继承 hook 再绑定到父类，避免改变 super() 与 cls 的含义。
        for name in ("setUpClass", "tearDownClass"):
            original = cls.__dict__.get(name)
            if original is None:
                continue
            if not isinstance(original, classmethod):
                raise TypeError(f"{cls.__name__}.{name} 必须声明为 classmethod")

            @wraps(original.__func__)
            def legacy_hook(subclass, *args, _hook=original.__func__, **hook_kwargs):
                with language_profile("legacy"):
                    return _hook(subclass, *args, **hook_kwargs)

            setattr(cls, name, classmethod(legacy_hook))
