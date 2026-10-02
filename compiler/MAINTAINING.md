# 怎样维护这版编译器

这里先回答“修改一个功能去哪里”，不要求先学完整编译原理。
统一入口是 ./xe / python -m compiler；compiler/main.py 保留旧脚本兼容。
Makefile 已把根环境和常用命令配好，整体结构见 [ARCHITECTURE.md](../ARCHITECTURE.md)。

## 先分清失败在哪一层

| 现象 | 先看 |
| --- | --- |
| 缺分号、括号不闭合、分支头写错 | parser.py；文字/字面量本身错误看 lexer.py |
| 名字不存在、类型不符、资源移动后使用 | semantic.py，按诊断编号找 fail 调用 |
| 找不到模块、导入冲突、跨模块访问私有声明 | modules.py；字段/方法权限也由 semantic.py 检查 |
| 检查通过，但报告后端尚未实现 | backend_c.py，增加真实编译执行验收 |
| 系统 C 编译器报错 | 生成的 <程序>.c 与 build.py；不是让用户改 Xe 来迁就错误 C |
| 程序崩溃、泄漏、重复释放 | 生成 C 的资源活跃标记、清理路径和 runtime/xe_runtime.h |
| Trait 契约/条件 Copy/泛型析构不符合预期 | semantic_traits.py；后端只使用已检查的具体实例 |
| 模式覆盖或过滤结果错误 | patterns.py 的覆盖分析、semantic.py 的选择器验证和 backend_c.py 的条件发射 |
| 全局初值/地址错误 | semantic_globals.py / backend_globals.py；标量计算共用 static_values.py |
| 外部函数 ABI/链接问题 | ffi.py 验证签名；build.py / driver.py 处理显式链接输入 |

make ast 只证明解析；make check 不会运行程序；make build 才调用系统编译器。
make run 真正执行，make audit 记录每一层。先找到失败层，避免同时改动所有文件。

模块 let 和 let[mut] 都注册到 globals/static_roots；constants 只是折叠初值的表，
不能再用“在 constants 中”推断它没有地址。只读/可写由 Binding.mutable 控制。
静态计算与运行库必须一致：f32 每一步舍入、整数除法向零截断、MIN % -1 为 0。
移位 helper 检查次数与左移溢出，不使用 C 的负数左移，也不依赖负数右移的实现定义行为。

Trait 的默认方法要保留其声明的泛型环境，不让 trait T 与 impl T 意外成为同一未知量。
具体 Drop 登记到前端检查队列，后端不能临时生成未经检查的正文；递归增长必须有明确
深度/实例预算和诊断，不能以 Python RecursionError 结束。元组选择的过滤成员与 handler
输入不同：过滤检查各成员，处理器仍接收完整元组，解包由用户显式写出。

C void 与 Xe Unit 的内部表示不同，所以 extern 使用桥接函数；不要把聚合或带捕获闭包
猜成兼容 C ABI。外部输入构建目前禁用缓存，因为头文件和库的传递依赖尚未完整登记。

标准库签名集中在 stdlib.py 的共用结构，以及 stdlib_io.py / stdlib_env.py 的接口表。
操作系统实现位于 stdlib/io 与 stdlib/env，生成 C 时内嵌，不依赖构建目录。
参数存储由 C 入口保存，Xe main 和 Drop 全部结束后才释放参数描述符表；不要在
函数 args() 返回时释放它，也不要让只读 Slice 因 let[mut] 获得元素写权限。
编译器 --run 的 -- 后参数必须以列表传给目标进程，不能用 shell 重新拼接。

## 一个小修改的顺序

1. 写最短源码复现，说明应该通过/拒绝以及预期结果。
2. 确认规则已有文档；新符号或改变旧含义先讨论，不能由实现悄悄决定。
3. 添加失败测试和正常测试，再只修改对应入口。
4. 涉及执行的功能必须真的构建运行，不能只比较生成 C 的字符串。
5. 运行 make compiler-test、make demo、make audit，再更新支持边界。

失败测试必须保留原来要测的错误。例如方法参数类型错误夹具，不能先被错误的 Copy<<
初始化挡住；应修夹具，而不是把期待的错误编号改成一个不相关编号。

## Python 静态检查

修改 Python 代码后可以先跑 `make python-check`：Pyright standard 与 Ruff 使用
根 pyproject.toml 的同一套规则，不用关闭编辑器检查来取得“没有红线”。
异构 AST 的值边界用 Any，明确的数据表和业务对象仍保留具体类型；新增 fail/error
辅助函数如果一定抛诊断，应标 NoReturn，让调用者的类型收窄与真实控制流一致。
辅助混入类的 cast 只用于确认已知宿主 Checker/CBackend，不能用来掩盖真正的
参数/返回类型不匹配。内部 assert 记录已经由前置步骤保证的不变量，不替代用户诊断。
测试找不到 cc 时用空字符串代表不可用，仍通过 skipUnless 跳过；不要用默认 "cc"
冒充检测成功，也不要把 None 放进 subprocess 的命令参数中。

## 三个关键数据结构

- AST：保留用户写了什么和在哪里；不偷偷加入取地址、unsafe、成功包装或 drop。
- Type / Value / Binding：Type 是含义，Value 是当前表达式及来源，Binding 是某个存储位置。
  变量 uid 区分同名遮蔽；名字相同不代表同一个对象。
- 后端 Slot / CValue：保存 C 存储和资源是否仍活跃。移动不是释放；接收者稍后释放。

理解指针时分别问：“访问哪个存储位置？”和“复制出的值还依赖谁？”
例如数组元素的地址依赖数组存储；复制纯整数不依赖它；str 描述符的复制仍依赖字符拥有者。
不能因为元素类型是整数就漏掉 x[i]@ 的地址生命周期，也不能因为是指针就取得资源所有权。

## 不要破坏这些不变量

- 实参、字段、数组元素从左到右求值；后一个表达式提前 return 时，前面已经创建的资源要清理。
- 一个资源只能被一个拥有者释放。移动、部分移动、忽略参数和提前退出都需测试。
- Never 表示不再产生值，不能在生成 C 时把它当作整数 0 初始化结构体。
- 类型/写权限/所有权错误必须拒绝；指针风险则给 warning 并传播 unsafe，不能用过去的
  独占借用规则禁止合法别名，也不能因无法证明内存安全而阻止普通指针程序编译。
- T@ 与 T@[mut] 都 Copy；复制地址不取得资源所有权。用户类型必须显式声明 Copy。
- T@[mut] 可以降为 T@，反向不行；这只改变外层指针的访问权限。不能递归改变
  T@@ 的内层指针、容器元素或函数签名，否则可能绕过只读限制。
- 本地函数返回来源摘要必须传播到稳定；不能任意截断轮数来让测试看起来更快。
- 未实现是能力诊断，非法代码是语言诊断，系统工具错误是工具诊断；不要混成同一类。

## 指针风险信息怎样流动

Checker.check() 只返回阻断编译的错误，Checker.warnings 单独保存非阻断诊断。
不要用“有 diagnostics 就失败”来处理两种级别。CLI、构建驱动和审计工具都需要展示警告。

Type.unsafe 是风险注记，相等和哈希忽略它，因此不会生成另一套 C ABI。
convert / common 使用 merge_unsafe 保留注记；声明写了普通 T@ 不应洗掉传入值的风险。
Value.origins 跟踪已知的地址来源，invalid_roots 记录失效的存储，warn_pointer 添加定位警告。
跨函数返回来源与风险摘要迭代到稳定；传参、枚举包装、字段提取、管道和函数值都有回归测试。
这只是保守且不完整的提示分析，不是借用求解器，也不保证发现所有释放、越界和并发问题。

inferred_types 记录语义信息，AST JSON 不插入用户没有写过的 unsafe。
后端仍用同一 C 指针表示；普通指针不带析构活跃标记，不负责释放所指资源。

## 闭包与迭代器：分清环境和参数

ClosureInfo 保存捕获字段、read/mut/once 调用能力和返回地址来源。
fn[x] 捕获拥有值，fn[x@] 捕获地址。地址捕获的环境字段实际为 T@，
但正文里同名绑定仍是原始 T 的非拥有别名；不要把隐藏字段类型泄漏给用户变量。
若 x 本身是 T@，fn[x] 的正文 x 仍是 T@，只有 fn[x@] 才在环境里保存 T@@。
正文决定调用能力，不由捕获方式决定；写入地址别名不等于写闭包环境字段。
普通 f() 是方法式接收者调用：read/mut 环境保留，once 才移动环境。
callback 作为普通函数参数传递仍可能移动，不能混淆这两个位置。
闭包类型身份必须区分具体 AST 表达式和泛型实例，不能只用源码偏移或函数签名做缓存键。

后端 callable_argument 固定接收者的地址或拥有值，隐藏函数访问原位环境字段。
只读/可写调用不能复制出假拥有环境、也不能清理外部字段；一次性调用则清理剩余捕获。
闭包所有权用已有 Slot.flags、fields 和 drop_complete 管理，避免另造一套析构机制。

for_iterators 记录自定义 next 的具体实例；FromFn 布局是 callback + done。
next 调用内部闭包的可写指针，保留环境；首次 Step::Stop 以后不再执行回调。
iterator_loop 外层 scope 拥有迭代器，内层 scope 拥有本轮元素，break/continue/return
必须覆盖不同清理范围。条件块内声明的资源临时 Slot 不能留到块外清理，否则生成 C
会引用超出作用域的变量。修改后运行 test_backend_closures 与 test_iterators 的实际 sanitizer 验收。

## 泛型不要用“未知类型”蒙混过关

泛型声明是模板；调用给出具体类型后，才生成独立的函数体和数据布局。
Checker.instantiate 保存实例缓存和具体签名，call_targets 把调用/函数值连接到具体实例。
原始 AST 仍保留用户的声明和位置；不要把隐藏函数写进用户的 AST JSON。

普通值名字与类型参数可能同名。只能在类型位置或类型限定路径里代入 T，不能全局搜索
替换字符串，也不能把 values[T] 中的值变量 T 改成类型名。实例中的具体类型环境需要
独立保存，泛型体中的下一次显式类型应用才能继续解析。
读取另一个类型声明的字段/载荷时，必须使用该声明的泛型环境，不能让当前函数的 T
覆盖全局用户类型 T 或另一个类型族的 T；后端读取声明时也遵守这一隔离。

实例的参数和返回值必须使用具体 Copy/移动规则重新检查，不能只检查模板一次。
新实例可以发现更多实例；函数返回来源摘要也可能变化，因此检查队列和摘要都要达到稳定。
同类型的递归必须复用缓存；不断增大的递归类型应用需要有数量上限和定位诊断，不能
让编译器耗尽内存、溢出 Python 调用栈，或者生成无法链接的 C。

unsafe 不区分 C ABI，也不能让第一次调用的地址风险污染后来的安全地址。
缓存存储使用结构类型，调用的数据流仍单独传播风险；源码显式的 [unsafe] 不能被清除。
泛型布局的字段/载荷按具体类型代入，自动清理仍恰好执行一次。

## 加载模块与拥有容器

modules.py 从入口递归加载可达文件，先登记模块和声明，再解析导入与函数正文。
同包环不按文件顺序决定含义；包依赖环另行报错。内部模块名称以 !module! 标识，
不能使用 $ 前缀，因为语义层已用它表示泛型变量。
所有内部 AST 节点保留 _file / _module；泛型实例的新节点也必须保留来源，
否则会报错到错误文件，或绕过私有方法检查。默认 AST JSON 仍来自未改写的单文件树。

Vec 的布局为 data / len / cap，缓冲区只被拥有容器释放。push 移交元素，pop 移出
并减少长度，clear 逆序释放元素但保留缓冲区；复制指针或切片不复制元素所有权。
backend_containers.py 为具体 Vec 类型登记清理函数，先生成原型再生成函数体，
让 Vec[Node] 的递归清理使用函数调用而不是无限展开生成代码。
扩容必须检查乘法/加法溢出；旧地址可能失效，语义层传播风险但不宣称安全。
维护这部分时运行 test_bootstrap_library、test_modules 和 test_source_scan_project，
不能用关闭 LeakSanitizer 的方式掩盖重复释放或泄漏。

Box 的布局只有 data，Box@ 指向描述符，ptr()/ptr_mut() 才返回其中的 T@。
new 先按普通参数规则取得 T，再用可失败的 xe_box_alloc 分配；失败分支要立即清理
该实参并撤销临时拥有标志，不能留到函数退出时重复 drop。成功则把所有权转进 Box。
into_value 转出 T 后只 free 外壳，绝不能调用 Box drop 再析构已转出的 T。
backend_containers.py 将 Vec/Box 放在同一具体类型清理缓存中，互相递归只调用已登记函数；
实例深度/数量限制沿用现有诊断，不能递归展开到 Python 崩溃。
test_box 用临时生成的 C 替换 xe_box_alloc 模拟 malloc 失败，不在正式语法加入测试附件。
Box 按绑定来源追踪指针风险，跨移动堆地址身份尚不精确；允许保守 warning，不拒绝程序。
内部 Value.borrowed 是“不具备资源移出权限的存储访问”标志，不是用户类型或借用协议；
数组/向量下标的类型仍是 T，显式 @ 才返回指针。

## 并发库：计数、数据、环境分开

semantic_sync.py 拒绝实际 Guard 存储跨线程（包括包装），函数签名不等于捕获存储。
原始指针风险仍给 warning，不将未完成的 Send/Sync 求解伪装成静态安全保证。
backend_sync.py 为具体类型生成描述符、T 析构回调和线程 job；线程 job 的第一个字段
是 T 结果，后面是具体闭包环境。spawn 前初始化环境，成功后环境归线程，失败则归调用者。
join 等待后转出 T 并释放 job；自动析构等待后 drop T。两条路径不可同时清理结果。
pthread_mutex_t 存在稳定控制块中，不能按值复制或把它直接放入可移动的 Xe 描述符。
Guard 自己保留一个控制块计数，释放顺序为检查线程归属、解锁、减少计数。
Shared/Weak 计数规则与互斥保护数据的规则分离；最后强计数的隐含弱计数保护 T 析构过程。
运行 test_sync（含失败注入、并发弱升级、跳转析构、ASan/UBSan 与泄漏检测）。
生成 C 使用并发运行库时，build.py 和 audit.py 自动加 -pthread；普通程序不引入 pthread。
ASan/UBSan 不检测所有数据竞争，不能将通过这些测试写成 ThreadSanitizer 已通过。

## 回归测试地图

发布候选验收统一运行 `make release-check CC=gcc`。release_metadata 固定 CLI/AST/
项目/锁文件版本和旧脚本入口；release_capabilities 固定 check/emit 能力诊断；
runtime_locations 验收运行期 Xe 位置；release 检查包内容/安全解包与包外实际运行，
release_ci 只检验工作流策略，不冒称远程执行。源码发布范围见根 RELEASE.md。

0.9 的 tuple[...] 同时用于类型、值和解包，解析时须按明确的语法位置区分。
解包不能反复计算右侧，也不能把先写入的目标当作后续源成员；资源忽略、旧值替换与
提前 return 都需要验证恰好一次清理。`let tuple[...]` 创建新绑定，已有目标赋值遵守可变性。

透明类型别名在类型解析时展开，支持顶层前向引用和链；名称循环应给定位诊断，
不能靠 Python 递归异常结束。别名不形成新的 C 布局，不授予 Copy、写权限或资源所有权。
原始 AST 保留 TypeAlias 声明与源码位置，语义类型和后端使用展开后的具体类型。

compiler/tests 中 frontend/cli 测结构和位置，semantic/data_declarations 测规则；
backend、backend_branches、backend_results、backend_functions 真正编译运行；
closure_semantic 检查捕获/调用能力/地址摘要，backend_closures 验收实际环境调用与析构；
iterators 验收 next/from_fn、具体类型、退出清理和返回来源，examples/iterators 可独立运行。
array_borrows 和 result_conversion 测类型/所有权与指针风险边界；calculator_project 验收真实解释器。
explicit_copy、pointer_revision_syntax、pointer_warnings 验收沿用自 0.8 的显式复制、单层匹配及风险传播。
generic_parser、generic_semantic、generic_backend、generic_integration 分别验收类型应用解析、
具体实例检查、实际 C 执行以及跨阶段的缓存/风险/资源边界；pointer_weakening 验收权限降级。
tests/warnings 只验收编译和风险注记，不统一运行可能产生未定义行为的程序。
audit 默认把 XE-PTR warning 程序标记 warning_not_run；不要为了报告全绿打开 --run-warnings。
运行测试的临时目录自动回收，不应把生成产物当源码提交。

ASan 检测部分内存错误，UBSan 检测部分未定义行为。两者很有用，但不能替代语义检查，
也不证明没有其他漏洞。生成 C 使用 GCC/Clang 兼容的工具链。

当单文件规则明显过大时，可以按职责抽取小模块；先保持测试与外部行为不变，
再做结构整理。不要一边发明语法、一边更换算法、一边改变后端，以免失去可定位的失败原因。

## Xe 自举子集

`bootstrap/compiler.xe` 独立实现一个可自编译子集，Python stage0 仍是参考实现与 seed
编译器。`bootstrap/verify.py` 只在 seed 步骤调用 stage0；不允许后来各代借用 Python
前端或替换/预处理输入源码。`test_selfhost` 验收固定点、参考行为、跨代失败诊断、
资源/算术/Unicode 边界；修改 stage0 公共语义或运行库后也必须运行它。

新编译器使用 Vec 表和整数 ID，不保存 Vec 元素地址；源码 String 必须在其所有 Token
视图之后释放，不能解析中修改；输出缓冲不得作为永久名称视图的拥有者。其清零析构
策略只适合已实现的内建资源及默认成员析构，不能擅自用于用户 Drop。下一阶段扩大
功能时要保持诊断与资源检查，而不是为了自编译成功绕过规则。
边界及代码导航见 [bootstrap/README](../bootstrap/README.md) 和 [第 31 章](../doc/31.md)。

## 原版编译器工具链

仓库根 xe 仅负责选择根目录解释器；python -m compiler 与它进入同一个 toolchain.main。
project 选择项目/入口，不自行改写模块路径；所有语言检查仍由 modules/Checker/C 后端
执行。原 cli/main.py/Makefile 入口继续可用，不因统一命令更改旧默认输出路径。

driver 每次重新加载并检查源码，缓存只跳过最后 C 编译。缓存键必须覆盖实际 C 文本、
输入/清单、编译器源码、C 工具身份、编译参数与相关环境；自定义 C 参数可能引入
额外输入，所以当前禁用缓存。不要只依靠 mtime 或缓存命中而跳过 warning。
build.compile_generated 编译私有 C 快照，在发布前登记自己生成程序的哈希，原子替换
可执行文件；失败不能覆盖旧程序。不要改回读取可被其他任务改写的共享 .c 来编译。

artifacts 收据不是可信代码。clean 核对根目录、目标路径和内容哈希，只删明确的普通文件；
外部输出、未知/修改产物、符号链接、损坏收据和 target/bootstrap 都保留，绝不递归
删除 target。不要并行 clean 与 build/run，也不要让不同任务共用一个自定义输出。
输出保护包含源码/清单的符号链接和硬链接别名；写产物时先保护后发布。

formatting 不重新打印整棵 AST，只整理现有行；字面量/注释内容保持不变，变换后再次解析
并核对忽略源码范围的结构。写入前所有输入先验证；没有官方全量格式规则时不能擅自
重写表达式或设计新排版语法。documentation 读取加载器改写前的树，保留用户名称、
源码位置与注释；仅展示本包可达公开 API，不泄露私有成员或自动执行文档示例。

test 只运行明确提供的带 main 的 Xe 程序，--compiler 才启动现有 unittest；不把
tests/fails/warnings 或所有示例作为可自动执行的单元测试。运行目标不拼接 shell，run
保持调用者的 cwd/stdin/终端与 argv；测试用例 stdin 为 EOF、有限超时、限长报告。
test_toolchain 使用独立临时项目验证真实运行、缓存失效、参数、格式/文档、失败保护和
清理范围，含保留 LeakSanitizer 的资源测试。运行 make toolchain-test；改共用 build.py
还要运行完整 make compiler-test。用户命令与限制见 [第 32 章](../doc/32.md)。
