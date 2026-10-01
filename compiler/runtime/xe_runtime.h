/* Xe stage0 的小运行库。只使用 libc；资源清理由生成的 C 显式调用。
 * str 是地址+字节长度视图，String 是拥有分配的资源，二者不能混用释放。
 * panic 终止进程，不承诺栈展开。初版数值运算检查整数溢出和除零。
 */
#ifndef XE_RUNTIME_H
#define XE_RUNTIME_H
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include <limits.h>
#include <errno.h>

typedef uint8_t XeUnit;
typedef struct { const unsigned char *data; size_t len; } XeStr;
typedef struct { unsigned char *data; size_t len; size_t cap; } XeString;
typedef struct { FILE *handle; } XeFile;
/* 字节/字符迭代器只保存视图和游标，不拥有源码缓冲区。 */
typedef struct { XeStr text; size_t position; } XeTextIter;

static void xe_panic(const char *message) {
    fprintf(stderr, "Xe runtime error: %s\n", message);
    exit(1);
}
static void *xe_alloc(size_t bytes) {
    void *p = malloc(bytes ? bytes : 1);
    if (!p) xe_panic("out of memory");
    return p;
}
/* Box 的分配失败是可处理结果，不能复用会终止进程的 xe_alloc。
 * 所有 Xe 具体类型的 C 布局至少占一个字节；失败时调用方负责清理实参。 */
static void *xe_box_alloc(size_t size) { return malloc(size); }
static XeString xe_string_from(XeStr view) {
    if (view.len == SIZE_MAX) xe_panic("string too large");
    XeString result = {xe_alloc(view.len + 1), view.len, view.len + 1};
    memcpy(result.data, view.data, view.len);
    result.data[view.len] = 0;
    return result;
}
static XeStr xe_string_view(const XeString *value) {
    return (XeStr){value->data, value->len};
}
static void xe_string_drop(XeString *value) {
    free(value->data);
    *value = (XeString){0};
}
static void xe_string_push(XeString *value, XeStr suffix) {
    if (suffix.len > SIZE_MAX - value->len - 1) xe_panic("string too large");
    size_t needed = value->len + suffix.len + 1;
    if (needed <= value->cap) {
        memmove(value->data + value->len, suffix.data, suffix.len);
        value->len += suffix.len;
        value->data[value->len] = 0;
        return;
    }
    /* 容量成倍增长；先复制再释放，允许追加自己的子视图。 */
    size_t capacity = value->cap <= SIZE_MAX / 2 ? value->cap * 2 : SIZE_MAX;
    if (capacity < needed) capacity = needed;
    unsigned char *data = xe_alloc(capacity);
    memcpy(data, value->data, value->len);
    memcpy(data + value->len, suffix.data, suffix.len);
    data[needed - 1] = 0;
    free(value->data);
    *value = (XeString){data, needed - 1, capacity};
}
static void *xe_vec_reserve(void *data, size_t size, size_t len,
                            size_t *capacity, size_t additional) {
    if (additional > SIZE_MAX - len || len + additional > SIZE_MAX / size)
        xe_panic("vector too large");
    size_t needed = len + additional;
    if (needed <= *capacity) return data;
    size_t grown = *capacity <= SIZE_MAX / 2 ? *capacity * 2 : SIZE_MAX;
    if (grown < 4) grown = 4;
    if (grown < needed) grown = needed;
    if (grown > SIZE_MAX / size) grown = needed;
    void *next = realloc(data, grown * size);
    if (!next) xe_panic("out of memory");
    *capacity = grown;
    return next;
}
static void xe_string_push_char(XeString *value, uint32_t scalar) {
    unsigned char bytes[4]; size_t count;
    if (scalar <= 0x7f) { bytes[0] = (unsigned char)scalar; count = 1; }
    else if (scalar <= 0x7ff) {
        bytes[0] = (unsigned char)(0xc0 | (scalar >> 6));
        bytes[1] = (unsigned char)(0x80 | (scalar & 63)); count = 2;
    } else if (scalar <= 0xffff && !(scalar >= 0xd800 && scalar <= 0xdfff)) {
        bytes[0] = (unsigned char)(0xe0 | (scalar >> 12));
        bytes[1] = (unsigned char)(0x80 | ((scalar >> 6) & 63));
        bytes[2] = (unsigned char)(0x80 | (scalar & 63)); count = 3;
    } else if (scalar >= 0x10000 && scalar <= 0x10ffff) {
        bytes[0] = (unsigned char)(0xf0 | (scalar >> 18));
        bytes[1] = (unsigned char)(0x80 | ((scalar >> 12) & 63));
        bytes[2] = (unsigned char)(0x80 | ((scalar >> 6) & 63));
        bytes[3] = (unsigned char)(0x80 | (scalar & 63)); count = 4;
    } else { xe_panic("invalid Unicode scalar"); return; }
    xe_string_push(value, (XeStr){bytes, count});
}
static XeFile xe_file_open_mode(XeStr path, const char *mode, int *error) {
    /* Xe 路径带长度，libc 路径靠 NUL 结束；禁止内嵌 NUL 被默默截断。 */
    if (memchr(path.data, 0, path.len)) { *error = EINVAL; return (XeFile){0}; }
    XeString terminated = xe_string_from(path);
    FILE *handle = fopen((const char *)terminated.data, mode);
    *error = handle ? 0 : errno;
    xe_string_drop(&terminated);
    return (XeFile){handle};
}
static XeFile xe_file_open(XeStr path, int *error) {
    return xe_file_open_mode(path, "rb", error);
}
static XeFile xe_file_create(XeStr path, int *error) {
    return xe_file_open_mode(path, "wb", error);
}
static int xe_file_write(XeFile *file, XeStr text) {
    errno = 0;
    if (text.len && fwrite(text.data, 1, text.len, file->handle) != text.len)
        return errno ? errno : EIO;
    return 0;
}
static int xe_file_flush(XeFile *file) {
    errno = 0;
    return fflush(file->handle) ? (errno ? errno : EIO) : 0;
}
static uint32_t xe_text_next(XeTextIter *it, bool characters) {
    if (it->position >= it->text.len) xe_panic("iterator exhausted");
    unsigned char first = it->text.data[it->position++];
    if (!characters || first < 0x80) return first;
    unsigned count; uint32_t scalar, minimum;
    if (first >= 0xc2 && first <= 0xdf) { count = 1; scalar = first & 31; minimum = 0x80; }
    else if (first >= 0xe0 && first <= 0xef) { count = 2; scalar = first & 15; minimum = 0x800; }
    else if (first >= 0xf0 && first <= 0xf4) { count = 3; scalar = first & 7; minimum = 0x10000; }
    else { xe_panic("invalid UTF-8 view"); return 0; }
    /* 正常 str 构造保证 UTF-8；仍诊断被 unsafe 操作破坏的文字内容。
     * 这不验证地址是否有效，不能把内容校验说成内存安全保证。 */
    if (count > it->text.len - it->position) xe_panic("invalid UTF-8 view");
    while (count--) {
        unsigned char next = it->text.data[it->position++];
        if ((next & 0xc0) != 0x80) xe_panic("invalid UTF-8 view");
        scalar = (scalar << 6) | (next & 63);
    }
    if (scalar < minimum || scalar > 0x10ffff || (scalar >= 0xd800 && scalar <= 0xdfff))
        xe_panic("invalid UTF-8 view");
    return scalar;
}
static void xe_file_drop(XeFile *file) {
    if (file->handle) fclose(file->handle);
    file->handle = NULL;
}
static size_t xe_file_size(const XeFile *file) {
    long position = ftell(file->handle);
    if (position < 0 || fseek(file->handle, 0, SEEK_END)) xe_panic("cannot determine file size");
    long end = ftell(file->handle);
    if (fseek(file->handle, position, SEEK_SET) || end < 0) xe_panic("cannot determine file size");
    return (size_t)end;
}
static bool xe_utf8_valid(XeStr text) {
    /* 首字节决定后续字节数；随后重建 Unicode 标量。
     * minimum 排除过长编码，还必须拒绝代理区和超出 Unicode 范围的值。 */
    for (size_t i = 0; i < text.len;) {
        unsigned char first = text.data[i++];
        if (first < 0x80) continue;
        unsigned remaining;
        uint32_t scalar, minimum;
        if (first >= 0xc2 && first <= 0xdf) { remaining = 1; scalar = first & 0x1f; minimum = 0x80; }
        else if (first >= 0xe0 && first <= 0xef) { remaining = 2; scalar = first & 0x0f; minimum = 0x800; }
        else if (first >= 0xf0 && first <= 0xf4) { remaining = 3; scalar = first & 0x07; minimum = 0x10000; }
        else return false;
        if (remaining > text.len - i) return false;
        while (remaining--) {
            unsigned char next = text.data[i++];
            if ((next & 0xc0) != 0x80) return false;
            scalar = (scalar << 6) | (next & 0x3f);
        }
        if (scalar < minimum || scalar > 0x10ffff || (scalar >= 0xd800 && scalar <= 0xdfff)) return false;
    }
    return true;
}
static XeString xe_file_read(const XeFile *file, int *error) {
    /* 缓冲区容量成倍增长，避免每读一块就复制全部旧内容。
     * 失败时回收已分配内存；成功时由返回 String 的接收者负责回收。 */
    XeString result = xe_string_from((XeStr){(const unsigned char *)"", 0});
    unsigned char chunk[4096];
    size_t count;
    while ((count = fread(chunk, 1, sizeof chunk, file->handle)) != 0) {
        if (count > SIZE_MAX - result.len - 1) xe_panic("file too large");
        size_t needed = result.len + count + 1;
        if (needed > result.cap) {
            size_t capacity = result.cap <= SIZE_MAX / 2 ? result.cap * 2 : SIZE_MAX;
            if (capacity < needed) capacity = needed;
            void *data = realloc(result.data, capacity);
            if (!data) xe_panic("out of memory");
            result.data = data; result.cap = capacity;
        }
        memcpy(result.data + result.len, chunk, count);
        result.len += count;
        result.data[result.len] = 0;
    }
    *error = ferror(file->handle) ? (errno ? errno : EIO) : 0;
    if (!*error && !xe_utf8_valid(xe_string_view(&result))) *error = EILSEQ;
    if (*error) xe_string_drop(&result);
    return result;
}
static int xe_str_compare(XeStr a, XeStr b) {
    size_t size = a.len < b.len ? a.len : b.len;
    int result = memcmp(a.data, b.data, size);
    if (result) return result;
    return (a.len > b.len) - (a.len < b.len);
}
static bool xe_str_slice_valid(XeStr value, size_t start, size_t end) {
    if (start > end || end > value.len) return false;
    /* str 已保证合法 UTF-8；只能在首字节之前或字符串末尾切开。 */
    return (start == value.len || (value.data[start] & 0xc0) != 0x80)
        && (end == value.len || (value.data[end] & 0xc0) != 0x80);
}
#include "../../stdlib/io/xe_io.h"
#include "../../stdlib/env/xe_env.h"

/* 使用 GCC/Clang 的溢出检查，不直接继承 C 的有符号溢出未定义行为。
 * 宏只消除不同整数宽度之间的重复，不改变每种类型的运算规则。 */
#define XE_INTEGER(NAME, TYPE, MINIMUM) \
static TYPE xe_add_##NAME(TYPE a, TYPE b) { TYPE r; if (__builtin_add_overflow(a,b,&r)) xe_panic("integer overflow"); return r; } \
static TYPE xe_sub_##NAME(TYPE a, TYPE b) { TYPE r; if (__builtin_sub_overflow(a,b,&r)) xe_panic("integer overflow"); return r; } \
static TYPE xe_mul_##NAME(TYPE a, TYPE b) { TYPE r; if (__builtin_mul_overflow(a,b,&r)) xe_panic("integer overflow"); return r; } \
static TYPE xe_div_##NAME(TYPE a, TYPE b) { if (!b) xe_panic("division by zero"); if ((MINIMUM) < 0 && a == (MINIMUM) && b == (TYPE)-1) xe_panic("integer overflow"); return a/b; } \
static TYPE xe_rem_##NAME(TYPE a, TYPE b) { if (!b) xe_panic("division by zero"); if ((MINIMUM) < 0 && a == (MINIMUM) && b == (TYPE)-1) return 0; return a%b; }
XE_INTEGER(i8, int8_t, INT8_MIN)
XE_INTEGER(i16, int16_t, INT16_MIN)
XE_INTEGER(i32, int32_t, INT32_MIN)
XE_INTEGER(i64, int64_t, INT64_MIN)
XE_INTEGER(u8, uint8_t, 0)
XE_INTEGER(u16, uint16_t, 0)
XE_INTEGER(u32, uint32_t, 0)
XE_INTEGER(u64, uint64_t, 0)
XE_INTEGER(isize, intptr_t, INTPTR_MIN)
XE_INTEGER(usize, size_t, 0)
#undef XE_INTEGER
#endif
