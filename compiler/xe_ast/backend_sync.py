"""并发库的 C 降低：描述符可移动，控制块/锁/线程环境地址稳定。

复用已有资源临时变量与活跃标记，成功才转交；失败立即清理普通
实参。线程入口拥有闭包环境，线程结果归 join 或句柄析构二选一。
"""
from .typesys import Type, NEVER


class SyncBackend:
    def sync_value_drop(self, owner):
        return self.container_helper(owner) + "_value"

    def sync_new(self, node, owner):
        element = owner.args[0]
        value = self.argument(self.expression(node["arguments"][0], element))
        if value.type == NEVER:
            return value
        if element.name in self.checker.types:
            self.define_type(element)
        result_type = self.type_at(node)
        result = self.temp(result_type, f"({self.ctype(result_type)}){{.tag = 1}}")
        data, state, error = self.fresh("sync_data"), self.fresh("sync_state"), self.fresh("sync_error")
        self.line(f"{self.ctype(element)} *{data} = xe_sync_alloc(sizeof *{data});")
        native = "XeShared" if owner.name == "Shared" else "XeMutex"
        self.line(f"{native} *{state} = NULL;")
        if owner.name == "Mutex":
            self.line(f"int {error} = ENOMEM;")
        self.line(f"if ({data}) {{")
        self.indent += 1
        call = (f"xe_shared_new({data}, {self.sync_value_drop(owner)})" if owner.name == "Shared" else
                f"xe_mutex_new({data}, {self.sync_value_drop(owner)}, &{error})")
        self.line(f"{state} = {call};")
        self.indent -= 1
        self.line("}")
        self.line(f"if ({state}) {{")
        self.indent += 1
        self.line(f"*{data} = {value.code};")
        self.transfer(value)
        self.line(f"({self.payload_code(result.code, 'Yes', 0)}).state = {state};")
        self.line(f"({result.code}).tag = 0;")
        self.indent -= 1
        self.line("} else {")
        self.indent += 1
        self.line(f"free({data});")
        if value.slot:
            self.cleanup_slot(value.slot)
        self.line(f"{self.payload_code(result.code, 'No', 0)} = {error if owner.name == 'Mutex' else '0'};")
        self.indent -= 1
        self.line("}")
        return result

    def sync_container_body(self, type_, name):
        """与 Vec/Box 共用清理缓存，跨容器递归仍先原型后函数体。"""
        element = type_.args[0]
        if type_.name in {"Shared", "Mutex"}:
            self.line(f"static void {name}_value(void *data) {{")
            self.indent += 1
            self.drop_complete(element, f"*(({self.ctype(element)} *)data)")
            self.line("free(data);")
            self.indent -= 1
            self.line("}")
        self.line(f"static void {name}({self.ctype(type_)} *value) {{")
        self.indent += 1
        if type_.name == "MutexGuard":
            self.line("xe_guard_release(&value->state);")
        else:
            release = {"Shared": "xe_shared_release", "Weak": "xe_weak_release",
                       "Mutex": "xe_mutex_release", "Thread": "xe_thread_release"}[type_.name]
            self.line(f"{release}(value->state); value->state = NULL;")
        self.indent -= 1
        self.line("}")

    def sync_method(self, node, base, pointer, name, receiver):
        result_type = self.type_at(node)
        state = f"({pointer})->state"
        element = base.args[0]
        if name in {"ptr", "ptr_mut"}:
            data = f"({state}).state->data" if base.name == "MutexGuard" else f"({state})->data"
            return self.temp(result_type, f"({self.ctype(element)} *)({data})")
        if name in {"share", "weak"}:
            counter = "strong" if base.name == "Shared" and name == "share" else "weak"
            self.line(f"xe_ref_add(&({state})->{counter});")
            return self.temp(result_type, f"({self.ctype(result_type)}){{{state}}}")
        if name == "upgrade":
            result = self.temp(result_type, f"({self.ctype(result_type)}){{.tag = 1}}")
            self.line(f"if (xe_weak_upgrade({state})) {{")
            self.indent += 1
            self.line(f"({self.payload_code(result.code, 'Yes', 0)}).state = {state};")
            self.line(f"({result.code}).tag = 0;")
            self.indent -= 1
            self.line("}")
            return result
        if name == "lock":
            result = self.temp(result_type, f"({self.ctype(result_type)}){{.tag = 1}}")
            error = self.fresh("lock_error")
            self.line(f"int {error} = xe_mutex_lock({state});")
            self.line(f"{self.payload_code(result.code, 'No', 0)} = {error};")
            self.line(f"if (!{error}) {{")
            self.indent += 1
            self.line(f"({self.payload_code(result.code, 'Yes', 0)}).state = (XeMutexGuard){{{state}, pthread_self()}};")
            self.line(f"({result.code}).tag = 0;")
            self.indent -= 1
            self.line("}")
            return result
        if name == "join":
            # 类型化结果存在 job 开头，由线程写，join 建立同步后再读。
            result = self.temp(result_type, f"({self.ctype(result_type)}){{.tag = 0}}")
            data = self.fresh("joined_job")
            self.line(f"void *{data} = xe_thread_join({state});")
            self.line(f"{self.payload_code(result.code, 'Yes', 0)} = *({self.ctype(element)} *){data};")
            self.line(f"free({data}); {state} = NULL;")
            self.transfer(receiver)
            return result
        self.fail(node, f"并发接口 {base}::{name} 尚未实现")

    def thread_spawn(self, node, owner):
        callback = self.argument(self.expression(node["arguments"][0]))
        if callback.type == NEVER:
            return callback
        element = owner.args[0]
        key = (callback.type, element)
        if key not in self.thread_jobs:
            name = self.fresh("thread_job")
            # C struct 的第一个字段起始地址就是结果起始地址，join 不需要
            # 知道闭包的具体类型。避免为 Thread[T] 增加闭包类型附件。
            self.thread_jobs[key] = name
            if element.name in self.checker.types:
                self.define_type(element)
            self.thread_definitions[name] = (f"typedef struct {{ {self.ctype(element)} result; "
                f"{self.ctype(callback.type)} callback; }} {name};")
        name = self.thread_jobs[key]
        if callback.type.name == "closure":
            self.closure_function(callback.type, owning=True)
        self.container_helper(owner)
        result_type = self.type_at(node)
        result = self.temp(result_type, f"({self.ctype(result_type)}){{.tag = 1}}")
        job, state, error = self.fresh("thread_data"), self.fresh("thread_state"), self.fresh("thread_error")
        self.line(f"{name} *{job} = xe_sync_alloc(sizeof *{job});")
        self.line(f"XeThread *{state} = NULL; int {error} = ENOMEM;")
        self.line(f"if ({job}) {{")
        self.indent += 1
        self.line(f"{job}->callback = {callback.code};")
        self.line(f"{state} = xe_thread_new({job}, {name}_run, {name}_discard, &{error});")
        self.indent -= 1
        self.line("}")
        self.line(f"if ({state}) {{")
        self.indent += 1
        self.transfer(callback)
        self.line(f"({self.payload_code(result.code, 'Yes', 0)}).state = {state};")
        self.line(f"({result.code}).tag = 0;")
        self.indent -= 1
        self.line("} else {")
        self.indent += 1
        self.line(f"free({job});")
        if callback.slot:
            self.cleanup_slot(callback.slot)
        self.line(f"{self.payload_code(result.code, 'No', 0)} = {error};")
        self.indent -= 1
        self.line("}")
        return result

    def emit_thread_jobs(self):
        saved_lines, saved_indent = self.lines, self.indent
        prototypes, bodies = [], []
        for (callback, element), name in self.thread_jobs.items():
            self.lines, self.indent = [], 0
            prototypes += [f"static void *{name}_run(void *);", f"static void {name}_discard(void *);"]
            self.line(f"static void *{name}_run(void *opaque) {{")
            self.line(f"    {name} *job = opaque;")
            call = (f"{self.closure_function(callback, owning=True)}(job->callback)" if callback.name == "closure"
                    else "(job->callback)()")
            self.line(f"    job->result = {call};")
            self.line("    return NULL;")
            self.line("}")
            self.line(f"static void {name}_discard(void *opaque) {{")
            self.indent += 1
            self.line(f"{name} *job = opaque;")
            self.drop_complete(element, "job->result")
            self.line("free(job);")
            self.indent -= 1
            self.line("}")
            bodies.append("\n".join(self.lines))
        self.lines, self.indent = saved_lines, saved_indent
        return prototypes, bodies
