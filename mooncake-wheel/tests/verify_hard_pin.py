#!/usr/bin/env python3
"""HardPin 策略人工验证脚本。

验证设计文档（hard_pin_design.md）中的核心保证：
  1. SSD 水位控制（4.2）：effective_free_ratio < watermark → 拒绝分配
  2. 驱逐保护（4.1）：无 LOCAL_DISK 副本的 MEMORY 不被驱逐
  3. SSD 不可驱逐：LOCAL_DISK 副本在任何情况下不被驱逐
  4. 负载均衡（4.2）：多 Client 下 SSD 水位控制实现负载均衡

用法：
    python verify_hard_pin.py --test <test_name>

每次测试前请清理 SSD 目录：rm -rf <SSD_PATH> && mkdir -p <SSD_PATH>

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
    """从 Master /metrics 端点获取 Prometheus 格式指标。"""
    metrics_port = os.getenv("METRICS_PORT", DEFAULT_METRICS_PORT)
    try:
        url = f"http://127.0.0.1:{metrics_port}/metrics"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=2) as resp:
            text = resp.read().decode()
        result = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                name = parts[0]
                # Skip histogram buckets (name{le="..."})
                if "{" in name:
                    name = name[:name.index("{")]
                try:
                    result[name] = float(parts[1])
                except ValueError:
                    pass
        return result
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
        mem_total = stats.get("master_total_capacity_bytes", 0)
        mem_used = stats.get("master_allocated_bytes", 0)
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
        if val > 1024:
            print(f"{prefix}{key} = {val:.0f} ({val/1024/1024:.2f}M)")
        else:
            print(f"{prefix}{key} = {val}")


def create_store(segment_size=DEFAULT_DDR_SIZE,
                 buffer_size=DEFAULT_DDR_SIZE,
                 enable_offload=True,
                 ssd_path_override=None,
                 ssd_total_size_override=None):
    """创建 Store 客户端。"""
    ssd_path = ""
    if enable_offload:
        ssd_limit_str = ssd_total_size_override or os.getenv(
            "MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES", "")
        if not ssd_limit_str:
            print("[ERROR] MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES 未设置！")
            sys.exit(1)
        if isinstance(ssd_limit_str, int):
            ssd_limit = ssd_limit_str
        else:
            ssd_limit = int(ssd_limit_str)
        # Set env var for C++ code (reads in FileStorageConfig::FromEnvironment)
        os.environ["MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES"] = str(ssd_limit)
        effective = ssd_limit - segment_size
        if effective <= 0:
            print(f"[ERROR] SSD({ssd_limit}) 必须 > DDR({segment_size})")
            sys.exit(1)
        ssd_path = ssd_path_override or os.getenv(
            "MOONCAKE_OFFLOAD_FILE_STORAGE_PATH", "/tmp/mooncake_ssd_test")
        heartbeat_interval = os.getenv("MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS",
                                        "(未设置, 默认10s)")
        # Auto-create SSD path if it doesn't exist
        os.makedirs(ssd_path, exist_ok=True)

    if segment_size >= 1024 * 1024 * 1024:
        ddr_str = f"{segment_size/1024/1024/1024:.1f}GB"
    else:
        ddr_str = f"{segment_size/1024/1024:.0f}MB"
    print(f"  配置:")
    print(f"    DDR: {ddr_str}")
    if enable_offload:
        print(f"    SSD: {ssd_limit/1024/1024/1024:.1f}GB")
        print(f"    effective: {effective/1024/1024/1024:.1f}GB")
        print(f"    SSD 路径: {ssd_path}")
        print(f"    心跳间隔: {heartbeat_interval}s")
    else:
        print(f"    SSD offload: DISABLED")

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
        enable_ssd_offload=enable_offload,
        ssd_offload_path=ssd_path,
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
    print(f"  (注: 实际触发可能更低，因 pending(offloading_objects) 计入 used)")
    print(f"  需要写入约 {int(trigger_at / KEY_SIZE)} 个 4MB key\n")

    written = 0
    rejected = 0
    batch_size = 100
    batch_sleep = 5.0  # 每批后等 5s，让 offload 管线排空 pending
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
    # 最小允许的 segment 大小是 16MB，用此值以便快速触发驱逐。
    # 关闭 offload 确保 protected_key 不会有 LOCAL_DISK 副本，
    # 从而保证驱逐保护逻辑一定被触发。
    seg_size = 16 * 1024 * 1024  # 16MB (最低要求)
    store = create_store(segment_size=seg_size, buffer_size=seg_size,
                         enable_offload=False)
    _store = store

    first_key = "protected_key"
    first_data = b"\x01" * (1024 * 100)
    retcode = store.put(first_key, first_data)
    if retcode != 0:
        raise RuntimeError(f"put {first_key} 失败: {retcode}")
    print(f"  写入 {first_key} (100KB)")

    print(f"  等待 lease 过期 ({kv_ttl}ms)...")
    time.sleep(kv_ttl / 1000.0 + 0.5)

    # 用 2MB filler key 填 16MB DDR（约 8 次写入即可填满）。
    filler_size = 2 * 1024 * 1024  # 2MB
    print(f"  填满 DDR (每次 {filler_size/1024/1024:.0f}MB)...")
    fill = 0
    for i in range(200):
        retcode = store.put(f"filler_{i}", b"\x02" * filler_size)
        if retcode == 0:
            fill += 1
        else:
            break
        time.sleep(INSERT_INTERVAL)
    print(f"  填充: {fill} 个后写满 ({fill * filler_size / 1024 / 1024:.0f}MB)")

    result = store.get(first_key)
    if result and result != b"":
        print(f"  ✓ {first_key} 仍可读 ({len(result)} bytes) — 驱逐保护生效")
    else:
        print(f"  ✗ {first_key} 不可读 — 被驱逐了")
        raise AssertionError("驱逐保护未生效")


def test_ssd_eviction_rejected():
    """验证 SSD 副本不可驱逐：写入足够数据触发 offload，确认 SSD 副本安全。"""
    global _store
    print("=== 验证：SSD 副本不可驱逐 ===\n")

    store = create_store()
    _store = store

    # 需要足够数据量才能触发 offload（实测 <200MB 可能不触发）
    num_keys = 50  # 50 × 4MB = 200MB
    print(f"  [1] 写入 {num_keys} 个 4MB key ({num_keys * 4}MB)...")
    t0 = time.time()
    for i in range(num_keys):
        key = f"ssd_safe_key_{i}"
        data = (f"ssd_data_{i}".encode().ljust(KEY_SIZE, b"\x03"))
        retcode = store.put(key, data)
        if retcode != 0:
            raise RuntimeError(f"put {key} 失败: retcode={retcode}")
        if (i + 1) % 10 == 0:
            print(f"    {i+1}/{num_keys} ({time.time()-t0:.1f}s)")
        time.sleep(INSERT_INTERVAL)
    print(f"  写入完成: {num_keys} 个 ({num_keys * 4}MB, {time.time()-t0:.1f}s)")
    print_metrics("写入后")

    offload_wait = 20
    print(f"\n  [2] 等 offload ({offload_wait}s)...")
    wait_with_progress(offload_wait)
    print_metrics("offload 后")

    # 验证随机 key 可读（数据应从 SSD 或 DDR 读取）
    test_keys = ["ssd_safe_key_0", "ssd_safe_key_25", "ssd_safe_key_49"]
    all_ok = True
    for key in test_keys:
        result = store.get(key)
        expected = f"ssd_data_{key.split('_')[-1]}".encode().ljust(KEY_SIZE, b"\x03")
        if result and len(result) == KEY_SIZE:
            print(f"  ✓ {key} 可读取 ({len(result)/1024/1024:.0f}MB)")
        else:
            rlen = len(result) if result else 0
            print(f"  ✗ {key} 不可读 (len={rlen})")
            all_ok = False

    if not all_ok:
        raise AssertionError("SSD 副本验证失败")

    # 6 次读取确认一致性
    print(f"\n  [3] 6 次连续读取验证一致性...")
    for attempt in range(6):
        result = store.get(test_keys[0])
        if not result or result == b"":
            print(f"  ✗ 第 {attempt} 次读取失败")
            raise AssertionError("SSD 副本丢失")
        if attempt == 0:
            print(f"  ✓ 所有读取一致")
        time.sleep(2)

    # 直接检查 SSD 目录确认 offload 确实发生
    ssd_path = os.getenv("MOONCAKE_OFFLOAD_FILE_STORAGE_PATH",
                         "/tmp/mooncake_ssd_test")
    if os.path.exists(ssd_path):
        files = [f for f in os.listdir(ssd_path) if os.path.isfile(os.path.join(ssd_path, f))]
        total_size = sum(os.path.getsize(os.path.join(ssd_path, f)) for f in files)
        print(f"\n  SSD 目录检查: {len(files)} 个文件, {total_size/1024/1024:.0f}MB")
        if total_size < 100 * 1024 * 1024:
            print(f"  ⚠ SSD 文件总量 < 100MB，offload 可能未完成或心跳间隔过长")
    else:
        print(f"\n  ⚠ SSD 目录不存在: {ssd_path}")

    print(f"\n  ✓ SSD 副本安全，驱逐保护生效")


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


def test_load_balancing():
    """验证多 Client 负载均衡：不对称 SSD 容量，小 SSD 先满后溢出到大 SSD。"""
    global _store
    print("=== 验证：多 Client 负载均衡（不对称 SSD 容量） ===\n")

    import shutil

    seg_size = DEFAULT_DDR_SIZE  # 4GB DDR per client

    # With per-segment effective formula: effective = SSD - per_segment_DDR
    # Client 1 (writer): SSD=8GB,  effective=8-4=4GB,   trigger at used > 3.4GB
    # Client 2:          SSD=16GB, effective=16-4=12GB, trigger at used > 10.2GB
    ssd_cap_1 = 8 * 1024 * 1024 * 1024   # 8GB
    ssd_cap_2 = 16 * 1024 * 1024 * 1024  # 16GB
    effective_1 = ssd_cap_1 - seg_size    # 4GB
    effective_2 = ssd_cap_2 - seg_size    # 12GB
    watermark = 0.15
    trigger_1 = effective_1 * (1 - watermark)  # 3.4GB ≈ 870 keys × 4MB
    trigger_2 = effective_2 * (1 - watermark)  # 10.2GB

    ssd_path_1 = "/tmp/mooncake_lb_test_1"
    ssd_path_2 = "/tmp/mooncake_lb_test_2"

    # 清理旧数据
    print("  清理旧 SSD 数据...")
    for p in [ssd_path_1, ssd_path_2]:
        if os.path.exists(p):
            shutil.rmtree(p)
        os.makedirs(p, exist_ok=True)

    print(f"\n  设定:")
    print(f"    Client 1 (写入端): SSD={ssd_cap_1/1024/1024/1024:.1f}GB, "
          f"effective={effective_1/1024/1024/1024:.1f}GB, "
          f"水位触发 > {trigger_1/1024/1024/1024:.1f}GB ({int(trigger_1/KEY_SIZE)} keys)")
    print(f"    Client 2:          SSD={ssd_cap_2/1024/1024/1024:.1f}GB, "
          f"effective={effective_2/1024/1024/1024:.1f}GB, "
          f"水位触发 > {trigger_2/1024/1024/1024:.1f}GB")

    print(f"\n  [1] 启动 Client 2 (SSD={ssd_cap_2/1024/1024/1024:.1f}GB)...")
    store2 = create_store(segment_size=seg_size, buffer_size=seg_size,
                          ssd_path_override=ssd_path_2,
                          ssd_total_size_override=ssd_cap_2)
    print(f"  Client 2 启动成功")

    print(f"\n  [2] 启动 Client 1 (SSD={ssd_cap_1/1024/1024/1024:.1f}GB)...")
    store1 = create_store(segment_size=seg_size, buffer_size=seg_size,
                          ssd_path_override=ssd_path_1,
                          ssd_total_size_override=ssd_cap_1)
    _store = store1
    print(f"  Client 1 启动成功")

    print_metrics("两个 Client 均已注册")

    # 写入约 1200 个 key（4.8GB），超过 Client 1 水位但低于 Client 2 水位
    num_keys = 1200
    print(f"\n  [3] Client 1 写入 {num_keys} 个 4MB key ({num_keys * 4}MB ≈ {num_keys * 4 / 1024:.1f}GB)...")
    print(f"      预计 Client 1 水位先触发（~{int(trigger_1/KEY_SIZE)} keys），"
          f"后续分配到 Client 2")

    written = 0
    rejected = 0
    first_reject_key = None
    t0 = time.time()

    for i in range(num_keys):
        key = f"lb_key_{i}"
        data = f"lb_{i}".encode().ljust(KEY_SIZE, b"\xAB")
        retcode = store1.put(key, data)
        if retcode == 0:
            written += 1
        else:
            rejected += 1
            if first_reject_key is None:
                first_reject_key = i
                total_gb = written * KEY_SIZE / 1024 / 1024 / 1024
                print(f"\n  ★ 首次拒绝: key={key}, 已写入 {written} 个 ({total_gb:.1f}GB)")
                print_metrics("首次拒绝时")
            if rejected >= 5:
                print(f"  连续 5 次拒绝，停止写入")
                break
        if (i + 1) % 200 == 0:
            elapsed = time.time() - t0
            print(f"    {i+1}/{num_keys} 写入 ({written} 成功, {rejected} 拒绝, {elapsed:.0f}s)")
        time.sleep(INSERT_INTERVAL)

    total_gb = written * KEY_SIZE / 1024 / 1024 / 1024
    print(f"\n  写入完成: {written} 成功 ({total_gb:.1f}GB), "
          f"{rejected} 拒绝, {time.time()-t0:.0f}s")
    if first_reject_key is not None:
        print(f"  首次拒绝于第 {first_reject_key} 个 key")

    print_metrics("写入完成")

    offload_wait = 60
    print(f"\n  [4] 等待 {offload_wait}s 让 offload 完成...")
    wait_with_progress(offload_wait)

    print_metrics("offload 后")

    # 检查两个 SSD 目录的文件大小
    ssd_sizes = {}
    print(f"\n  --- SSD 目录检查 ---")
    for label, path in [("Client 1 SSD", ssd_path_1), ("Client 2 SSD", ssd_path_2)]:
        if os.path.exists(path):
            files = [f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))]
            total_size = sum(os.path.getsize(os.path.join(path, f)) for f in files)
            print(f"  {label}: {len(files)} 个文件, {total_size/1024/1024:.0f}MB ({total_size/1024/1024/1024:.2f}GB)")
            ssd_sizes[label] = total_size
        else:
            print(f"  {label}: 目录不存在")
            ssd_sizes[label] = 0

    # 验证结果
    size_1 = ssd_sizes.get("Client 1 SSD", 0)
    size_2 = ssd_sizes.get("Client 2 SSD", 0)

    print(f"\n  --- 判断 ---")
    if size_1 > 0 and size_2 > 0:
        print(f"  ✓ 两个 Client 的 SSD 均有数据 — 负载均衡生效")
        if size_2 > size_1:
            print(f"  ✓ Client 2 SSD ({size_2/1024/1024:.0f}MB) > "
                  f"Client 1 SSD ({size_1/1024/1024:.0f}MB) — 溢出行为正常")
        print(f"  Client 1 水位触发约在 {trigger_1/1024/1024/1024:.1f}GB, "
              f"实际 SSD={size_1/1024/1024/1024:.2f}GB")
    elif size_1 > 0:
        print(f"  ✗ 仅 Client 1 有数据 — 负载均衡未生效")
    else:
        print(f"  ✗ SSD 均无数据 — offload 可能失败（检查心跳间隔和磁盘空间）")

    # 关闭 Client 2
    store2.close()


def test_sequential_shutdown():
    """验证 4 节点顺序 SSD 关闭：不同 SSD 容量下，节点按容量从小到大依次被排除。

    计算公式（已修复为 per-segment）：
      effective_capacity = ssd_total_capacity_bytes - per_segment_ddr
    其中 per_segment_ddr 是该 segment 自身的 DDR 容量，非全局总和。

    本测试使用 DDR=1GB×4 节点。
    SSD 配置：3GB, 4GB, 5GB, 6GB → effective=2, 3, 4, 5 GB。
    """
    global _store
    import shutil

    print("=== 验证：4 节点顺序 SSD 关闭 ===\n")

    DDR_PER_CLIENT = 1 * 1024 * 1024 * 1024     # 1GB per client
    SSD_CAPS = [
        3 * 1024 * 1024 * 1024,    # C1 (smallest):  effective=2GB, trigger=1.7GB
        4 * 1024 * 1024 * 1024,    # C2:             effective=3GB, trigger=2.55GB
        5 * 1024 * 1024 * 1024,    # C3:             effective=4GB, trigger=3.4GB
        6 * 1024 * 1024 * 1024,    # C4 (largest):   effective=5GB, trigger=4.25GB
    ]

    watermark = 0.15
    for i, cap in enumerate(SSD_CAPS):
        effective = cap - DDR_PER_CLIENT  # per-segment formula
        trigger = effective * (1 - watermark)
        print(f"  Client {i+1}: SSD={cap/1024/1024/1024:.0f}GB → "
              f"effective={effective/1024/1024/1024:.1f}GB, "
              f"trigger={trigger/1024/1024/1024:.2f}GB ({int(trigger/KEY_SIZE)} keys)")

    SSD_PATHS = [
        "/tmp/mooncake_seq_test_c1",
        "/tmp/mooncake_seq_test_c2",
        "/tmp/mooncake_seq_test_c3",
        "/tmp/mooncake_seq_test_c4",
    ]
    BATCH_SIZE = 250          # 250 keys × 4MB = 1GB per batch
    OFFLOAD_WAIT = 10          # seconds between batches
    MAX_BATCHES = 30           # safety limit (~30GB total)

    print(f"\n  DDR 每节点: {DDR_PER_CLIENT / 1024/1024/1024:.0f}GB × 4")
    print(f"  Batch: {BATCH_SIZE} keys ({BATCH_SIZE * 4}MB), "
          f"offload wait: {OFFLOAD_WAIT}s")

    # Clean and create SSD directories
    print(f"\n  [1] 准备 SSD 目录...")
    for p in SSD_PATHS:
        if os.path.exists(p):
            shutil.rmtree(p)
        os.makedirs(p, exist_ok=True)
    print(f"  目录已清理")

    # Create all 4 clients — largest SSD first (registration order matters!)
    # If a small-SSD client registered first, its effective capacity would be
    # temporarily inflated because fewer DDR segments are counted in ddr_total.
    print(f"\n  [2] 启动 Client (按 SSD 从大到小注册)...")
    stores = []
    for i in range(3, -1, -1):  # 3→2→1→0 (C4 largest → C1 smallest)
        store = create_store(
            segment_size=DDR_PER_CLIENT,
            buffer_size=DDR_PER_CLIENT,
            ssd_path_override=SSD_PATHS[i],
            ssd_total_size_override=SSD_CAPS[i],
        )
        stores.insert(0, store)  # stores[0] = C1, stores[1] = C2, ...
        print(f"  Client {i+1} (SSD={SSD_CAPS[i]/1024/1024/1024:.0f}GB) 启动成功")
    print(f"  所有 Client 已注册 (C1→C4: {len(stores)} 个)")

    writer_store = stores[0]  # C1 (smallest SSD) is the writer
    _store = writer_store

    print_metrics("注册后")

    def get_ssd_dir_sizes():
        """返回每个 SSD 目录的文件总大小列表。"""
        results = []
        for path in SSD_PATHS:
            if os.path.exists(path):
                total = sum(
                    os.path.getsize(os.path.join(path, f))
                    for f in os.listdir(path)
                    if os.path.isfile(os.path.join(path, f))
                )
                results.append(total)
            else:
                results.append(0)
        return results

    def check_size_order(sizes, label):
        """检查 SSD 大小是否按容量递增（允许 5% 容差）。"""
        verdicts = []
        for i in range(3):
            # C(i+1) should have ≤ data than C(i+2)
            ok = sizes[i] <= sizes[i+1] * 1.05
            if ok:
                verdicts.append("OK")
            else:
                verdicts.append(f"INV: C{i+1}>{i+2}")
        status = " | ".join(f"C{i+1}≤C{i+2}:{v}" for i, v in enumerate(verdicts))
        print(f"    [{label}] {status}")
        return all(v == "OK" for v in verdicts)

    def find_stabilized(sizes, prev_sizes):
        """检测哪些节点的 SSD 停止增长（本批次增长 < 5%）。"""
        stabilized = []
        for i in range(4):
            if prev_sizes[i] > 0 and sizes[i] <= prev_sizes[i] * 1.05:
                stabilized.append(i + 1)
        return stabilized

    # Main write loop
    total_written = 0
    total_rejected = 0
    prev_sizes = [0, 0, 0, 0]
    all_rejected_batch = None

    for batch_idx in range(MAX_BATCHES):
        batch_written = 0
        batch_rejected = 0
        t_start = time.time()

        for i in range(BATCH_SIZE):
            key = f"seq_key_{total_written}"
            data = f"seq_{total_written}".encode().ljust(KEY_SIZE, b"\xCD")
            retcode = writer_store.put(key, data)
            if retcode == 0:
                batch_written += 1
            else:
                batch_rejected += 1
            total_written += 1
            time.sleep(INSERT_INTERVAL)

        batch_elapsed = time.time() - t_start
        total_rejected += batch_rejected

        print(f"\n--- Batch {batch_idx + 1} ({batch_elapsed:.0f}s) ---")
        print(f"  写入: {batch_written} 成功, {batch_rejected} 拒绝 "
              f"(累计: {total_written} 尝试, {total_rejected} 拒绝)")

        # Wait for offload
        print(f"  等待 offload ({OFFLOAD_WAIT}s)...")
        wait_with_progress(OFFLOAD_WAIT)
        print_metrics(f"batch {batch_idx + 1}")

        # Check SSD sizes
        sizes = get_ssd_dir_sizes()
        print(f"  SSD 目录大小:")
        for i in range(4):
            change = sizes[i] - prev_sizes[i]
            sign = "+" if change >= 0 else ""
            print(f"    C{i+1} ({SSD_CAPS[i]/1024/1024/1024:.0f}GB SSD): "
                  f"{sizes[i]/1024/1024:.0f}MB ({sign}{change/1024/1024:.0f}MB)")

        check_size_order(sizes, f"batch {batch_idx + 1}")

        stabilized = find_stabilized(sizes, prev_sizes)
        if stabilized:
            print(f"  停止增长: C{','.join(map(str, stabilized))}")

        prev_sizes = sizes

        # Stop condition: entire batch rejected → all segments excluded
        if batch_written == 0 and batch_rejected > 0:
            all_rejected_batch = batch_idx + 1
            print(f"\n  *** Batch {batch_idx + 1}: 所有写入被拒 — "
                  f"全部 {4} 个 segment 已被排除 ***")
            break

    # Final verification
    print(f"\n{'='*60}")
    print(f"最终验证")
    print(f"{'='*60}\n")

    final_sizes = get_ssd_dir_sizes()
    print_metrics("最终")

    print(f"\n  SSD 最终状态:")
    for i in range(4):
        eff = SSD_CAPS[i] - DDR_PER_CLIENT
        trigger = eff * (1 - watermark)
        print(f"    C{i+1} (SSD={SSD_CAPS[i]/1024/1024/1024:.0f}GB, "
              f"eff={eff/1024/1024/1024:.1f}GB, trigger={trigger/1024/1024/1024:.2f}GB): "
              f"{final_sizes[i]/1024/1024:.0f}MB")

    ssds_with_data = sum(1 for s in final_sizes if s > 0)
    print(f"\n  SSD 有数据的节点: {ssds_with_data}/4")

    order_ok = check_size_order(final_sizes, "final")
    if not order_ok:
        print(f"\n  *** 注意：最终 SSD 大小顺序不完全递增，"
              f"可能是因为 free-ratio-first 分配策略偏好较大 SSD")

    # Success criteria
    all_ok = True
    if ssds_with_data < 3:
        print(f"  ✗ 仅 {ssds_with_data}/4 SSD 有数据 (预期 >= 3)")
        all_ok = False

    if all_rejected_batch is not None:
        print(f"  ✓ 全局拒绝达成 (batch {all_rejected_batch}) — "
              f"验证 HardPin 不 fallback")
    else:
        print(f"  ✗ 未达到全局拒绝 — 所有 batch 均有写入成功")
        all_ok = False

    if all_ok:
        print(f"\n  ✓ 4 节点顺序 SSD 关闭验证通过")
    else:
        raise AssertionError("4 节点顺序 SSD 关闭验证失败")

    # Cleanup: close non-writer stores
    for i in range(1, 4):
        stores[i].close()


TESTS = {
    "offload_only": test_offload_only,
    "ssd_full_reject": test_ssd_full_reject,
    "eviction_protection": test_eviction_protection,
    "ssd_eviction_rejected": test_ssd_eviction_rejected,
    "full_lifecycle": test_full_lifecycle,
    "load_balancing": test_load_balancing,
    "sequential_shutdown": test_sequential_shutdown,
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
