# 指针风险警告

本目录保存合法但可能存在地址风险的程序。应解析、检查并生成 C/可执行文件，断言 warning
及推导的 unsafe 注记；不要把本目录作为统一运行测试，尤其不要解引用可能悬垂的指针。

- return_local_pointer.xe：XE-PTR-0001，旧 tests/fails/semantic_return_local_borrow.xe。
- owner_moved_pointer.xe：XE-PTR-0002，资源被移动后，已有指针可能失效。
- return_local_view.xe：XE-PTR-0001，String 析构后，其 str 视图的地址可能悬垂。

旧 tests/fails/borrow_alias.xe 已迁移到 language/pointer_alias.xe：普通指针别名完全合法，
无需 warning；两个可写指针指向同一个对象也不属于独占借用错误。
资源移动后使用资源本身、只读指针写入、由 p# 盗取资源仍属于必须失败的测试。
warning 不能当成内存安全证明，风险传播不能通过省略目标类型的 unsafe 附件洗掉。
