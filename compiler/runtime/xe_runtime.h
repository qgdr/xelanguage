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

typedef uint8_t XeUnit;
typedef struct { const unsigned char *data; size_t len; } XeStr;
typedef struct { unsigned char *data; size_t len; size_t cap; } XeString;

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
    /* Allocate before freeing: even unchecked self-views remain valid during copying. */
    unsigned char *data = xe_alloc(needed);
    memcpy(data, value->data, value->len);
    memcpy(data + value->len, suffix.data, suffix.len);
    data[needed - 1] = 0;
    free(value->data);
    *value = (XeString){data, needed - 1, needed};
}
static int xe_str_compare(XeStr a, XeStr b) {
    size_t size = a.len < b.len ? a.len : b.len;
    int result = memcmp(a.data, b.data, size);
    if (result) return result;
    return (a.len > b.len) - (a.len < b.len);
}
static void xe_write(FILE *stream, XeStr value) {
    if (value.len && fwrite(value.data, 1, value.len, stream) != value.len)
        xe_panic("output failed");
}
static void xe_print_i64(FILE *stream, int64_t value) { fprintf(stream, "%" PRId64, value); }
static void xe_print_u64(FILE *stream, uint64_t value) { fprintf(stream, "%" PRIu64, value); }
static void xe_print_float(FILE *stream, double value) { fprintf(stream, "%.17g", value); }
static void xe_print_bool(FILE *stream, bool value) { fputs(value ? "true" : "false", stream); }
static void xe_print_pointer(FILE *stream, const void *value) { fprintf(stream, "%p", value); }
static void xe_print_char(FILE *stream, uint32_t value) {
    unsigned char bytes[4]; size_t length;
    if (value <= 0x7f) { bytes[0] = value; length = 1; }
    else if (value <= 0x7ff) {
        bytes[0] = 0xc0 | (value >> 6); bytes[1] = 0x80 | (value & 0x3f); length = 2;
    } else if (value <= 0xffff && !(value >= 0xd800 && value <= 0xdfff)) {
        bytes[0] = 0xe0 | (value >> 12); bytes[1] = 0x80 | ((value >> 6) & 0x3f);
        bytes[2] = 0x80 | (value & 0x3f); length = 3;
    } else if (value <= 0x10ffff && !(value >= 0xd800 && value <= 0xdfff)) {
        bytes[0] = 0xf0 | (value >> 18); bytes[1] = 0x80 | ((value >> 12) & 0x3f);
        bytes[2] = 0x80 | ((value >> 6) & 0x3f); bytes[3] = 0x80 | (value & 0x3f); length = 4;
    } else { xe_panic("invalid Unicode scalar"); return; }
    xe_write(stream, (XeStr){bytes, length});
}

/* GCC/Clang checked arithmetic builtins avoid C signed-overflow undefined behavior. */
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
