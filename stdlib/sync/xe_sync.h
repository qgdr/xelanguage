/* POSIX stage0 并发运行库。这里只管理控制块和操作系统对象；
 * T 的具体析构由编译器生成的回调负责。计数原子化不保护用户数据读写。
 * 不复制 pthread_mutex_t，不把锁放进会被移动的 Xe 描述符内部。
 */
#ifndef XE_SYNC_H
#define XE_SYNC_H
#include <pthread.h>
#include <stdatomic.h>

typedef void (*XeValueDrop)(void *);
typedef struct {
    atomic_size_t strong, weak;
    void *data;
    XeValueDrop drop;
} XeShared;
typedef struct {
    atomic_size_t refs;
    pthread_mutex_t lock;
    void *data;
    XeValueDrop drop;
} XeMutex;
typedef struct { XeMutex *state; pthread_t owner; } XeMutexGuard;
typedef struct {
    pthread_t id;
    void *job;
    XeValueDrop discard;
} XeThread;

static void *xe_sync_alloc(size_t size) { return malloc(size); }

/* 有效句柄保持计数非零。检查溢出，不能环绕成零导致提前析构。 */
static void xe_ref_add(atomic_size_t *count) {
    size_t old = atomic_load_explicit(count, memory_order_relaxed);
    for (;;) {
        if (old == SIZE_MAX) xe_panic("reference count overflow");
        if (atomic_compare_exchange_weak_explicit(count, &old, old + 1,
                memory_order_relaxed, memory_order_relaxed)) return;
    }
}
static bool xe_ref_last(atomic_size_t *count) {
    return atomic_fetch_sub_explicit(count, 1, memory_order_acq_rel) == 1;
}
static XeShared *xe_shared_new(void *data, XeValueDrop drop) {
    XeShared *state = xe_sync_alloc(sizeof *state);
    if (!state) return NULL;
    atomic_init(&state->strong, 1);
    /* strong 非零期间有一个隐含 weak，保护析构过程中控制块的生命。 */
    atomic_init(&state->weak, 1);
    state->data = data; state->drop = drop;
    return state;
}
static void xe_weak_release(XeShared *state) {
    if (state && xe_ref_last(&state->weak)) free(state);
}
static void xe_shared_release(XeShared *state) {
    if (state && xe_ref_last(&state->strong)) {
        state->drop(state->data);
        xe_weak_release(state);
    }
}
static bool xe_weak_upgrade(XeShared *state) {
    size_t old = atomic_load_explicit(&state->strong, memory_order_relaxed);
    while (old) {
        if (old == SIZE_MAX) xe_panic("reference count overflow");
        if (atomic_compare_exchange_weak_explicit(&state->strong, &old, old + 1,
                memory_order_acquire, memory_order_relaxed)) return true;
    }
    /* 不允许 0 -> 1：值已经析构，但弱句柄仍使控制块存活。 */
    return false;
}
static XeMutex *xe_mutex_new(void *data, XeValueDrop drop, int *error) {
    XeMutex *state = xe_sync_alloc(sizeof *state);
    if (!state) { *error = ENOMEM; return NULL; }
    *error = pthread_mutex_init(&state->lock, NULL);
    if (*error) { free(state); return NULL; }
    atomic_init(&state->refs, 1);
    state->data = data; state->drop = drop;
    return state;
}
static void xe_mutex_release(XeMutex *state) {
    if (state && xe_ref_last(&state->refs)) {
        /* 每个持锁/等锁操作都拥有一个 ref；最后释放时不可能还在用锁。 */
        int error = pthread_mutex_destroy(&state->lock);
        if (error) xe_panic("cannot destroy mutex");
        state->drop(state->data); free(state);
    }
}
static int xe_mutex_lock(XeMutex *state) {
    xe_ref_add(&state->refs);
    int error = pthread_mutex_lock(&state->lock);
    if (error) xe_mutex_release(state);
    return error;
}
static void xe_guard_release(XeMutexGuard *guard) {
    if (!guard->state) return;
    if (!pthread_equal(guard->owner, pthread_self()))
        xe_panic("MutexGuard must be released by its locking thread");
    XeMutex *state = guard->state;
    int error = pthread_mutex_unlock(&state->lock);
    if (error) xe_panic("cannot unlock mutex");
    guard->state = NULL;
    xe_mutex_release(state);
}
/* 单独封装创建，便于测试注入操作系统失败，不污染 Xe 的公开语法。 */
static int xe_thread_create(pthread_t *id, void *(*run)(void *), void *job) {
    return pthread_create(id, NULL, run, job);
}
static XeThread *xe_thread_new(void *job, void *(*run)(void *), XeValueDrop discard, int *error) {
    XeThread *state = xe_sync_alloc(sizeof *state);
    if (!state) { *error = ENOMEM; return NULL; }
    state->job = job; state->discard = discard;
    *error = xe_thread_create(&state->id, run, job);
    if (*error) { free(state); return NULL; }
    return state;
}
static void *xe_thread_join(XeThread *state) {
    /* 消耗拥有句柄后必须先等待再释放环境/结果。合法句柄只 join 一次。
     * 自己等待自己或外部破坏句柄不可安全回收，明确终止而不是 free
     * 仍在运行的线程环境；不暗中 detach 一个用户期望已经结束的线程。 */
    int error = pthread_join(state->id, NULL);
    if (error) xe_panic("cannot join owned thread");
    void *job = state->job;
    free(state);
    return job;
}
static void xe_thread_release(XeThread *state) {
    if (!state) return;
    XeValueDrop discard = state->discard;
    void *job = xe_thread_join(state);
    discard(job);
}
#endif
