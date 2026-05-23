#!/usr/bin/env python3
"""HardPin 策略人工验证脚本。

验证设计文档（hard_pin_design.md）中的三大核心保证：
  1. 驱逐保护（4.1）：无 LOCAL_DISK 副本的 MEMORY 不被驱逐
  2. SSD 水位控制（4.2）：effective_free_ratio < watermark → 拒绝分配
  3. SSD 不可驱逐：LOCAL_DISK 副本在任何情况下不被驱逐

用法：
    python verify_hard_pin.py --test <test_name>

关键环境变量：
    MC_METADATA_SERVER                        - Master 元数据地址
    MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES   - SSD 容量上限（字节）
    MOONCAKE_OFFLOAD_FILE_STORAGE_PATH        - SSD 数据存储目录
    MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS - Offload 心跳间隔（建议设为 1）
"""

import argparse
import os
import sys
import time
import traceback
import urllib.request
import json

from mooncake.store import MooncakeDistributedStore

DEFAULT_MASTER_PORT = "50053"
DEFAULT_METADATA_PORT = "8880"
DEFAULT_METRICS_PORT = "9104"


def fetch_metrics():
    """从 Master metrics 端口获取指标。"""
    metrics_port = os.getenv("METRICS_PORT", DEFAULT_METRICS_PORT)
    try:
        url = f"http://127.0.0.1:{metrics_port}/stats"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def print_metrics(label=""):
    """打印当前 Master 的 Mem/SSD 状态。"""
    stats = fetch_metrics()
    if not stats:
        return
    prefix = f"  [{label}] " if label else "  "
    # 尝试从 metrics 中提取关键数值
    for key, val in stats.items() if isinstance(stats, dict) else []:
        if "mem" in key.lower() or "memory" in key.lower() or "ssd" in key.lower() or "file" in key.lower():
            pass  # metrics 格式不确定，仅打印原始数据
    # 简单打印关键字段
    try:
        mem_total = stats.get("master_total_segment_capacity_bytes", 0)
        mem_used = stats.get("master_used_memory_bytes", 0)
        ssd_total = stats.get("master_total_file_capacity_bytes", 0)
        ssd_used = stats.get("master_allocated_file_size_bytes", 0)
        if mem_total > 0:
            print(f"{prefix}Mem: {mem_used/1024/1024:.0f}M/{mem_total/1024/1024:.0f}M "
                  f"({mem_used/mem_total*100:.0f}%)")
        if ssd_total > 0 and ssd_total < 10**15:  # 过滤掉异常大值
            print(f"{prefix}SSD: {ssd_used/1024/1024:.0f}M/{ssd_total/1024/1024:.0f}M "
                  f"({ssd_used/ssd_total*100:.0f}%)")
    except Exception:
        pass


def create_store(segment_size=64 * 1024 * 1024,
                 buffer_size=64 * 1024 * 1024):
    """创建 Store 客户端。"""
    ssd_limit_str = os.getenv("MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES", "")
    if not ssd_limit_str:
        print("[ERROR] MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES 未设置！")
        sys.exit(1)

    ssd_limit = int(ssd_limit_str)
    effective = ssd_limit - segment_size
    if effective <= 0:
        print(f"[ERROR] SSD({ssd_limit}) 必须 > DDR({segment_size})")
        sys.exit(1)

    print(f"  SSD={ssd_limit/1024/1024:.0f}MB, DDR={segment_size/1024/1024:.0f}MB, "
          f"effective={effective/1024/1024:.0f}MB")

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
        local_hostname, metadata_server, segment_size, buffer_size,
        protocol, device_name, master_server,
        enable_ssd_offload=True, ssd_offload_path=ssd_path,
    )
    if retcode:
        raise RuntimeError(f"Store setup 失败: retcode={retcode}")
    return store


# 全局 store 引用，用于 finally 中保持存活
_store = None


def test_ssd_full_reject():
    """验证 SSD 水位控制：effective_free < watermark → 拒绝。

    分批写入，每批后等 offload 排空 DDR。
    通过验证已写入 key 的可读性来确认 offload 确实在工作。
    """
    global _store
    print("=== 验证：SSD 水位拒绝（不 fallback）===")
    print()

    ssd_limit = int(os.getenv("MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES", "0"))
    seg_size = 64 * 1024 * 1024
    effective = ssd_limit - seg_size
    watermark = 0.15
    trigger_at = effective * (1 - watermark)  # used > 此值时拒绝

    print(f"  effective_capacity = {effective/1024/1024:.0f}MB")
    print(f"  watermark = {watermark*100:.0f}%")
    print(f"  理论拒绝阈值: used > {trigger_at/1024/1024:.0f}MB")
    print()

    store = create_store(segment_size=seg_size, buffer_size=seg_size)
    _store = store

    object_size = 4 * 1024 * 1024
    batch_size = 4
    batch_sleep = 5

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
                if rejected == 1:
                    total_written_mb = written * object_size / 1024 / 1024
                    print(f"  ★ 首次拒绝: key={key}, retcode={retcode}")
                    print(f"    已写入 {written} 个 ({total_written_mb:.0f}MB)")
                if rejected >= 3:
                    break

        if rejected >= 3:
            break

        total_mb = written * object_size / 1024 / 1024
        print(f"  批次 {batch_num}: +{batch_written}, "
              f"累计 {written} 个 ({total_mb:.0f}MB)")

        # 等待 offload
        time.sleep(batch_sleep)

        # 验证 offload 是否在工作：读回第一批的第一个 key
        if written > 0:
            check_key = f"ssd_full_key_0"
            result = store.get(check_key)
            if result and len(result) == object_size:
                print(f"    offload 确认: {check_key} 可读取 ({len(result)/1024/1024:.0f}MB)")
            else:
                print(f"    [警告] {check_key} 不可读 (len={len(result) if result else 0})")
                print(f"    offload 可能未运行！检查 MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1")

        # 打印 Master 指标
        print_metrics(f"批次{batch_num}后")

    # 最终报告
    total_mb = written * object_size / 1024 / 1024
    print(f"\n--- 结果 ---")
    print(f"  写入成功: {written} 个 ({total_mb:.0f}MB)")
    print(f"  写入被拒: {rejected} 个")
    print(f"  理论阈值: {trigger_at/1024/1024:.0f}MB (watermark={watermark*100:.0f}%)")

    if rejected == 0:
        print("\n  ✗ 全部写入成功，SSD 水位未触发")
        raise AssertionError("SSD 水位拒绝未生效")

    # 验证 key_0 仍可读（证明数据确实到了 SSD）
    print(f"\n--- 验证 offload ---")
    check_key = "ssd_full_key_0"
    result = store.get(check_key)
    if result and len(result) == object_size:
        print(f"  ✓ {check_key} 可读取 ({len(result)/1024/1024:.0f}MB) — offload 确实在工作")
    else:
        print(f"  ✗ {check_key} 不可读 — offload 可能未运行")
        raise AssertionError("offload 未运行，无法验证 SSD 水位")

    print(f"\n  ✓ 验证通过：{total_mb:.0f}MB 后 SSD 水位触发拒绝")


def test_eviction_protection():
    """验证驱逐保护：无 LOCAL_DISK 的 MEMORY 不被驱逐。"""
    global _store
    print("=== 验证：驱逐保护 ===\n")

    kv_ttl = int(os.getenv("DEFAULT_KV_LEASE_TTL", "2000"))
    seg_size = 4 * 1024 * 1024
    store = create_store(segment_size=seg_size, buffer_size=seg_size)
    _store = store

    first_key = "protected_key"
    first_data = b"\x01" * (1024 * 100)
    retcode = store.put(first_key, first_data)
    if retcode != 0:
        raise RuntimeError(f"put {first_key} 失败: {retcode}")
    print(f"  写入 {first_key} (100KB)")

    print(f"  等待 lease 过期 ({kv_ttl}ms)...")
    time.sleep(kv_ttl / 1000.0 + 0.5)

    print("  填满 DDR...")
    fill = 0
    for i in range(500):
        retcode = store.put(f"filler_{i}", b"\x02" * (1024 * 100))
        if retcode == 0:
            fill += 1
        else:
            break
    print(f"  填充: {fill} 个后写满")

    result = store.get(first_key)
    if result and result != b"":
        print(f"  ✓ {first_key} 仍可读 ({len(result)} bytes) — 驱逐保护生效")
    else:
        print(f"  ✗ {first_key} 不可读 — 被驱逐了")
        raise AssertionError("驱逐保护未生效")


def test_ssd_eviction_rejected():
    """验证 SSD 副本不可驱逐。"""
    global _store
    print("=== 验证：SSD 副本不可驱逐 ===\n")

    seg_size = 64 * 1024 * 1024
    store = create_store(segment_size=seg_size, buffer_size=seg_size)
    _store = store

    key = "ssd_safe_key"
    data = b"\x03" * 4096
    retcode = store.put(key, data)
    if retcode != 0:
        raise RuntimeError(f"put failed: {retcode}")
    print(f"  写入 {key}")

    print("  等 offload (5s)...")
    time.sleep(5)

    for attempt in range(6):
        result = store.get(key)
        if not result or result == b"":
            print(f"  ✗ 第 {attempt} 次读取失败")
            raise AssertionError("SSD 副本丢失")
        if result != data:
            raise AssertionError("数据不一致")
        if attempt == 0:
            print(f"  ✓ offload 后读取正确")
        if attempt < 5:
            time.sleep(1)

    print(f"  ✓ 6 次读取全部一致 — SSD 副本安全")


def test_full_lifecycle():
    """验证完整生命周期：写入 → offload → 驱逐 → SSD 读取。"""
    global _store
    print("=== 验证：完整生命周期 ===\n")

    kv_ttl = int(os.getenv("DEFAULT_KV_LEASE_TTL", "2000"))
    seg_size = 64 * 1024 * 1024
    store = create_store(segment_size=seg_size, buffer_size=seg_size)
    _store = store

    key = "lifecycle_key"
    data = b"\x04" * (4 * 1024 * 1024)

    # 阶段 1: 写入
    retcode = store.put(key, data)
    if retcode != 0:
        raise RuntimeError(f"put failed: {retcode}")
    print(f"  [1] 写入 {key} (4MB)")

    # 阶段 2: 等 offload
    print(f"  [2] 等 offload (5s)...")
    time.sleep(5)
    result = store.get(key)
    if not result or result == b"":
        raise AssertionError("offload 后不可读")
    print(f"      offload 完成, 数据可读")

    # 阶段 3: 等 lease 过期 + 填满 DDR
    print(f"  [3] 等 lease 过期 ({kv_ttl}ms)...")
    time.sleep(kv_ttl / 1000.0 + 0.5)

    fill = 0
    for i in range(50):
        retcode = store.put(f"filler_{i}", b"\x05" * (4 * 1024 * 1024))
        if retcode == 0:
            fill += 1
        else:
            break
    print(f"      压力写入 {fill} 个对象")

    # 阶段 4: 从 SSD 读
    time.sleep(2)
    result = store.get(key)
    if not result or result == b"":
        raise AssertionError("DDR 驱逐后从 SSD 读取失败")
    if result != data:
        raise AssertionError("数据不一致")
    print(f"  [4] 从 SSD 读取成功 — 数据完整")
    print(f"  ✓ 生命周期验证通过")


TESTS = {
    "ssd_full_reject": test_ssd_full_reject,
    "eviction_protection": test_eviction_protection,
    "ssd_eviction_rejected": test_ssd_eviction_rejected,
    "full_lifecycle": test_full_lifecycle,
}


def main():
    parser = argparse.ArgumentParser(description="HardPin 策略人工验证")
    parser.add_argument("--test", required=True, choices=TESTS.keys())
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"HardPin 验证: {args.test}")
    print(f"{'='*60}\n")

    try:
        TESTS[args.test]()
    except Exception as e:
        print(f"\n验证失败: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        # 保持 store 存活直到用户按回车，方便查验
        if _store:
            print(f"\n>>> Store 仍存活，可检查 Master 状态。按回车退出...")
            try:
                input()
            except EOFError:
                pass
            _store.close()


if __name__ == "__main__":
    main()
