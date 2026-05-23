#!/usr/bin/env python3
"""HardPin 策略人工验证脚本。

验证设计文档（hard_pin_design.md）中的三大核心保证：
  1. 驱逐保护（4.1）：无 LOCAL_DISK 副本的 MEMORY 不被驱逐
  2. SSD 水位控制（4.2）：effective_free_ratio < watermark → 拒绝分配
  3. SSD 不可驱逐：LOCAL_DISK 副本在任何情况下不被驱逐

用法：
    python verify_hard_pin.py --test <test_name>

test_name:
    ssd_full_reject       - 验证保证2：SSD 水位拒绝（不 fallback）
    eviction_protection   - 验证保证1：驱逐保护
    ssd_eviction_rejected - 验证保证3：SSD 副本不可驱逐
    full_lifecycle        - 端到端：写入→offload→驱逐→读取

关键环境变量：
    MC_METADATA_SERVER                        - Master 元数据地址
    DEFAULT_KV_LEASE_TTL                      - Lease TTL ms
    MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES   - SSD 容量上限（字节）
    MOONCAKE_OFFLOAD_FILE_STORAGE_PATH        - SSD 数据存储目录

注意：
    store.put() 返回整数状态码：0=成功, 非0=失败（不抛异常）
    store.get() 返回 bytes：空 bytes b"" 表示失败
"""

import argparse
import os
import sys
import time
import traceback

from mooncake.store import MooncakeDistributedStore

# 默认端口配置
DEFAULT_MASTER_PORT = "50053"
DEFAULT_METADATA_PORT = "8880"
DEFAULT_METRICS_PORT = "9104"


def create_store(segment_size=64 * 1024 * 1024,
                 buffer_size=64 * 1024 * 1024):
    """创建并初始化一个 Store 客户端。

    SSD 容量通过 MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES 环境变量控制，
    Client 初始化时会通过 ReportSsdCapacity RPC 上报给 Master。
    """
    ssd_limit_str = os.getenv("MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES", "")
    if not ssd_limit_str:
        print("[ERROR] MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES 未设置！")
        print("  HardPin 需要 SSD 容量限制来计算水位。")
        print("  例如：MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=134217728 (128MB)")
        sys.exit(1)

    ssd_limit = int(ssd_limit_str)
    effective = ssd_limit - segment_size
    print(f"  SSD 限制: {ssd_limit / 1024 / 1024:.0f}MB, "
          f"DDR: {segment_size / 1024 / 1024:.0f}MB, "
          f"effective_capacity: {effective / 1024 / 1024:.1f}MB")

    if effective <= 0:
        print(f"[ERROR] effective_capacity <= 0！SSD({ssd_limit}) 必须 > DDR({segment_size})")
        sys.exit(1)

    store = MooncakeDistributedStore()
    protocol = os.getenv("PROTOCOL", "tcp")
    device_name = os.getenv("DEVICE_NAME", "eth0")
    local_hostname = os.getenv("LOCAL_HOSTNAME", "127.0.0.1")
    metadata_server = os.getenv("MC_METADATA_SERVER",
                                f"http://127.0.0.1:{DEFAULT_METADATA_PORT}/metadata")
    master_server = os.getenv("MASTER_SERVER",
                              f"127.0.0.1:{DEFAULT_MASTER_PORT}")

    ssd_path = os.getenv("MOONCAKE_OFFLOAD_FILE_STORAGE_PATH",
                         "/tmp/mooncake_ssd_test")

    retcode = store.setup(
        local_hostname,
        metadata_server,
        segment_size,
        buffer_size,
        protocol,
        device_name,
        master_server,
        enable_ssd_offload=True,
        ssd_offload_path=ssd_path,
    )
    if retcode:
        raise RuntimeError(f"Store setup 失败: retcode={retcode}")
    return store


def test_ssd_full_reject():
    """验证设计文档 4.2 节：SSD 水位控制。

    核心验证点：
      - effective_free_ratio < watermark 时拒绝 PutStart
      - 返回 NO_AVAILABLE_HANDLE，不 fallback 写入
      - 拒绝原因是 SSD 水位，不是 DDR 满

    分批写入策略（避免 DDR 满 → 驱逐风暴）：
      DDR = 64MB, 每批写 4 个 4MB 对象 = 16MB (DDR 占 25%)
      每批后等 5 秒让 offload 排空 DDR
      → DDR 始终远低于 95% 驱逐线
      → 任何拒绝都是 SSD 水位触发，不是 DDR 满

    水位计算：
      SSD = 128MB, DDR = 64MB → effective_capacity = 64MB
      watermark = 15% → effective_free < 9.6MB 时拒绝
      即 used > 54.4MB 时拒绝 → 约 14 个 4MB 对象

    环境变量要求：
      MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=134217728  (128MB)
    """
    print("=== 验证保证2：SSD 水位控制（effective_free < watermark → 拒绝）===")
    print()

    ssd_limit = int(os.getenv("MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES", "0"))
    seg_size = 64 * 1024 * 1024  # 64MB DDR
    effective = ssd_limit - seg_size  # 64MB effective
    watermark = 0.15
    watermark_bytes = effective * watermark  # 9.6MB
    trigger_at = effective - watermark_bytes  # 54.4MB

    print(f"  参数：SSD={ssd_limit/1024/1024:.0f}MB, DDR={seg_size/1024/1024:.0f}MB")
    print(f"  effective_capacity = {effective/1024/1024:.0f}MB")
    print(f"  watermark = {watermark*100:.0f}% → 拒绝阈值 ~{trigger_at/1024/1024:.0f}MB")
    print(f"  策略：每批 4 个 4MB 对象，批间等 5 秒让 offload 排空 DDR")
    print()

    store = create_store(segment_size=seg_size, buffer_size=seg_size)

    object_size = 4 * 1024 * 1024  # 4MB
    batch_size = 4
    batch_sleep = 5  # 每批后等 5 秒

    written = 0
    rejected = 0
    batch_num = 0

    for _ in range(100):
        batch_num += 1
        batch_written = 0

        for i in range(batch_size):
            key = f"ssd_full_key_{written}"
            data = b"\x00" * object_size
            retcode = store.put(key, data)

            if retcode == 0:
                written += 1
                batch_written += 1
            else:
                rejected += 1
                used_mb = written * object_size / 1024 / 1024
                ddr_mb = batch_written * object_size / 1024 / 1024
                if rejected == 1:
                    print(f"  ★ 首次拒绝: key={key}")
                    print(f"    已写入 {written} 个 (~{used_mb:.0f}MB)")
                    print(f"    当前批次 DDR 占用 ~{ddr_mb:.0f}MB / {seg_size/1024/1024:.0f}MB")
                    print(f"    DDR 使用率 ~{ddr_mb/(seg_size/1024/1024)*100:.0f}%，远低于 95% → 确认是 SSD 水位拒绝")
                    print(f"    Master 日志应含 'Refusing allocation'")
                    print(f"    Client 日志应含 'NO_AVAILABLE_HANDLE' (client_service.cpp:1211)")
                if rejected >= 3:
                    break

        if rejected >= 3:
            break

        total_mb = written * object_size / 1024 / 1024
        print(f"  批次 {batch_num}: +{batch_written} 个, "
              f"累计 {written} 个 (~{total_mb:.0f}MB / {effective/1024/1024:.0f}MB effective)")
        # 等待 offload 排空 DDR，避免 DDR 满触发驱逐风暴
        time.sleep(batch_sleep)

    total_mb = written * object_size / 1024 / 1024
    print(f"\n结果：成功 {written} 个 (~{total_mb:.0f}MB), 被拒绝 {rejected} 个")

    if rejected > 0 and total_mb >= trigger_at / 1024 / 1024 - 8:
        print("✓ 验证通过：SSD 水位不足时正确拒绝，未 fallback")
    elif rejected == 0:
        print("✗ 失败：全部写入成功，SSD 水位未触发")
        print("  检查：Master 是否 --allocation_strategy=hard_pin？")
        raise AssertionError("SSD 水位拒绝未生效")
    else:
        print(f"✗ 失败：在 {total_mb:.0f}MB 时被拒，但水位阈值在 ~{trigger_at/1024/1024:.0f}MB")
        print("  可能是 DDR 满导致的拒绝，不是 SSD 水位")
        raise AssertionError("拒绝时机不对，可能是 DDR 而非 SSD")

    store.close()


def test_eviction_protection():
    """验证设计文档 4.1 节：驱逐保护。

    核心验证点：
      - 没有 LOCAL_DISK 副本的 key，MEMORY 不能被驱逐
      - 即使 DDR 写满，旧 key 的数据仍保留在 DDR 中
    """
    print("=== 验证保证1：驱逐保护（无 LOCAL_DISK → MEMORY 不驱逐）===")
    print()

    kv_ttl = int(os.getenv("DEFAULT_KV_LEASE_TTL", "2000"))
    seg_size = 4 * 1024 * 1024  # 4MB DDR
    store = create_store(segment_size=seg_size, buffer_size=seg_size)

    # 写入 protected_key（不 offload，没有 LOCAL_DISK 副本）
    first_key = "protected_key"
    retcode = store.put(first_key, b"\x01" * (1024 * 100))  # 100KB
    if retcode != 0:
        raise RuntimeError(f"写入 {first_key} 失败: retcode={retcode}")
    print(f"  写入 {first_key} (100KB, 无 LOCAL_DISK 副本)")

    # 等待 lease 过期
    print(f"  等待 lease 过期 ({kv_ttl}ms + 500ms)...")
    time.sleep(kv_ttl / 1000.0 + 0.5)

    # 填满 DDR 触发驱逐
    print("  填满 DDR 触发驱逐...")
    fill_success = 0
    for i in range(500):
        key = f"filler_{i}"
        retcode = store.put(key, b"\x02" * (1024 * 100))  # 100KB
        if retcode == 0:
            fill_success += 1
        else:
            # DDR 满了且无法驱逐（驱逐保护生效）
            break
    print(f"  填充写入：{fill_success} 个后 DDR 满")

    # 验证 protected_key 还在
    result = store.get(first_key)
    if result and result != b"":
        print(f"  ✓ {first_key} 仍可读取（驱逐保护生效）")
        print("✓ 验证通过：无 LOCAL_DISK 的 MEMORY 不被驱逐\n")
    else:
        print(f"  ✗ {first_key} 无法读取")
        raise AssertionError("protected_key 被驱逐了，驱逐保护未生效")

    store.close()


def test_ssd_eviction_rejected():
    """验证设计文档保证3：SSD 数据不可驱逐。

    核心验证点：
      - 写入 + offload 后，LOCAL_DISK 副本持久存在
      - 反复读取数据始终正确
    """
    print("=== 验证保证3：SSD 副本不可驱逐 ===")
    print()

    seg_size = 64 * 1024 * 1024
    store = create_store(segment_size=seg_size, buffer_size=seg_size)

    key = "ssd_safe_key"
    data = b"\x03" * 4096
    retcode = store.put(key, data)
    if retcode != 0:
        raise RuntimeError(f"put failed: retcode={retcode}")
    print(f"  写入 {key}")

    # 等待 offload 完成
    print("  等待 offload 完成（5 秒）...")
    time.sleep(5)

    # 验证 offload 后数据可读
    result = store.get(key)
    if not result or result == b"":
        raise AssertionError(f"offload 后读取 {key} 失败")
    if result != data:
        raise AssertionError(f"数据不一致")
    print(f"  ✓ offload 后读取正确")

    # 反复读取验证 SSD 副本持久安全
    for i in range(5):
        time.sleep(1)
        result = store.get(key)
        if not result or result == b"":
            raise AssertionError(f"第 {i+1} 次读取失败，SSD 副本可能被驱逐")
        if result != data:
            raise AssertionError(f"第 {i+1} 次读取数据不一致")

    print("✓ 验证通过：SSD 副本持久安全，反复读取一致\n")
    store.close()


def test_full_lifecycle():
    """验证端到端数据流转：写入 → offload → DDR驱逐 → SSD读取。

    验证设计文档第 5 节的状态流转：
      写入DDR → 排队中 → Offloading → DDR_SSD共存 → SSD仅存
    """
    print("=== 端到端验证：写入 → offload → 驱逐 → SSD 读取 ===")
    print()

    kv_ttl = int(os.getenv("DEFAULT_KV_LEASE_TTL", "2000"))
    seg_size = 64 * 1024 * 1024
    store = create_store(segment_size=seg_size, buffer_size=seg_size)

    # [写入DDR] 阶段
    key = "lifecycle_key"
    data = b"\x04" * (4 * 1024 * 1024)  # 4MB
    retcode = store.put(key, data)
    if retcode != 0:
        raise RuntimeError(f"put failed: retcode={retcode}")
    print(f"  [写入DDR] {key} (4MB) — MEMORY 副本, refcnt > 0")

    # [排队中 → Offloading] 等待 offload
    print("  [排队中 → Offloading] 等待 heartbeat 驱动 DDR→SSD...")
    time.sleep(5)
    result = store.get(key)
    if not result or result == b"":
        raise AssertionError("offload 后读取失败")
    print(f"  [DDR_SSD共存] offload 完成, MEMORY + LOCAL_DISK 都存在")

    # 等待 lease 过期
    print(f"  等待 lease 过期 ({kv_ttl}ms + 500ms)...")
    time.sleep(kv_ttl / 1000.0 + 0.5)

    # [驱逐] 填满 DDR 触发 MEMORY 驱逐（有 LOCAL_DISK，可驱逐）
    print("  [驱逐] 填满 DDR 触发 MEMORY 驱逐...")
    fill_count = 0
    for i in range(50):
        retcode = store.put(f"filler_{i}", b"\x05" * (4 * 1024 * 1024))
        if retcode == 0:
            fill_count += 1
        else:
            break
    print(f"  压力写入 {fill_count} 个 4MB 对象")

    # [SSD仅存] 验证从 SSD 读取
    time.sleep(2)
    result = store.get(key)
    if not result or result == b"":
        raise AssertionError("DDR 驱逐后从 SSD 读取失败")
    if result != data:
        raise AssertionError("DDR 驱逐后数据不一致")
    print(f"  [SSD仅存] {key} 从 SSD 读取成功，数据一致")

    print("✓ 验证通过：完整 DDR→SSD→驱逐→读取 生命周期正常\n")
    store.close()


TESTS = {
    "ssd_full_reject": test_ssd_full_reject,
    "eviction_protection": test_eviction_protection,
    "ssd_eviction_rejected": test_ssd_eviction_rejected,
    "full_lifecycle": test_full_lifecycle,
}


def main():
    parser = argparse.ArgumentParser(description="HardPin 策略人工验证")
    parser.add_argument("--test", required=True, choices=TESTS.keys(),
                        help="要运行的测试名称")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"HardPin 验证: {args.test}")
    print(f"{'='*60}\n")

    try:
        TESTS[args.test]()
        print("所有验证通过！")
    except Exception as e:
        print(f"\n验证失败: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        print("\n>>> 按回车键退出（可先查看 Master 日志）...")
        try:
            input()
        except EOFError:
            pass


if __name__ == "__main__":
    main()
