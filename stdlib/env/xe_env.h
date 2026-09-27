/* stage0 标准库：进程命令行参数。XeStr 只描述操作系统提供的字符串，
 * 不拥有它；仅描述符表由本库分配，并在 Xe main 的资源清理之后释放。
 * 此文件在 Xe 基础运行库之后包含，也会被嵌入生成的独立 C 文件。
 */
#ifndef XE_STDLIB_ENV_H
#define XE_STDLIB_ENV_H

typedef struct { XeStr *data; size_t len; int error; } XeEnvArgs;

static int xe_env_argc;
static char **xe_env_argv;
static bool xe_env_args_loaded;
static XeEnvArgs xe_env_args_cache;

static void xe_env_init(int argc, char **argv) {
    /* 不在启动时验证 UTF-8：不调用 args 的程序不应被无关参数拒绝。 */
    xe_env_argc = argc;
    xe_env_argv = argv;
}

static XeEnvArgs xe_env_args(void) {
    /* 成功和失败都缓存，保证重复调用不会重复分配，也不会改变结果。
     * 操作系统已经拆分参数：空参数、空格、引号都是参数内容，不再解析。
     */
    if (xe_env_args_loaded) return xe_env_args_cache;
    xe_env_args_loaded = true;
    if (xe_env_argc < 0 || (xe_env_argc && !xe_env_argv)) {
        xe_env_args_cache.error = EINVAL;
        return xe_env_args_cache;
    }
    size_t count = (size_t)xe_env_argc;
    if (!count) return xe_env_args_cache;
    if (count > SIZE_MAX / sizeof(XeStr)) {
        xe_env_args_cache.error = EOVERFLOW;
        return xe_env_args_cache;
    }
    /* 不能使用会 panic 的 xe_alloc：分配失败是 args 的 No[io::Error]。 */
    XeStr *items = malloc(count * sizeof *items);
    if (!items) {
        xe_env_args_cache.error = ENOMEM;
        return xe_env_args_cache;
    }
    for (size_t index = 0; index < count; ++index) {
        if (!xe_env_argv[index]) {
            free(items);
            xe_env_args_cache.error = EINVAL;
            return xe_env_args_cache;
        }
        items[index] = (XeStr){(const unsigned char *)xe_env_argv[index],
                              strlen(xe_env_argv[index])};
        if (!xe_utf8_valid(items[index])) {
            free(items);
            xe_env_args_cache.error = EILSEQ;
            return xe_env_args_cache;
        }
    }
    xe_env_args_cache = (XeEnvArgs){items, count, 0};
    return xe_env_args_cache;
}

static void xe_env_cleanup(void) {
    /* 严格只释放自己创建的表，argv 指向的原字符串属于操作系统。
     * XeStr/Slice 都是非拥有视图，不应为每个调用生成资源析构。
     */
    free(xe_env_args_cache.data);
    xe_env_args_cache = (XeEnvArgs){0};
    xe_env_args_loaded = false;
    xe_env_argc = 0;
    xe_env_argv = NULL;
}
#endif
