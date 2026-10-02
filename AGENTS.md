# Xe 实现契约与协作约定

本文件既指导本仓库，也可以独立复制到新项目，作为实现基本一致的 Xe 语言的起点。
这里的核心约定是规范，不是让实现者自由设计一门相似的语言。
仓库内完整说明见 doc/00.md，候选版本范围见 RELEASE.md，结构见 ARCHITECTURE.md。
独立项目没有这些文件时，仍应遵守下述核心规则；未规定的扩展先与语言设计者确认。

## 1. 目标和工作方式

目标是创造可用、易读、可以逐步自举的语言，不是研究编译器理论。
优先级：简单可靠、友好诊断、可维护、可扩展。可以用成熟解析器、简单递归解析、
C/LLVM 等现有后端；不要求自行实现优化、寄存器分配或垃圾回收算法。
代码按职责分层，解释重要约束和原因，让没有系统软件训练的维护者也能理解。

不得擅自增加关键字、符号或改变已审核语义。遇到设计空白，说明具体程序、歧义和
备选方案，请用户确认后再实现。修复违反既有语义的 bug 要补回归。
不因为熟悉 Rust 而偷偷引入借用、生命周期、隐式解引用、自动 clone 或特殊打印规则。
不把 AST 可解析、语义通过、后端支持、可以运行、内存安全混为一谈。

本候选版是 1.0.0-rc.1，语法标识 xe-1.0，AST schema 1。
完整前端是 Python stage0，生成自包含 C11，验收 Linux x86_64 / GCC 13 / Python 3.13。
Xe 自举项目只覆盖能编译自身的子集；不能把子集固定点宣传成完整语言自举。
Xe 不承诺完全内存安全、嵌入式或跨平台支持。

## 2. 外观、块和名称

源码为 UTF-8，扩展名 .xe，支持 // 和可嵌套 /* ... */ 注释。
语句以 ; 结束；{ ... expression } 的末尾无分号表达式是块的值。
空块或无尾值的正常块为 Unit；return、break、continue 不产生正常后续值。
换行只是空白，不结束表达式。() 用于调用和分组，不表示元组。
[] 用于类型/符号附件、泛型应用、枚举负载、数组、索引；由语法位置区分。
:: 用于路径和关联项，. 用于成员。单独的 : 用于名称的类型/约束注解；
:> 是完整的分支管道符号，不拆成类型冒号和大于号。

```xe
fn main() {
    let answer: i32 = { let a = 20; a + 22 };
    println("{}", answer);
}
```

基本类型包括 i8/i16/i32/i64/isize、u8/u16/u32/u64/usize、f32/f64、bool、char、
Unit、Never、str。char 是 Unicode 标量，str 是含地址和字节长度的只读 UTF-8 视图，
String 是拥有内存的字符串。字符串不是 C 的零结尾 char*。
类型位置允许括号分组。默认整数为 i32，浮点为 f64；上下文可以确定字面量类型，
但已有变量不因此偷偷改变类型。bool 不与整数隐式互换。
stage0 源位置使用 Unicode 码点 offset、从 1 起的行列、半开区间；
bootstrap 子集目前用字节位置，不冒充协议完全一致。
当前 Unicode 名称按 Python 字符分类识别、不规范化；跨宿主严格分类标准仍待审核，
不能擅自缩成 ASCII 或改成另一套 Unicode 规则。

## 3. 绑定、参数和 Copy

局部 let name: T = expression; 声明不可写绑定，类型可推导。
可写绑定统一写 let[mut] name ...;；var 仅保留为隐藏兼容糖，不作为推荐语法。
函数参数只写 name: T，参数绑定不可写；不允许参数名前加 let 或 mut，
也不使用 name[mut]。改变局部计数时，显式声明局部可写变量。
所指数据是否可写由指针类型决定，和指针变量本身能否重新赋值是两件事。

= 仅复制 Copy 值；<< 仅移动非 Copy 值，不能拿来替代普通赋值。
source >> destination; 按类型复制或移动，两类值均可用。
函数参数与普通管道输入按类型 Copy 或移动；不得因为函数名是 println 而特殊借用。
移动后源位置不可再读取；重新初始化需要合法写权限。

基本值、普通指针、str、无捕获函数值具有 Copy；数组/元组的 Copy 取决于成员。
用户结构体和枚举必须显式 impl Copy for Type {}，不自动默认 Copy。
实现 Copy 要求所有字段/所有变体负载均为 Copy，且不能同时实现自定义 Drop。
拥有资源的 String、Vec、Box、Shared、Weak 不能隐式复制；复制资源用明确接口，
例如 clone() 或共享指针的 share()，不是赋值运算符的隐藏行为。

```xe
struct Point { x: i32, y: i32, }
impl Copy for Point {}
fn main() {
    let[mut] point = Point { .x = 1; .y = 2; };
    point.x = 3;
    let text: String << String::from("Xe");
    println("{} {}", point.x, text@);
}
```

## 4. 指针及运算一致性

T@ 就是普通指针，value@ : T@，value@[mut] : T@[mut]，pointer# : T。
@ 取地址，# 解引用，均为后缀运算。指针不是拥有者，不延长对象生命期。
T@[mut] 可以写所指数据；T@ 不可以写。创建可写指针需要可写来源。
允许 T@[mut] 浅层降级为 T@，赋值、参数、返回均适用，降级后不恢复写权限。
不递归改变嵌套指针、容器或函数参数中的权限。
指针是 Copy，允许别名，允许反复把同一指针传给函数，不检查独占借用。

允许 p.field / p[index] / 方法查找自动越过一层指针；其他运算不自动解引用。
指针不能转移所指资源所有权：非 Copy 的 p# 不能传给按值消费函数或移动管道。
若所指类型是 Copy，允许读取/复制，包括调用接收 Self 的方法。
通过可写指针替换资源可以合法，但资源仍由原拥有者负责最终清理。

T@[unsafe] / T@[mut, unsafe] 只记录风险，不是新指针 ABI，不增加写权限或所有权。
可识别的悬垂/失效风险自动传播 unsafe 并 warning；warning 不阻止生成程序。
无法识别不意味着安全。没有赋予 unsafe { ... } 绕过规则的正式语义。
str@ 指向视图描述符；str@[mut] 可替换描述符，不等于可写 UTF-8 字节。

## 5. 资源清理和替换

块退出时自动清理仍拥有的资源，按实际初始化顺序的逆序执行。
正常尾值、return、break、continue 和 ?[return] 都必须清理退出的作用域。
移动出的资源不再次 Drop；内部活跃标记不属于用户语法。
impl Drop for Type { fn drop(self: Self@[mut]) { ... } } 定义自定义清理。
自定义 Drop 之后仍需清理其未移动的资源字段；Copy 与自定义 Drop 互斥。
普通结构允许部分移动和重初始化，但缺字段时不能读取/移动整体；
自定义 Drop 对象不允许移走非 Copy 字段后留下不完整自身。

替换已初始化资源，先求得独立右侧值，再清理旧值，再写入新值。
左侧对象/索引表达式不能重复求值；不能在执行右侧前 Drop 旧值。
索引目标在右侧完成后按已保存索引重验界、解析当前存储，不能跨 Vec 扩容保留旧地址；
父拥有者若已移动而未重建，访问其内部目标仍按已移动/未初始化报错。
资源移动、分支汇合、提前退出、字段初始化和替换共享同一所有权模型。
panic 直接终止，不承诺栈展开/Drop；不要用它检验正常析构。
手动调用 Drop 钩子和普通 drop 函数的边界尚待设计者审核，不能暗中新增禁止语法。

## 6. 结构体、枚举、方法和泛型

结构声明 struct Name { field: T, }，构造 Name { .field = value; }。
资源字段使用 .field << value;，也可 value >> .field;；不把 : 当初始化符号。
枚举 enum Token { End, Integer[i64], Identifier[String], }；
构造 Token::End / Token::Integer[42]，不是调用 Token::Integer(42)。
方法在 impl 中定义，self 类型必须是 Self/所属类型或其一层指针。
按值 self 对非 Copy 对象消耗自身，指针 self 保留原对象；
object.method() 按声明自动取合适的地址，不改变自由函数传参规则。

泛型声明附件在声明关键字后，泛型应用附件在被使用的名称后：

```xe
struct[T] Holder { value: T, }
fn[T] wrap(value: T) -> Holder[T] {
    Holder[T] { value >> .value; }
}
```

fn[T] 说明“声明泛型函数”，wrap[i32] 说明“使用 wrap 的实例”，二者作用对象不同。
不使用尖括号泛型，也不把泛型声明移到函数名后。
泛型按具体使用实例检查和生成代码；T: Trait 用于约束，
多个约束写 where T implements Copy, T implements Measure；不使用 + 合并约束。
静态 Trait 声明方法契约、默认方法及具体验证，条件实现例如
impl[T] Copy for Holder[T] where T implements Copy {}。
默认方法正文按实际使用的具体实现检查，可以访问其合法可见的字段/函数/全局；
不宣称在声明时已经证明任意未来实现都满足正文。
未使用泛型的正文没有“已验证所有可能实例”的保证。

## 7. 结果类型：构造和消解

类型 T? 是 T?[None] 的简写，概念展开 enum Maybe[T] { Yes[T], None, }；
T?[E] 概念展开 enum Maybe[T, E] { Yes[T], No[E], }。
E 是任意另一分支类型，不一定是错误；需要错误约束时写 E: Error。
这里的 Error 需要用户声明 Trait；当前没有预定义的 Error Trait。
None 在结果上下文表示无负载分支，不是可转成任何类型的 null。
显式构造用 Maybe::Yes[value]、Maybe::No[other]、Maybe::None。
函数返回 T? 时允许直接返回 T 或 None；返回 T?[E] 时允许直接返回 T，
失败值用明确的 No 构造。提升限于函数返回/其上下文流，不是任意赋值强转。
每个 ? 只消解一层，嵌套结果逐层处理。

类型的 ? 构造结果，表达式的 ? 选择/消解结果，是一对反向操作，不是同一个运算。
可在教学/编辑器中说明二者方向不同，不能声称 a? : T? 的后缀代数关系。
普通表达式 ? 必须完整处理分支：

```xe
fn positive(value: i32) -> i32? {
    if value > 0 { value } else { None }
}
fn main() {
    let number = positive(-1)?
        1> value -> value
        2> _ -> 0;
    println("{}", number);
}
```

?[return] 成功得到 T，失败从当前函数传播同类型失败分支；
?[panic] 成功得到 T，失败立即终止。没有裸 ? 自动错误传播。

## 8. 管道和闭包歧义

value |> handler 把输入作为函数参数；a |> f1 |> f2 >> b; 顺序处理后写入。
1>、2> 和模式 :> 右侧只能是可调用目标，或明确的分支参数绑定与正文。
a? 1> foo 2> bar; 表示调用 foo/bar，不返回函数值。
1> value -> expression 是立即执行的处理正文，不是闭包字面量。
返回 foo 函数值写 _ -> foo，返回闭包写 _ -> fn(...) { ... }。
不允许 1> 0、1> _ 作为值/身份缩写，也不允许 2> -> 0。
无负载分支调用零参数函数；_ -> expression 是忽略输入/无负载的统一适配器。
多个负载参数写 [x: T, y: U] -> expression。
正文是完整表达式；后续管道若属于整个分流结果，先用括号固定分流边界。

所有匿名函数/闭包以 fn 开始，不使用裸 x -> ... 的闭包字面量。
捕获显式列在 fn 后：`fn[a, text](...) { ... }`；Copy 值复制，资源移动。
不隐式捕获；保留资源时先明确 clone。
fn[current@[mut]] 捕获 current 的地址，但正文 current 仍是原 T 的可写别名，
写 current = current + 1，不加 #。只读捕获 fn[current@] 同理但不能写。
若原本捕获 p: T@，正文 p 仍是 T@，读取其目标需要 p#。
这是明确捕获语法，不允许在其他位置偷偷把变量变成指针或反过来。

每个带捕获闭包表达式有独立具体环境类型，都是非 Copy；
同签名不同环境不能自动合并为 if/分支结果或普通 fn 类型。
无捕获函数可作为 fn(A, B) -> R 值。
f(...) 是按调用能力选择 self 的方法式调用，不必为保留闭包写 (f@)(...)。
修改闭包自身拥有的环境需要可写 f；仅修改捕获地址的目标不需要可写 f。
移出捕获资源的闭包只能调用一次；参数仍按通常 Copy/移动规则传递。

## 9. 一层模式匹配

唯一形式 subject ? { selector :> handler, ... }，逗号分支，{} 固定范围。
模式只选择，不绑定变量；名称和类型在 :> 后声明。

```xe
enum Token { End, Integer[i64], Identifier[String], }
fn size(token: Token@) -> i64 {
    token ?[@] {
        Token::Integer :> number: i64@ -> number#,
        Token::Identifier :> text: String@ -> 1,
        Token::End :> _ -> 0,
    }
}
fn main() {
    let token << Token::Integer[42];
    println("{}", size(token@));
}
```

支持具名变体、字面量、_、直接负载过滤、tuple[...] 和 | 模式组合。
Token::Integer[0] 筛选值但不取消处理器的负载输入。
不递归展开，例如 Outer::Wrapped[Inner::Number[_]] 不合法；再写 ? 处理内层。
不能在选择器写 text@ 或 text: T 绑定；不使用旧 match / => 分支。
按源码顺序取首个匹配分支，检查穷尽性和完全不可达分支。
组合模式须有相同输入签名；所有正常分支结果须类型兼容。

按值匹配复制 Copy 或消费资源；?[@] 把每个负载 T 统一作为 T@ 传入，
基本值也不例外；?[@[mut]] 给 T@[mut]，需要可写来源。
负载本来是 U@，只读匹配得到 U@@，不自动展平。
对枚举指针匹配要明确指针模式，不能隐式取走资源。
裸 _ 选择器传整个对象；元组选择器也传整个元组，不自动拆成多参数。
有自定义 Drop 的枚举不能抽走资源负载后留下不完整自身。

## 10. 元组、容器和迭代

元组用 tuple[A, B] 类型、tuple[a, b] 值、.0/.1 成员；不复用 {} 或 ()。
非空单元素元组也用 tuple[...]；空值用 Unit，不增加 tuple[]。
解包 let tuple[a, b] = source; 或非 Copy 时 <<；let[mut] tuple[...] 声明可写名称。
已有目标可用 tuple[a, b] = source;、<<、source >> tuple[a, b];。
下划线忽略的资源仍正常清理；右侧只求值一次，再处理目标替换。
type A = B; 是透明顶层别名；type Handler = fn(i32) -> i32; 合法。
不承诺局部/泛型别名或创建新的名义类型。

Array[T, N] 是固定长度数组，通常放栈上；Vec[T] 拥有可增长堆存储。
array[index] : T，索引不偷偷变成 T@；取指针写 array[index]@。
非 Copy 元素不能从索引移走使容器留洞，Vec 可使用明确的 pop 接口。
Slice[T] / SliceMut[T] 是视图，不拥有元素；失效风险不能宣传为借用安全。
数组/切片/Vec 的 for 绑定元素指针；整数范围 start..end 绑定值且不含右端点。
自定义 next / from_fn 绑定返回元素 T，T 自身也可以是指针。
Step[T] 有 Item[T] 和 Stop；Stop 明确结束，不能把 T? 的 None 当结束。
from_fn 用闭包产生 Step，首次 Stop 后不再调用闭包。
yield 尚未设计完成，不因为实现迭代器而加入暂停/恢复语法。

## 11. 数值和位运算

整数算术越界、除零、非法移位报错或运行时 panic，不使用 C 未定义行为。
as 只允许类型整个值域都能无损表示的转换，不按变量当前值猜安全。
可能失败的整数转换用 u8::try_from(number) 返回 u8?[ConversionError]，
需要立即成功值可用 u8::try_from(number)?[panic]。
不引入 as[checked]/as[wrap] 截断糖。
比较链 a == b == c 等价于相邻比较全真，公共操作数只求值一次，并短路。

位运算用 bitand、bitor、bitxor、bitnot、bitshl、bitshr，不用 &/|/^/~ 作普通位运算。
bitnot 为前缀，其他为二元中缀。移位不复用所有权 << / >>。
左移结果须仍可由原整数位宽表示，否则常量报错或运行时 panic，不截高位。
无符号右移补零，有符号右移补符号位。
次数小于零或大于等于位宽报错/panic，不取模；次数可以是不同宽度整数。

## 12. 表达式优先级（高到低）

| 层级 | 运算 | 结合方式 |
| --- | --- | --- |
| 1 | 调用、泛型/索引附件、成员、后缀 @ / # / ?[return] / ?[panic] | 从左向右 |
| 2 | 前缀 not、bitnot、+、- | 从右向左 |
| 3 | as 类型转换 | 从左向右 |
| 4 | * / % | 从左向右 |
| 5 | + - | 从左向右 |
| 6 | bitshl bitshr | 从左向右 |
| 7 | bitand | 从左向右 |
| 8 | bitxor | 从左向右 |
| 9 | bitor | 从左向右 |
| 10 | == != < <= > >= | 比较链 |
| 11 | and | 短路、从左向右 |
| 12 | or | 短路、从左向右 |
| 13 | .. / ..= | 不连续结合 |
| 14 | \|> | 从左向右 |
| 15 | 普通 ? 分流 | 最低 |

= / << / >> 是语句，不是嵌套赋值表达式；
::、:、:>、1>/2>、-> 属于各自结构，不排进二元运算表。
[] 附件不是任意表达式运算；类型表达式使用自己的语法。

## 13. 模块、公开性和全局

文件即模块，不需要 mod/module 声明；main.xe 或 src/main.xe 为程序入口，
lib.xe 或 src/lib.xe 为库入口，同目录工具模块用文件名。路径用 crate/self/super 和 ::。
use crate::tools::foo; 导入，as 改名，支持分组 use，不使用通配符导入。
默认私有，声明前 pub 公开，如 pub fn、pub struct、pub type、pub let、pub use。
结构公开不自动公开字段/方法；字段 pub field: T，方法 pub fn ...。
私有项对定义模块及子模块可见；pub use 不能越权公开私有项。
同包模块可交叉引用，先收集再解析；本地包 path 依赖图不能循环。
最近 xe.toml 描述 package 和依赖；不承诺网络注册表、Git 依赖或 xe.lock。

模块 let NAME: T = constant_expression; / let[mut] NAME: T = constant_expression;
需明确类型、Copy 和静态可求值初值；不调用顶层函数/执行 IO，不拥有动态资源。
只读和可写全局均可取稳定地址；写权限仍由 let[mut]/@[mut] 控制。
可写全局不自动同步。入口 main 不带参数/泛型，返回 Unit 或 i32；参数通过库读取。

## 14. 标准库、线程和 C

核心有 print、println、readline，及 std::env::args、文件、容器、智能指针、线程模块。
打印不特殊借用；保留资源写 println("{}", text@);。
格式模板要求字面量，支持匿名 {}、指针 {:p} 和转义 {{/}}。
不声称任意类型自动 Display/Debug、完整 format 或任意 printf 格式已实现。
命令行参数包括 argv[0]，保留空字符串、空格、UTF-8，不手工 shell 拼接执行。
读取失败、EOF、迭代 Stop 分属明确状态，不能任意互换。

Box[T] 为独占堆资源；Shared[T] 显式 share，Weak[T] 显式弱引用，升级结果需要分流。
Mutex[T] 锁操作返回 Guard，正常退出按资源规则解锁。
Thread 与同步接口按实际库契约，不给普通指针附加自动线程安全保证。
不能从非 Copy 的智能指针目标拿走资源，释放/共享操作用明确库接口。

extern "C" { fn native_add(a: i32, b: i32) -> i32; } 声明有限 C FFI。
支持精确标量及其嵌套指针，Unit 返回桥接 C void；无捕获 fn 可作兼容回调，
当前 callback 返回不能为 Unit/Never。不承诺 Xe 聚合/C struct 布局、可变参数、
带捕获闭包、C 导出库或其他 ABI。原始 C 符号需合法且不冲突。
显式链接输入用 --link-input；不能把 str 当作 char*。

## 15. 诊断和未实现范围

错误面向语言使用者，含文件、行列、原因和可行修正；保持稳定机器 code/severity。
词法/解析、名称/类型、权限/所有权、后端、系统编译错误分层报告。
语义 error 阻止生成程序；warning 仍可编译，但不保证执行安全。
AST JSON 应含 kind 和半开 span；AST-only 不能偷偷做成全语义检查。
panic 位置指出 Xe 源码，不冒充完整调用栈或 C 崩溃捕获。
保留形式须明确拒绝，不以“后端尚不支持”掩盖错误类型程序通过。

本版不承诺动态 Trait、关联类型/父 Trait、重叠特化、常量泛型、泛型别名、
递归模式、守卫、独立泛型闭包、闭包动态共同类型、yield、async、完整格式化、
LSP、布局/volatile/原子语法、裸机、交叉编译、模块增量或全语言自举。
Map/Set/Iterator/Formatter 旧内建占位不是可用标准类型；用户可自定义同名普通类型。
全局资源/运行时初始化、统一切片失败策略、File 外部游标状态约定、
空指针/地址整数化/布局等未冻结事项，先提案，不由实现者自行决定。

## 16. 验收与发布

每个功能要有合法解析/AST、非法语法、合法语义、非法类型/所有权、
真实编译执行，以及必要的清理和 ASan/UBSan/default LSan 回归。
不能依赖空目录 glob 产生零样例假通过，不能禁用泄漏检查掩盖错误。
独立实现至少验证本文件完整例子及移后使用、只读指针写入、
从指针移动资源、非 Copy 的 =、不穷尽分支、错误移位等失败案例。

本仓库只使用根目录 uv 环境：

```sh
uv sync --locked --dev
make python-check
make compiler-test CC=gcc
make release-check CC=gcc
```

生成物放 target/，不提交缓存、二进制、AST JSON、密钥或环境。
分层：源文本/词法/解析 -> AST -> 模块/类型/资源检查 -> C 后端 -> 系统工具链。
构建失败不覆盖有效旧程序；清理只删登记产物，不删用户源码/链接输入。
正式源码候选包从干净提交用 --require-clean 打包，再从包外目录冒烟。
本地通过不表示远端 CI 通过，也不能证明所有程序完全无误。
提交、标签、push、上传各需用户授权；不 force push 或覆盖既有标签。
保留用户工作树修改，使用 apply_patch 局部编辑，先验证再声明完成。
改变含义要同步规范、例子、诊断、测试，不把历史快照当现行规范。
