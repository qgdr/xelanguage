/* 此文件代表已有 C 库。符号名与 Xe extern 声明逐字一致。
 * 定宽标量/普通指针使用明确 ABI，void 由编译器桥接为 Xe Unit。 */
#include <stdint.h>

int32_t demo_sum(int32_t left, int32_t right) {
    return left + right;
}

void demo_increment(int32_t *value) {
    *value += 1;
}
