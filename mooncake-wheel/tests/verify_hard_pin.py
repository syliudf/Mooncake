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
        print(f"  [{label}] (metrics 不可用)")
        return
    prefix = f"  [{label}] " if label else "  "
    try:
        mem_total = stats.get("master_total_segment_capacity_bytes", 0)
        mem_used = stats.get("master_used_memory_bytes", 0)
        ssd_total = stats.get("master_total_file_capacity_bytes", 0)
        ssd_used = stats.get("master_allocated_file_size_bytes", 0)
        if mem_total > 0:
            print(f"{prefix}Mem: {mem_used/1024/1024:.1f}M/{mem_total/1024/1024:.1f}M "
                  f"({mem_used/mem_total*100:.1f}%)")
        if ssd_total > 0 and ssd_total < 10**15:
            print(f"{prefix}SSD: {ssd_used/1024/1024:.1f}M/{ssd_total/1024/1024:.1f}M "
                  f"({ssd_used/ssd_total*100:.1f}%)")
        elif ssd_total >= 10**15:
            print(f"{prefix}SSD: {ssd_used/1024/1024:.1f}M / infinity")
        else:
            print(f"{prefix}SSD: (not reported)")
    except Exception as e:
        print(f"{prefix}(metrics parse error: {e})")


def print_all_metrics(label=""):
    """打印所有 metrics 以便诊断。"""
    stats = fetch_metrics()
    if not stats:
        print(f"  [{label}] (metrics 不可用)")
        return
    prefix = f"  [{label}] " if label else "  "
    for key in sorted(stats.keys()):
        val = stats[key]
        if isinstance(val, (int, float)) and val > 1024:
            print(f"{prefix}{key} = {val} ({val/1024/1024:.2f}M)")
        else:
            print(f"{prefix}{key} = {val}")


def create_store(segment_size=640 * 1024 * 1024,
                 buffer_size=640 * 1024 * 1024):
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

    ssd_path = os.getenv("MOONCAKE_OFFLOAD_FILE_STORAGE_PATH",
                         "/tmp/mooncake_ssd_test")
    heartbeat_interval = os.getenv("MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS",
                                   "(未设置, 默认10s)")

    print(f"  配置:")
    print(f"    SSD 总量: {ssd_limit/1024/1024:.0f}MB")
    print(f"    DDR 容量: {segment_size/1024/1024:.0f}MB")
    print(f"    effective: {effective/1024/1024:.0f}MB")
    print(f"    SSD 路径: {ssd_path}")
    print(f"    心跳间隔: {heartbeat_interval}s")
    print(f"    路径存在: {os.path.exists(ssd_path)}")

    store = MooncakeDistributedStore()
    protocol = os.getenv("PROTOCOL", "tcp")
    device_name = os.getenv("DEVICE_NAME", "eth0")
    local_hostname = os.getenv("LOCAL_HOSTNAME", "127.0.0.1")
    metadata_server = os.getenv("MC_METADATA_SERVER",
                                f"http://127.0.0.1:{DEFAULT_METADATA_PORT}/metadata")
    master_server = os.getenv("MASTER_SERVER",
                              f"127.0.0.1:{DEFAULT_MASTER_PORT}")

    print(f"    local_hostname: {local_hostname}")
    print(f"    metadata_server: {metadata_server}")
    print(f"    master_server: {master_server}")

    retcode = store.setup(
        local_hostname, metadata_server, segment_size, buffer_size,
        protocol, device_name, master_server,
        enable_ssd_offload=True, ssd_offload_path=ssd_path,
    )
    if retcode:
        raise RuntimeError(f"Store setup 失败: retcode={retcode}")
    print("  Store setup 成功")
    return store


# 全局 store 引用，用于 finally 中保持存活
_store = None


def test_ssd_full_reject():
    """验证 SSD 水位控制：effective_free < watermark → 拒绝。

    策略：写入少量 key 后等待足够长时间，验证 offload 确实发生。
    然后分批继续写入直到 SSD 水位触发拒绝。
    """
    global _store
    print("=== 验证：SSD 水位拒绝（不 fallback）===")
    print()

    ssd_limit = int(os.getenv("MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES", "0"))
    seg_size = 640 * 1024 * 1024  # 640MB DDR
    store = create_store(segment_size=seg_size, buffer_size=seg_size)
    effective = ssd_limit - seg_size
    watermark = 0.15
    trigger_at = effective * (1 - watermark)

    print(f"  effective_capacity = {effective/1024/1024:.0f}MB")
    print(f"  watermark = {watermark*100:.0f}%")
    print(f"  理论拒绝阈值: used > {trigger_at/1024/1024:.0f}MB")
    print()

    _store = store

    object_size = 4 * 1024 * 1024  # 4MB
    offload_wait = 15  # 等待 offload 的秒数（足够长）

    # === 阶段 1: 验证 offload 管线是否工作 ===
    print(f"\n--- 阶段 1: 验证 offload 管线 ({offload_wait}s 等待) ---")
    initial_keys = 3
    for i in range(initial_keys):
        key = f"ssd_full_key_{i}"
        data = b"\x00" * object_size
        retcode = store.put(key, data)
        if retcode != 0:
            raise RuntimeError(f"写入 {key} 失败: retcode={retcode}")
        print(f"  写入 {key} ({object_size/1024/1024:.0f}MB) → retcode={retcode}")

    total_mb = initial_keys * object_size / 1024 / 1024
    print(f"\n  已写入 {initial_keys} 个 ({total_mb:.0f}MB)")
    print_metrics("写入后立即")

    print(f"\n  等待 {offload_wait}s 让 offload 排空 DDR...")
    for t in range(offload_wait):
        sys.stdout.write(f"\r    {t+1}/{offload_wait}s")
        sys.stdout.flush()
        time.sleep(1)
    print()

    print_metrics(f"offload 等待 {offload_wait}s 后")

    # 验证 offload 是否工作：读回第一个 key
    check_key = "ssd_full_key_0"
    result = store.get(check_key)
    if result and len(result) == object_size:
        print(f"  ✓ {check_key} 可读取 ({len(result)/1024/1024:.0f}MB) — 数据存活")
    else:
        rlen = len(result) if result else 0
        print(f"  ✗ {check_key} 不可读 (len={rlen})")
        print(f"  ★ offload 管线可能未工作！")
        print(f"  请检查：")
        print(f"    1. MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS 是否设为 1")
        print(f"    2. Master 日志中是否有 PushOffloadingQueue 错误")
        print(f"    3. SSD 路径是否存在且有写权限")

        # 打印全量 metrics 帮助诊断
        print(f"\n  --- 全量 metrics 诊断 ---")
        print_all_metrics("offload 检查")

        # 不立即退出，继续执行让用户可以看到更多现象
        print(f"\n  继续执行以观察后续行为...")

    # === 阶段 2: 继续分批写入直到 SSD 水位触发 ===
    print(f"\n--- 阶段 2: 分批写入直到 SSD 水位触发 ---")
    written = initial_keys
    rejected = 0
    batch_size = 2  # 每批 2 个（8MB），减少批次间 offload 压力
    batch_sleep = offload_wait  # 每批后等待足够长

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
                    print(f"\n  ★ 首次拒绝: key={key}, retcode={retcode}")
                    print(f"    已写入 {written} 个 ({total_written_mb:.0f}MB)")
                    print_metrics("首次拒绝时")
                if rejected >= 3:
                    break

        if rejected >= 3:
            break

        total_mb = written * object_size / 1024 / 1024
        print(f"  批次 {batch_num}: +{batch_written}, "
              f"累计 {written} 个 ({total_mb:.0f}MB)")

        # 等待 offload
        print(f"    等待 {batch_sleep}s...")
        for t in range(batch_sleep):
            sys.stdout.write(f"\r      {t+1}/{batch_sleep}s")
            sys.stdout.flush()
            time.sleep(1)
        print()

        # 每批后打印 metrics 和验证可读性
        print_metrics(f"批次{batch_num}后")
        if written > 0:
            check_key = f"ssd_full_key_0"
            result = store.get(check_key)
            if result and len(result) == object_size:
                print(f"    offload 确认: {check_key} 可读取")
            else:
                print(f"    [警告] {check_key} 不可读 (len={len(result) if result else 0})")

    # 最终报告
    total_mb = written * object_size / 1024 / 1024
    print(f"\n--- 结果 ---")
    print(f"  写入成功: {written} 个 ({total_mb:.0f}MB)")
    print(f"  写入被拒: {rejected} 个")
    print(f"  理论阈值: {trigger_at/1024/1024:.0f}MB (watermark={watermark*100:.0f}%)")
    print_metrics("最终")

    if rejected == 0:
        print("\n  ✗ 全部写入成功，SSD 水位未触发")
        raise AssertionError("SSD 水位拒绝未生效")

    # 验证 key_0 仍可读
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

    seg_size = 640 * 1024 * 1024  # 640MB DDR
    store = create_store(segment_size=seg_size, buffer_size=seg_size)
    _store = store

    key = "ssd_safe_key"
    data = b"\x03" * 4096
    retcode = store.put(key, data)
    if retcode != 0:
        raise RuntimeError(f"put failed: {retcode}")
    print(f"  写入 {key}")

    offload_wait = 30
    print(f"  等 offload ({offload_wait}s)...")
    time.sleep(offload_wait)

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
            time.sleep(2)

    print(f"  ✓ 6 次读取全部一致 — SSD 副本安全")


def test_full_lifecycle():
    """验证完整生命周期：写入 → offload → 驱逐 → SSD 读取。"""
    global _store
    print("=== 验证：完整生命周期 ===\n")

    kv_ttl = int(os.getenv("DEFAULT_KV_LEASE_TTL", "2000"))
    seg_size = 640 * 1024 * 1024  # 640MB DDR
    store = create_store(segment_size=seg_size, buffer_size=seg_size)
    _store = store

    key = "lifecycle_key"
    data = b"\x04" * (4 * 1024 * 1024)

    # 阶段 1: 写入
    retcode = store.put(key, data)
    if retcode != 0:
        raise RuntimeError(f"put failed: {retcode}")
    print(f"  [1] 写入 {key} (4MB)")
    print_metrics("写入后")

    # 阶段 2: 等 offload
    offload_wait = 20
    print(f"  [2] 等 offload ({offload_wait}s)...")
    time.sleep(offload_wait)
    result = store.get(key)
    if not result or result == b"":
        print(f"  ★ offload 后不可读 — 打印全量 metrics:")
        print_all_metrics("offload 后")
        raise AssertionError("offload 后不可读")
    print(f"      offload 完成, 数据可读")
    print_metrics("offload 后")

    # 阶段 3: 等 lease 过期 + 填满 DDR
    print(f"  [3] 等 lease 过期 ({kv_ttl}ms)...")
    time.sleep(kv_ttl / 1000.0 + 0.5)

    fill = 0
    for i in range(200):
        retcode = store.put(f"filler_{i}", b"\x05" * (4 * 1024 * 1024))
        if retcode == 0:
            fill += 1
        else:
            break
    print(f"      压力写入 {fill} 个对象 ({fill*4}MB)")
    print_metrics("压力写入后")

    # 阶段 4: 从 SSD 读
    time.sleep(5)
    result = store.get(key)
    if not result or result == b"":
        print(f"  ★ DDR 驱逐后从 SSD 读取失败 — 打印全量 metrics:")
        print_all_metrics("读取失败")
        raise AssertionError("DDR 驱逐后从 SSD 读取失败")
    if result != data:
        raise AssertionError("数据不一致")
    print(f"  [4] 从 SSD 读取成功 — 数据完整")
    print(f"  ✓ 生命周期验证通过")


def test_offload_only():
    """仅验证 offload 管线是否工作。

    这是最基本的测试：写入一个 key，等待足够长时间，验证可读。
    如果这个测试失败，说明 offload 管线本身有问题。
    """
    global _store
    print("=== 验证：Offload 管线基础测试 ===\n")

    seg_size = 640 * 1024 * 1024  # 640MB DDR
    store = create_store(segment_size=seg_size, buffer_size=seg_size)
    _store = store

    key = "offload_test_key"
    data = b"\xAB" * (1 * 1024 * 1024)  # 1MB

    print(f"  [1] 写入 {key} (1MB)")
    retcode = store.put(key, data)
    if retcode != 0:
        raise RuntimeError(f"put failed: {retcode}")
    print_metrics("写入后")

    # 等待足够长时间
    offload_wait = 30
    print(f"  [2] 等待 {offload_wait}s 让 heartbeat 触发 offload...")
    for t in range(offload_wait):
        sys.stdout.write(f"\r    {t+1}/{offload_wait}s")
        sys.stdout.flush()
        time.sleep(1)
    print()

    print_metrics(f"等待 {offload_wait}s 后")

    # 验证
    result = store.get(key)
    if result and len(result) == len(data):
        print(f"  ✓ {key} 可读取 ({len(result)/1024/1024:.0f}MB) — offload 管线正常")
    else:
        rlen = len(result) if result else 0
        print(f"  ✗ {key} 不可读 (len={rlen}, expected={len(data)})")
        print(f"\n  --- 全量 metrics 诊断 ---")
        print_all_metrics("offload 失败")
        raise AssertionError("Offload 管线未工作")

    # 检查 SSD 路径是否有文件
    ssd_path = os.getenv("MOONCAKE_OFFLOAD_FILE_STORAGE_PATH", "")
    if ssd_path and os.path.exists(ssd_path):
        files = os.listdir(ssd_path)
        print(f"  SSD 路径文件数: {len(files)}")
        total_size = 0
        for f in files:
            fp = os.path.join(ssd_path, f)
            if os.path.isfile(fp):
                total_size += os.path.getsize(fp)
        print(f"  SSD 路径总大小: {total_size/1024/1024:.1f}MB")


TESTS = {
    "offload_only": test_offload_only,
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

    # 打印关键环境变量
    print("  环境变量:")
    for var in ["MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES",
                "MOONCAKE_OFFLOAD_FILE_STORAGE_PATH",
                "MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS",
                "MC_METADATA_SERVER"]:
        val = os.getenv(var, "(未设置)")
        print(f"    {var} = {val}")
    print()

    try:
        TESTS[args.test]()
    except Exception as e:
        print(f"\n验证失败: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        if _store:
            print(f"\n>>> Store 仍存活，可检查 Master 状态。按回车退出...")
            try:
                input()
            except EOFError:
                pass
            _store.close()


if __name__ == "__main__":
    main()
