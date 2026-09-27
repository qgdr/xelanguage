/* Xe std::io 的 libc 实现。
 * XeStr / XeString 和字符串释放由 compiler/runtime/xe_runtime.h 提供。
 * 输出遵守既有 panic 合同；readline 用错误值报告失败，不终止进程。
 */
#ifndef XE_STDLIB_IO_H
#define XE_STDLIB_IO_H

/* POSIX 的标准描述符有固定入口，不调用可能被 C11 隐藏声明的 fileno。
 * Windows 使用 CRT 提供的 _fileno/_isatty；未知平台保守地返回 false。
 */
#if defined(_WIN32)
#include <io.h>
#elif defined(__unix__) || defined(__APPLE__)
#include <unistd.h>
#endif

static bool xe_io_stdin_is_terminal(void) {
#if defined(_WIN32)
    return _isatty(_fileno(stdin)) != 0;
#elif defined(__unix__) || defined(__APPLE__)
    return isatty(STDIN_FILENO) != 0;
#else
    return false;
#endif
}
static bool xe_io_stdout_is_terminal(void) {
#if defined(_WIN32)
    return _isatty(_fileno(stdout)) != 0;
#elif defined(__unix__) || defined(__APPLE__)
    return isatty(STDOUT_FILENO) != 0;
#else
    return false;
#endif
}
static bool xe_io_stderr_is_terminal(void) {
#if defined(_WIN32)
    return _isatty(_fileno(stderr)) != 0;
#elif defined(__unix__) || defined(__APPLE__)
    return isatty(STDERR_FILENO) != 0;
#else
    return false;
#endif
}

static bool xe_io_terminal_supports_color(bool terminal) {
    /* 这是是否建议输出 ANSI 颜色的策略，不是所有终端能力的完整探测。
     * NO_COLOR 空值不禁用；非空值禁用。TERM=dumb 即使是 TTY 也禁用。
     * Windows 暂不配置控制台 ANSI 模式，因此不猜测它支持颜色。
     */
#if defined(_WIN32)
    (void)terminal;
    return false;
#else
    if (!terminal) return false;
    const char *no_color = getenv("NO_COLOR");
    if (no_color && *no_color) return false;
    const char *term = getenv("TERM");
    return !term || strcmp(term, "dumb") != 0;
#endif
}
static bool xe_io_stdout_supports_color(void) {
    return xe_io_terminal_supports_color(xe_io_stdout_is_terminal());
}
static bool xe_io_stderr_supports_color(void) {
    return xe_io_terminal_supports_color(xe_io_stderr_is_terminal());
}

static void xe_io_write(FILE *stream, XeStr value) {
    if (value.len && fwrite(value.data, 1, value.len, stream) != value.len)
        xe_panic("output failed");
}
static void xe_io_print_i64(FILE *stream, int64_t value) {
    if (fprintf(stream, "%" PRId64, value) < 0) xe_panic("output failed");
}
static void xe_io_print_u64(FILE *stream, uint64_t value) {
    if (fprintf(stream, "%" PRIu64, value) < 0) xe_panic("output failed");
}
static void xe_io_print_float(FILE *stream, double value) {
    if (fprintf(stream, "%.17g", value) < 0) xe_panic("output failed");
}
static void xe_io_print_bool(FILE *stream, bool value) {
    if (fputs(value ? "true" : "false", stream) == EOF) xe_panic("output failed");
}
static void xe_io_print_pointer(FILE *stream, const void *value) {
    if (fprintf(stream, "%p", value) < 0) xe_panic("output failed");
}
static void xe_io_print_error(FILE *stream, int error) {
    if (fputs(strerror(error), stream) == EOF) xe_panic("output failed");
}
static void xe_io_newline(FILE *stream) {
    if (fputc('\n', stream) == EOF) xe_panic("output failed");
}
/* stdio 可能只接收缓冲数据，直到 fflush 才发现目标不可写。
 * 生成的入口在 Xe main 返回并清理资源后刷新两个输出流，不能把输出失败
 * 当作成功退出。这里只保证 libc 接收输出，不承诺磁盘断电后数据仍持久。
 * readline 自己检查 fflush 并返回错误值，不替调用者选择此处的 panic。
 */
static void xe_io_flush(FILE *stream) {
    if (fflush(stream) == EOF) xe_panic("output failed");
}
static void xe_io_print_char(FILE *stream, uint32_t value) {
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
    xe_io_write(stream, (XeStr){bytes, length});
}

/* has_line 区分空行与 EOF；error 区分读取错误与正常 EOF。
 * 只有 has_line && !error 时 text 拥有分配，调用方必须恰好释放一次。
 */
typedef struct { XeString text; bool has_line; int error; } XeIoReadline;

static XeIoReadline xe_io_readline(FILE *stream) {
    XeIoReadline result = {0};
    /* 输入之前刷新提示。刷新失败同样返回 I/O 错误，不继续消耗输入。 */
    errno = 0;
    if (fflush(stdout) == EOF) {
        result.error = errno ? errno : EIO;
        return result;
    }
    bool ended_with_lf = false;
    while (true) {
        errno = 0;
        int byte = fgetc(stream);
        if (byte == EOF) {
            if (ferror(stream)) result.error = errno ? errno : EIO;
            break;
        }
        result.has_line = true;
        if (byte == '\n') { ended_with_lf = true; break; }
        /* 保留一个 NUL 结束字节，但业务长度仍包含输入中的 NUL。 */
        if (result.text.len >= SIZE_MAX - 1) { result.error = EOVERFLOW; break; }
        size_t needed = result.text.len + 2;
        if (needed > result.text.cap) {
            size_t capacity = result.text.cap ?
                (result.text.cap <= SIZE_MAX / 2 ? result.text.cap * 2 : SIZE_MAX) : 128;
            if (capacity < needed) capacity = needed;
            void *data = realloc(result.text.data, capacity);
            if (!data) { result.error = ENOMEM; break; }
            result.text.data = data;
            result.text.cap = capacity;
        }
        result.text.data[result.text.len++] = (unsigned char)byte;
    }
    if (result.error) {
        xe_string_drop(&result.text);
        result.has_line = false;
        return result;
    }
    if (!result.has_line) return result;
    if (ended_with_lf && result.text.len && result.text.data[result.text.len - 1] == '\r')
        --result.text.len;
    /* 真正的空行也返回一个拥有 String，而非用 EOF 的零值冒充。 */
    if (!result.text.data) {
        result.text.data = malloc(1);
        if (!result.text.data) { result.error = ENOMEM; result.has_line = false; return result; }
        result.text.cap = 1;
    }
    result.text.data[result.text.len] = 0;
    if (!xe_utf8_valid(xe_string_view(&result.text))) {
        xe_string_drop(&result.text);
        result.has_line = false;
        result.error = EILSEQ;
    }
    return result;
}
#endif
