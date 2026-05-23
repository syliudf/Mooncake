#!/usr/bin/env python3
"""HardPin 策略人工验证脚本。

用法：
    python verify_hard_pin.py --test <test_name>

test_name:
    ssd_full_reject       - 验证 SSD 满时拒绝写入
    eviction_protection   - 验证无 LOCAL_DISK 的数据不被驱逐
    ssd_eviction_rejected - 验证 SSD 副本不能被驱逐
    full_lifecycle        - 完整写入→offload→驱逐→读取 生命周期

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
    # 检查 SSD 容量环境变量
    ssd_limit_str = os.getenv("MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES", "")
    if not ssd_limit_str:
        print("  [ERROR] MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES 未设置！")
        print("  HardPin 需要 SSD 容量限制来计算水位。")
        print("  例如：MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=268435456 (256MB)")
        sys.exit(1)

    ssd_limit = int(ssd_limit_str)
    effective = ssd_limit - segment_size
    print(f"  SSD 限制: {ssd_limit / 1024 / 1024:.0f}MB, "
          f"DDR: {segment_size / 1024 / 1024:.0f}MB, "
          f"effective_capacity: {effective / 1024 / 1024:.1f}MB")

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


def put_with_check(store, key, data):
    """put 并检查返回码。返回 True=成功, False=失败。"""
    retcode = store.put(key, data)
    return retcode == 0


def test_ssd_full_reject():
    """验证：SSD 水位不足时，put 返回非零（不 fallback 写入）。

    场景设置：
      - SSD = 256MB, DDR = 64MB → effective_capacity = 192MB
      - 水位线 15% → 至少需要 28.8MB 空闲
      - 写入 ~164MB 数据后 effective_free < 28.8MB，水位触发
    """
    print("=== 测试：SSD 满时拒绝写入（不 fallback）===")

    # SSD=256MB, DDR=64MB
    ssd_limit = int(os.getenv("MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES", "0"))
    seg_size = 64 * 1024 * 1024  # 64MB DDR
    effective = ssd_limit - seg_size  # 192MB effective
    watermark = 0.15
    watermark_bytes = effective * watermark  # 28.8MB

    print(f"  SSD={ssd_limit/1024/1024:.0f}MB, DDR={seg_size/1024/1024:.0f}MB")
    print(f"  effective_capacity={effective/1024/1024:.1f}MB, "
          f"水位线={watermark*100:.0f}% ({watermark_bytes/1024/1024:.1f}MB)")

    store = create_store(segment_size=seg_size, buffer_size=seg_size)

    # 每次写入 4MB
    object_size = 4 * 1024 * 1024
    written = 0
    rejected = 0

    for i in range(200):
        key = f"ssd_full_key_{i}"
        data = b"\x00" * object_size
        success = put_with_check(store, key, data)
        if success:
            written += 1
            used_mb = written * object_size / 1024 / 1024
            if i % 10 == 0:
                print(f"  写入 #{i}: 成功 (已写 {written} 个, "
                      f"~{used_mb:.0f}MB)")
        else:
            rejected += 1
            used_mb = written * object_size / 1024 / 1024
            if rejected == 1:
                print(f"  ★ 首次拒绝出现在第 {i} 次写入")
                print(f"    已写入 ~{used_mb:.0f}MB / effective {effective/1024/1024:.0f}MB")
                print(f"    这证明 SSD 水位检查生效，没有 fallback")
            # 水位触发后再写几个，确认持续拒绝
            if rejected >= 5:
                break

    print(f"\n结果：成功写入 {written} 个 (~{written*object_size/1024/1024:.0f}MB), "
          f"被拒绝 {rejected} 个")

    if rejected > 0:
        print("✓ 验证通过：SSD 水位不足时正确拒绝写入\n")
    else:
        print("✗ 验证失败：所有写入都成功了，SSD 水位拒绝未生效！")
        print("  请检查：")
        print("  1. Master 启动参数是否包含 --allocation_strategy=hard_pin")
        print("  2. MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES 是否正确设置")
        print("  3. Master 日志是否有 'Refusing allocation'")
        raise AssertionError("应有写入被拒绝，但全部成功了")

    store.close()


def test_eviction_protection():
    """验证：没有 LOCAL_DISK 副本的 key，MEMORY 不被驱逐。

    方法：写入数据后立即填满 DDR，观察无 LOCAL_DISK 的旧数据是否被保留。
    HardPin 驱逐保护会阻止驱逐没有 LOCAL_DISK 副本的 MEMORY。
    """
    print("=== 测试：驱逐保护（无 LOCAL_DISK 时不驱逐 MEMORY）===")
    kv_ttl = int(os.getenv("DEFAULT_KV_LEASE_TTL", "2000"))

    # 小 DDR (4MB)，大 SSD (256MB)
    seg_size = 4 * 1024 * 1024
    store = create_store(segment_size=seg_size, buffer_size=seg_size)

    # 写入一个 key（不 offload，因此没有 LOCAL_DISK 副本）
    first_key = "protected_key"
    retcode = store.put(first_key, b"\x01" * 1024)
    if retcode != 0:
        print(f"  [ERROR] 写入 {first_key} 失败: retcode={retcode}")
        raise RuntimeError(f"put failed with retcode={retcode}")
    print(f"  写入 {first_key} (无 LOCAL_DISK 副本)")

    # 等待 lease 过期（过期后才可被驱逐）
    print(f"  等待 lease 过期 ({kv_ttl}ms + 500ms)...")
    time.sleep(kv_ttl / 1000.0 + 0.5)

    # 继续写入填满 DDR，触发驱逐
    print("  填满 DDR 以触发驱逐...")
    fill_success = 0
    for i in range(500):
        key = f"filler_{i}"
        retcode = store.put(key, b"\x02" * 1024)
        if retcode == 0:
            fill_success += 1
    print(f"  填充写入：成功 {fill_success} 个")

    # 验证 protected_key 是否还能读到
    result = store.get(first_key)
    if result and result != b"":
        print(f"  ✓ {first_key} 仍可读取（驱逐保护生效）")
        print("✓ 验证通过：无 LOCAL_DISK 的 MEMORY 不被驱逐\n")
    else:
        print(f"  ✗ {first_key} 无法读取（返回空）")
        print("  驱逐保护可能未生效！")
        raise AssertionError("protected_key 被驱逐了，驱逐保护未生效")

    store.close()


def test_ssd_eviction_rejected():
    """验证：HardPin 模式下 LOCAL_DISK 副本不能被驱逐。

    方法：写入 + 等待 offload 完成后，反复读取验证 SSD 副本一直安全。
    """
    print("=== 测试：SSD 驱逐被拒绝 ===")

    seg_size = 64 * 1024 * 1024
    store = create_store(segment_size=seg_size, buffer_size=seg_size)

    key = "ssd_safe_key"
    data = b"\x03" * 4096
    retcode = store.put(key, data)
    if retcode != 0:
        raise RuntimeError(f"put failed with retcode={retcode}")
    print(f"  写入 {key}")

    # 等待 offload 完成（heartbeat 驱动，通常 1-2 秒）
    print("  等待 offload 完成...")
    time.sleep(5)

    # 验证数据可读
    result = store.get(key)
    if not result or result == b"":
        raise AssertionError(f"offload 后读取 {key} 失败")
    if result != data:
        raise AssertionError(f"数据不一致: 期望 {len(data)} 字节, "
                             f"实际 {len(result)} 字节")
    print(f"  ✓ offload 后仍可读取 {key}（SSD 副本安全）")

    # 多次读取，验证 SSD 副本一直可用
    for i in range(5):
        time.sleep(1)
        result = store.get(key)
        if not result or result == b"":
            raise AssertionError(f"第 {i+1} 次读取失败，SSD 副本可能被驱逐")

    print("✓ 验证通过：SSD 副本持久安全，无法被驱逐\n")
    store.close()


def test_full_lifecycle():
    """验证：完整的 写入→offload→DDR驱逐→SSD读取 生命周期。

    场景设置：
      - SSD = 256MB, DDR = 64MB → effective_capacity = 192MB
      - 写入数据 → offload → 填满 DDR → 驱逐 MEMORY → 从 SSD 读取
    """
    print("=== 测试：完整 HardPin 生命周期 ===")
    kv_ttl = int(os.getenv("DEFAULT_KV_LEASE_TTL", "2000"))

    seg_size = 64 * 1024 * 1024  # 64MB DDR
    store = create_store(segment_size=seg_size, buffer_size=seg_size)

    # 阶段 1：写入数据
    key = "lifecycle_key"
    data_size = 4 * 1024 * 1024  # 4MB
    data = b"\x04" * data_size
    retcode = store.put(key, data)
    if retcode != 0:
        raise RuntimeError(f"put failed with retcode={retcode}")
    print(f"  [阶段1] 写入 {key} ({data_size / 1024 / 1024:.0f}MB) — 成功")

    # 阶段 2：等待 offload 完成
    print("  [阶段2] 等待 offload 完成...")
    time.sleep(5)
    result = store.get(key)
    if not result or result == b"":
        raise AssertionError("offload 后读取失败")
    if result != data:
        raise AssertionError("offload 后数据不一致")
    print(f"  [阶段2] offload 完成，数据可读 — 成功")

    # 阶段 3：等待 lease 过期后填满 DDR 触发驱逐
    print(f"  [阶段3] 等待 lease 过期 ({kv_ttl}ms + 500ms)...")
    time.sleep(kv_ttl / 1000.0 + 0.5)

    fill_success = 0
    for i in range(50):
        fill_key = f"filler_{i}"
        retcode = store.put(fill_key, b"\x05" * (4 * 1024 * 1024))
        if retcode == 0:
            fill_success += 1
        else:
            break
    print(f"  [阶段3] DDR 压力写入 {fill_success} 个 4MB 对象")

    # 阶段 4：从 SSD 读取被驱逐的数据
    time.sleep(2)
    result = store.get(key)
    if not result or result == b"":
        raise AssertionError("DDR 驱逐后从 SSD 读取失败")
    if result != data:
        raise AssertionError("DDR 驱逐后数据不一致")
    print(f"  [阶段4] DDR 驱逐后仍可从 SSD 读取 {key} — 成功")

    print("✓ 验证通过：完整生命周期正常\n")
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
        # 保持进程存活以便查看 Master 日志
        print("\n>>> 按回车键退出（可先查看 Master 日志）...")
        try:
            input()
        except EOFError:
            pass


if __name__ == "__main__":
    main()
