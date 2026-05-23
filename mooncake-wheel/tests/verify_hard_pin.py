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

# 默认规模：DDR=4GB, SSD=16GB
DEFAULT_DDR_SIZE = 4 * 1024 * 1024 * 1024    # 4GB
DEFAULT_SSD_SIZE = 16 * 1024 * 1024 * 1024   # 16GB
KEY_SIZE = 4 * 1024 * 1024                    # 4MB
INSERT_INTERVAL = 0.01                        # 10ms


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
            print(f"{prefix}Mem: {mem_used/1024/1024:.0f}M/{mem_total/1024/1024:.0f}M "
                  f"({mem_used/mem_total*100:.1f}%)")
        if ssd_total > 0 and ssd_total < 10**15:
            print(f"{prefix}SSD: {ssd_used/1024/1024:.0f}M/{ssd_total/1024/1024:.0f}M "
                  f"({ssd_used/ssd_total*100:.1f}%)")
        elif ssd_total >= 10**15:
            print(f"{prefix}SSD: {ssd_used/1024/1024:.0f}M / infinity")
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


def create_store(segment_size=DEFAULT_DDR_SIZE,
                 buffer_size=DEFAULT_DDR_SIZE):
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
    print(f"    SSD: {ssd_limit/1024/1024/1024:.1f}GB")
    print(f"    DDR: {segment_size/1024/1024/1024:.1f}GB")
    print(f"    effective: {effective/1024/1024/1024:.1f}GB")
    print(f"    SSD 路径: {ssd_path}")
    print(f"    心跳间隔: {heartbeat_interval}s")

    store = MooncakeDistributedStore()
    protocol = os.getenv("PROTOCOL", "tcp")
    device_name = os.getenv("DEVICE_NAME", "eth0")
    local_hostname = os.getenv("LOCAL_HOSTNAME", "127.0.0.1")
    metadata_server = os.getenv("MC_METADATA_SERVER",
                                f"http://127.0.0.1:{DEFAULT_METADATA_PORT}/metadata")
    master_server = os.getenv("MASTER_SERVER",
                              f"127.0.0.1:{DEFAULT_MASTER_PORT}")

    print(f"    metadata_server: {metadata_server}")
    print(f"    master_server: {master_server}")

    t0 = time.time()
    retcode = store.setup(
        local_hostname, metadata_server, segment_size, buffer_size,
        protocol, device_name, master_server,
        enable_ssd_offload=True, ssd_offload_path=ssd_path,
    )
    elapsed = time.time() - t0
    if retcode:
        raise RuntimeError(f"Store setup 失败: retcode={retcode}")
    print(f"  Store setup 成功 ({elapsed:.1f}s)")
    return store


# 全局 store 引用
_store = None


def wait_with_progress(seconds, prefix=""):
    """等待指定秒数，显示进度。"""
    for t in range(seconds):
        sys.stdout.write(f"\r  {prefix}{t+1}/{seconds}s")
        sys.stdout.flush()
        time.sleep(1)
    print()


def test_offload_only():
    """验证 offload 管线：写入 200MB 数据，等待 offload。"""
    global _store
    print("=== 验证：Offload 管线基础测试 ===\n")

    store = create_store()
    _store = store

    num_keys = 50  # 50 × 4MB = 200MB
    print(f"\n  [1] 写入 {num_keys} 个 4MB key ({num_keys*4}MB)...")
    t0 = time.time()
    for i in range(num_keys):
        key = f"offload_key_{i}"
        data = b"\xAB" * KEY_SIZE
        retcode = store.put(key, data)
        if retcode != 0:
            print(f"  ✗ 写入 {key} 失败: retcode={retcode}")
            raise RuntimeError(f"put failed at key {i}")
        if (i + 1) % 10 == 0:
            print(f"    {i+1}/{num_keys} 写入完成 ({(i+1)*4}MB, {time.time()-t0:.1f}s)")
        time.sleep(INSERT_INTERVAL)

    print(f"  写入完成: {num_keys} 个 ({num_keys*4}MB, {time.time()-t0:.1f}s)")
    print_metrics("写入后")

    offload_wait = 20
    print(f"\n  [2] 等待 {offload_wait}s 让 heartbeat 触发 offload...")
    wait_with_progress(offload_wait)

    print_metrics(f"offload 等待 {offload_wait}s 后")

    # 验证随机 key 可读
    test_keys = ["offload_key_0", "offload_key_25", "offload_key_49"]
    all_ok = True
    for key in test_keys:
        result = store.get(key)
        if result and len(result) == KEY_SIZE:
            print(f"  ✓ {key} 可读取 ({len(result)/1024/1024:.0f}MB)")
        else:
            rlen = len(result) if result else 0
            print(f"  ✗ {key} 不可读 (len={rlen})")
            all_ok = False

    if not all_ok:
        print(f"\n  --- 全量 metrics 诊断 ---")
        print_all_metrics("offload 失败")
        raise AssertionError("Offload 管线验证失败")

    # 检查 SSD 路径
    ssd_path = os.getenv("MOONCAKE_OFFLOAD_FILE_STORAGE_PATH", "")
    if ssd_path and os.path.exists(ssd_path):
        files = [f for f in os.listdir(ssd_path) if os.path.isfile(os.path.join(ssd_path, f))]
        total_size = sum(os.path.getsize(os.path.join(ssd_path, f)) for f in files)
        print(f"  SSD 路径: {len(files)} 个文件, {total_size/1024/1024:.0f}MB")

    print(f"\n  ✓ Offload 管线验证通过")


def test_ssd_full_reject():
    """验证 SSD 水位控制：写入到 effective_capacity × 85% 后触发拒绝。"""
    global _store
    print("=== 验证：SSD 水位拒绝（不 fallback）===\n")

    ssd_limit = int(os.getenv("MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES", "0"))
    seg_size = DEFAULT_DDR_SIZE
    store = create_store(segment_size=seg_size, buffer_size=seg_size)
    _store = store

    effective = ssd_limit - seg_size
    watermark = 0.15
    trigger_at = effective * (1 - watermark)  # used > 此值时拒绝

    print(f"\n  effective_capacity = {effective/1024/1024/1024:.1f}GB")
    print(f"  watermark = {watermark*100:.0f}%")
    print(f"  理论拒绝阈值: used > {trigger_at/1024/1024/1024:.1f}GB")
    print(f"  需要写入约 {int(trigger_at / KEY_SIZE)} 个 4MB key\n")

    written = 0
    rejected = 0
    batch_size = 100
    batch_sleep = 5  # 每批后等 5s 让 offload 排空 DDR
    report_interval = 500
    t0 = time.time()

    for batch_idx in range(10000):
        batch_written = 0
        for i in range(batch_size):
            key = f"ssd_full_key_{written}"
            data = b"\x00" * KEY_SIZE
            retcode = store.put(key, data)

            if retcode == 0:
                written += 1
                batch_written += 1
            else:
                rejected += 1
                if rejected == 1:
                    total_gb = written * KEY_SIZE / 1024 / 1024 / 1024
                    print(f"\n  ★ 首次拒绝: key={key}, retcode={retcode}")
                    print(f"    已写入 {written} 个 ({total_gb:.1f}GB)")
                    print_metrics("首次拒绝时")
                if rejected >= 5:
                    break

            time.sleep(INSERT_INTERVAL)

        if rejected >= 5:
            break

        # 报告进度
        if written % report_interval < batch_size:
            total_gb = written * KEY_SIZE / 1024 / 1024 / 1024
            elapsed = time.time() - t0
            speed = written / elapsed if elapsed > 0 else 0
            print(f"  {written} 个 ({total_gb:.1f}GB), "
                  f"{speed:.0f} keys/s, {elapsed:.0f}s")

        time.sleep(batch_sleep)

    # 最终报告
    total_gb = written * KEY_SIZE / 1024 / 1024 / 1024
    trigger_gb = trigger_at / 1024 / 1024 / 1024
    print(f"\n--- 结果 ---")
    print(f"  写入成功: {written} 个 ({total_gb:.1f}GB)")
    print(f"  写入被拒: {rejected} 个")
    print(f"  理论阈值: {trigger_gb:.1f}GB (watermark={watermark*100:.0f}%)")
    print_metrics("最终")

    if rejected == 0:
        print("\n  ✗ 未触发 SSD 水位拒绝")
        raise AssertionError("SSD 水位拒绝未生效")

    print(f"\n  ✓ 验证通过：{total_gb:.1f}GB 后 SSD 水位触发拒绝")


def test_eviction_protection():
    """验证驱逐保护：无 LOCAL_DISK 的 MEMORY 不被驱逐。"""
    global _store
    print("=== 验证：驱逐保护 ===\n")

    kv_ttl = int(os.getenv("DEFAULT_KV_LEASE_TTL", "2000"))
    # 用小 DDR 以便快速触发驱逐
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
        time.sleep(INSERT_INTERVAL)
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

    store = create_store()
    _store = store

    key = "ssd_safe_key"
    data = b"\x03" * 4096
    retcode = store.put(key, data)
    if retcode != 0:
        raise RuntimeError(f"put failed: {retcode}")
    print(f"  写入 {key}")

    offload_wait = 20
    print(f"  等 offload ({offload_wait}s)...")
    wait_with_progress(offload_wait)

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
    store = create_store()
    _store = store

    key = "lifecycle_key"
    data = b"\x04" * KEY_SIZE

    # 阶段 1: 写入
    retcode = store.put(key, data)
    if retcode != 0:
        raise RuntimeError(f"put failed: {retcode}")
    print(f"  [1] 写入 {key} (4MB)")
    print_metrics("写入后")

    # 阶段 2: 等 offload
    offload_wait = 20
    print(f"  [2] 等 offload ({offload_wait}s)...")
    wait_with_progress(offload_wait)
    result = store.get(key)
    if not result or result == b"":
        print_all_metrics("offload 失败")
        raise AssertionError("offload 后不可读")
    print(f"      offload 完成, 数据可读")
    print_metrics("offload 后")

    # 阶段 3: 等 lease 过期 + 填满 DDR
    print(f"  [3] 等 lease 过期 ({kv_ttl}ms)...")
    time.sleep(kv_ttl / 1000.0 + 0.5)

    # 填满 4GB DDR：需要 ~1000 个 4MB key
    print(f"  填满 DDR (写入 1100 个 4MB key)...")
    t0 = time.time()
    fill = 0
    for i in range(1100):
        retcode = store.put(f"filler_{i}", b"\x05" * KEY_SIZE)
        if retcode == 0:
            fill += 1
        else:
            break
        if (i + 1) % 200 == 0:
            print(f"    {i+1}/1100 ({(i+1)*4}MB, {time.time()-t0:.1f}s)")
        time.sleep(INSERT_INTERVAL)
    print(f"  压力写入 {fill} 个 ({fill*4}MB, {time.time()-t0:.1f}s)")
    print_metrics("压力写入后")

    # 阶段 4: 从 SSD 读
    time.sleep(5)
    result = store.get(key)
    if not result or result == b"":
        print_all_metrics("读取失败")
        raise AssertionError("DDR 驱逐后从 SSD 读取失败")
    if result != data:
        raise AssertionError("数据不一致")
    print(f"  [4] 从 SSD 读取成功 — 数据完整")
    print(f"  ✓ 生命周期验证通过")


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
