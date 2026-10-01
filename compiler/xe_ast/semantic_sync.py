"""并发库的静态契约；普通指针风险是警告，线程归属是资源限制。

这不是 Send/Sync Trait 求解器，不承诺检测所有数据竞争。只拒绝已知
不能跨线程释放的 MutexGuard，递归检查聚合和捕获环境，防止包装绕过。
"""
from .typesys import Type, NEVER, UNKNOWN, maybe


SYNC_TYPES = {"Shared", "Weak", "Mutex", "MutexGuard", "Thread"}
SYNC_MARKERS = {"Expired", "SyncError", "ThreadError"}


class SyncChecker:
    def sync_contains(self, type_, names, seen=None):
        if type_.name in names:
            return True
        seen = set() if seen is None else seen
        if type_ in seen:
            return False
        seen = seen | {type_}
        # 函数签名不是捕获存储。返回 Guard 的函数指针仍是静态代码地址，
        # 可以在线程内部创建该线程自己的 Guard；不能把签名当成已持锁资源。
        if type_.name == "fn":
            return False
        if type_.name == "closure":
            return any(self.sync_contains(t, names, seen) for _, t in self.closures[type_].captures)
        if any(self.sync_contains(t, names, seen) for t in type_.args):
            return True
        declaration = self.types.get(type_.name)
        if declaration:
            from .typesys import substitute
            bindings = {"$" + p["name"]: t for p, t in zip(declaration.get("generics", []), type_.args)}
            fields = [f["type"] for f in declaration.get("fields", [])]
            fields += [t for v in declaration.get("variants", []) for t in v["payload"]]
            # 只展开字段定义一次，避免递归泛型无限扩大类型。
            if any(t.name == type_.name for t in seen if t != type_):
                return False
            return any(self.sync_contains(substitute(self.type_of(f, self.generic_set(declaration)), bindings),
                                          names, seen) for f in fields)
        return False

    def sync_constructor(self, owner, member, node):
        from .semantic import Value
        if owner.name in {"Shared", "Mutex"} and member == "new":
            values = self.arguments(node["arguments"], [owner.args[0]], node)
            if values[0].type == NEVER:
                return Value(NEVER, node)
            error = "AllocError" if owner.name == "Shared" else "SyncError"
            return Value(maybe(owner, Type(error)), node, origins=values[0].origins)
        if owner.name != "Thread" or member != "spawn":
            return None
        values = self.arguments(node["arguments"], [UNKNOWN], node)
        callback = values[0]
        if callback.type == NEVER:
            return Value(NEVER, node)
        signature = callback.type
        if signature.name not in {"fn", "closure"} or len(signature.args) != 1:
            self.fail(node, "spawn 需要没有参数的函数或拥有闭包", "XE-THREAD-0001")
        if not self.compatible(signature.args[-1], owner.args[0]):
            self.fail(node, f"线程函数返回 {signature.args[-1]}，但 Thread 要求 {owner.args[0]}", "XE-THREAD-0001")
        if any(self.sync_contains(t, {"MutexGuard"}) for t in (signature, owner.args[0])):
            self.fail(node, "MutexGuard 必须在加锁线程释放，不能作为线程捕获或返回值（包括包装后）",
                      "XE-THREAD-0002", "在线程内部 lock，并在同一个线程退出其作用域")
        if self.sync_contains(signature, {"ptr", "str", "Slice", "SliceMut", "Bytes", "Chars"}):
            # 普通地址仍合法，只提醒其目标存活和同步需要调用者负责。
            self.warn_pointer(callback, "线程捕获包含普通指针或非拥有视图；请维护目标生命并同步并发访问",
                              "XE-PTR-0003")
        result = Value(maybe(owner, Type("ThreadError")), node, origins=callback.origins)
        if self.carries_borrow(owner.args[0]):
            self.warn_pointer(result, "线程结果包含普通指针或非拥有视图；join 等待完成但不延长所指数据的生命",
                              "XE-PTR-0003")
        return result
