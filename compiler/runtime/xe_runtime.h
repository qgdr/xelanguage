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

static void xe_panic(const char *message) {
    fprintf(stderr, "Xe runtime error: %s\n", message);
    exit(1);
}
static void *xe_alloc(size_t bytes) {
    void *p = malloc(bytes ? bytes : 1);
    if (!p) xe_panic("out of memory");
    return p;
}
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
    /* 先分配并复制，再释放旧缓冲区，避免复制时失去来源数据。
     * 这只是运行库的实现保证，不关闭 Xe 对冲突借用的检查。 */
    unsigned char *data = xe_alloc(needed);
    memcpy(data, value->data, value->len);
    memcpy(data + value->len, suffix.data, suffix.len);
    data[needed - 1] = 0;
    free(value->data);
    *value = (XeString){data, needed - 1, needed};
}
static XeFile xe_file_open(XeStr path, int *error) {
    /* Xe 路径带长度，libc 路径靠 NUL 结束；禁止内嵌 NUL 被默默截断。 */
    if (memchr(path.data, 0, path.len)) { *error = EINVAL; return (XeFile){0}; }
    XeString terminated = xe_string_from(path);
    FILE *handle = fopen((const char *)terminated.data, "rb");
    *error = handle ? 0 : errno;
    xe_string_drop(&terminated);
    return (XeFile){handle};
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
