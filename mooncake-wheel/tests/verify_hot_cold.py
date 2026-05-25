#!/usr/bin/env python3
"""冷热交换实验：验证 offload（冷降级）+ promotion-on-hit（热晋升）。

原理：
  写入 → offload → SSD（冷数据降级）
  重复 GET SSD-only key → CountMinSketch 超阈值 → promotion → DDR（热数据晋升）

前提：
  - Master 需启用 --promotion_on_hit=true --promotion_admission_threshold=2
  - 本脚本需在 main 分支运行（promotion-on-hit 仅在 main 分支）

用法：
  MC_METADATA_SERVER=http://127.0.0.1:8880/metadata \
  MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=8589934592 \
  MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1 \
  MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=/tmp/mooncake_hotcold_test \
  python verify_hot_cold.py
"""

import os
import sys
import time
import urllib.request

from mooncake.store import MooncakeDistributedStore

DEFAULT_MASTER_PORT = "50053"
DEFAULT_METADATA_PORT = "8880"
DEFAULT_METRICS_PORT = "9104"

# 规模：DDR=256MB, SSD=8GB, 200 keys × 4MB = 800MB
DDR_SIZE = 256 * 1024 * 1024       # 256MB
SSD_SIZE = 8 * 1024 * 1024 * 1024  # 8GB
KEY_SIZE = 4 * 1024 * 1024         # 4MB
NUM_KEYS = 200                      # 200 keys = 800MB >> 256MB DDR
NUM_HOT_KEYS = 5                    # 5 个 key 做热访问
PROMOTION_THRESHOLD = 2             # CountMinSketch 频率门槛
PROMOTION_GETS = 4                  # 每个热 key GET 次数 (>threshold)
INSERT_INTERVAL = 0.01

METADATA_SERVER = os.getenv("MC_METADATA_SERVER",
                            f"http://127.0.0.1:{DEFAULT_METADATA_PORT}/metadata")
MASTER_SERVER = os.getenv("MASTER_SERVER", f"127.0.0.1:{DEFAULT_MASTER_PORT}")
METRICS_PORT = os.getenv("METRICS_PORT", DEFAULT_METRICS_PORT)
SSD_PATH = os.getenv("MOONCAKE_OFFLOAD_FILE_STORAGE_PATH",
                     "/tmp/mooncake_hotcold_test")


def fetch_metrics():
    try:
        url = f"http://127.0.0.1:{METRICS_PORT}/metrics"
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
                if "{" in name:
                    name = name[:name.index("{")]
                try:
                    result[name] = float(parts[1])
                except ValueError:
                    pass
        return result
    except Exception:
        return None


def print_state(phase):
    """打印 DDR/SSD/Key 数量状态。"""
    stats = fetch_metrics()
    prefix = f"  [{phase}] "
    if not stats:
        print(f"{prefix}(metrics 不可用)")
        return
    mem_total = stats.get("master_total_capacity_bytes", 0)
    mem_used = stats.get("master_allocated_bytes", 0)
    ssd_total = stats.get("master_total_file_capacity_bytes", 0)
    ssd_used = stats.get("master_allocated_file_size_bytes", 0)
    evict_success = stats.get("master_eviction_success_total", 0)
    if mem_total > 0:
        print(f"{prefix}DDR: {mem_used/1024/1024:.0f}M / {mem_total/1024/1024:.0f}M "
              f"({mem_used/mem_total*100:.1f}%)")
    if ssd_total > 0 and ssd_total < 10**15:
        print(f"{prefix}SSD: {ssd_used/1024/1024:.0f}M / {ssd_total/1024/1024:.0f}M "
              f"({ssd_used/ssd_total*100:.1f}%)")
    if evict_success > 0:
        print(f"{prefix}驱逐成功次数: {evict_success:.0f}")

    # SSD 目录文件大小
    if os.path.exists(SSD_PATH):
        files = [f for f in os.listdir(SSD_PATH) if os.path.isfile(os.path.join(SSD_PATH, f))]
        total = sum(os.path.getsize(os.path.join(SSD_PATH, f)) for f in files)
        print(f"{prefix}SSD 目录文件: {len(files)} 个, {total/1024/1024:.0f}M")


def wait_with_progress(seconds, label=""):
    for t in range(seconds):
        if t % 5 == 0:
            print_state(f"{label}{t}s")
        sys.stdout.write(f"\r  {label}{t+1}/{seconds}s")
        sys.stdout.flush()
        time.sleep(1)
    print()


def main():
    # 清理
    import shutil
    if os.path.exists(SSD_PATH):
        shutil.rmtree(SSD_PATH)
    os.makedirs(SSD_PATH, exist_ok=True)

    print("=" * 60)
    print("冷热交换实验: offload (冷降级) + promotion-on-hit (热晋升)")
    print("=" * 60)
    print(f"\n  规模: DDR={DDR_SIZE/1024/1024:.0f}MB, SSD={SSD_SIZE/1024/1024/1024:.0f}GB, "
          f"{NUM_KEYS} keys × {KEY_SIZE/1024/1024:.0f}MB = {NUM_KEYS * KEY_SIZE/1024/1024:.0f}MB")
    print(f"  热 key: {NUM_HOT_KEYS} 个, 每个 GET {PROMOTION_GETS} 次 "
          f"(>频率门槛 {PROMOTION_THRESHOLD})")
    print(f"  promotion_on_hit=true, admission_threshold={PROMOTION_THRESHOLD}")
    print()

    # ── 创建 Store ──
    print("[Setup] 创建 Store Client...")
    os.environ["MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES"] = str(SSD_SIZE)
    store = MooncakeDistributedStore()
    retcode = store.setup(
        os.getenv("LOCAL_HOSTNAME", "127.0.0.1"),
        METADATA_SERVER, DDR_SIZE, DDR_SIZE,
        os.getenv("PROTOCOL", "tcp"), os.getenv("DEVICE_NAME", "eth0"),
        MASTER_SERVER,
        enable_ssd_offload=True, ssd_offload_path=SSD_PATH,
    )
    if retcode != 0:
        print(f"[ERROR] Store setup 失败: retcode={retcode}")
        sys.exit(1)
    print("  Store setup 成功\n")

    # ═══════════════════════════════════════════════════════════
    # Phase 1: 写入 200 个 key (800MB)
    # ═══════════════════════════════════════════════════════════
    print("=" * 60)
    print("Phase 1: 写入数据 (DDR → offload pending)")
    print("=" * 60)

    hot_key_indices = [0, 10, 20, 30, 40]  # 选 5 个作为热 key
    hot_keys = [f"hot_key_{i}" for i in hot_key_indices]

    t0 = time.time()
    for i in range(NUM_KEYS):
        if i in hot_key_indices:
            key = f"hot_key_{i}"
        else:
            key = f"cold_key_{i}"
        data = f"key_{i}".encode().ljust(KEY_SIZE, b"\x00")
        retcode = store.put(key, data)
        if retcode != 0:
            print(f"  ✗ put {key} 失败: retcode={retcode}")
            sys.exit(1)
        if (i + 1) % 50 == 0:
            print(f"    {i+1}/{NUM_KEYS} ({(i+1)*4}MB, {time.time()-t0:.0f}s)")
        time.sleep(INSERT_INTERVAL)

    total_mb = NUM_KEYS * KEY_SIZE / 1024 / 1024
    print(f"  写入完成: {NUM_KEYS} 个 ({total_mb:.0f}MB, {time.time()-t0:.0f}s)")
    print_state("写入完成")

    # ═══════════════════════════════════════════════════════════
    # Phase 2: 等待 offload + 驱逐 (冷数据降级)
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("Phase 2: 等待 offload + 驱逐 (冷数据降级 DDR→SSD)")
    print("=" * 60)
    print(f"  800MB > {DDR_SIZE/1024/1024:.0f}MB DDR → 驱逐必然发生")
    print(f"  心跳=1s, lease 立即可过期 → offload+驱逐 15s 足够")
    print(f"  (每次心跳搬一批, 驱逐线程 10ms 一检)")

    wait_with_progress(20, "等待 ")

    print("\n  Phase 2 完成 — 预期: DDR≈0, SSD≈800MB (全部冷数据)")
    print_state("Phase2完成")

    # ═══════════════════════════════════════════════════════════
    # Phase 3: 冷读 — 不触发 promotion
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("Phase 3: 冷读验证 (每个 cold key 读 1 次, 不触发 promotion)")
    print("=" * 60)

    cold_sample = [f"cold_key_{i}" for i in [1, 50, 100, 150, 199]]
    cold_ok = 0
    for key in cold_sample:
        result = store.get(key)
        if result and len(result) == KEY_SIZE:
            cold_ok += 1
            print(f"  ✓ {key} 从 SSD 读取成功 ({len(result)/1024/1024:.0f}MB)")
        else:
            rlen = len(result) if result else 0
            print(f"  ✗ {key} 读取失败 (len={rlen})")
        time.sleep(0.1)
    print(f"  冷读通过: {cold_ok}/{len(cold_sample)} (DDR 中无副本, 全从 SSD 读)")
    print_state("Phase3完成")

    # ═══════════════════════════════════════════════════════════
    # Phase 4: 热读 — 触发 promotion-on-hit
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("Phase 4: 热读触发 (重复 GET 热 key, 触发 promotion)")
    print("=" * 60)
    print(f"  热 key: {hot_keys}")
    print(f"  每个 GET {PROMOTION_GETS} 次 (>频率门槛 {PROMOTION_THRESHOLD})")
    print(f"  每次 GET 调用 GetReplicaList → CountMinSketch.inc →")
    print(f"  频率 >= {PROMOTION_THRESHOLD} → TryPushPromotionQueue →")
    print(f"  client heartbeat → SSD→staging→DDR → NotifyPromotionSuccess")

    for key in hot_keys:
        print(f"\n  >>> 热 key: {key}")
        for attempt in range(PROMOTION_GETS):
            t1 = time.time()
            result = store.get(key)
            elapsed_ms = (time.time() - t1) * 1000
            ok = "✓" if (result and len(result) == KEY_SIZE) else "✗"
            freq_note = ""
            if attempt == PROMOTION_THRESHOLD - 1:
                freq_note = f" ← 频率达到阈值 {PROMOTION_THRESHOLD}"
            if attempt >= PROMOTION_THRESHOLD:
                freq_note = " (promotion 已触发)"
            print(f"    GET #{attempt+1}: {ok} {elapsed_ms:.1f}ms{freq_note}")
            time.sleep(0.2)

    print(f"\n  等待 10s 让 promotion heartbeat 完成 SSD→DDR 搬运...")
    wait_with_progress(10, "promotion ")

    print("\n  Phase 4 完成")
    print_state("Phase4完成")

    # ═══════════════════════════════════════════════════════════
    # Phase 5: 晋升验证
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("Phase 5: 晋升验证 (热 key 回到 DDR, 冷 key 仍在 SSD)")
    print("=" * 60)

    # 热 key 应从 DDR 读（延迟低）
    print("\n  --- 热 key 延迟 (应 < 1ms, DDR 命中) ---")
    hot_latencies = []
    for key in hot_keys:
        t1 = time.time()
        result = store.get(key)
        elapsed_us = (time.time() - t1) * 1_000_000
        ok = "✓" if (result and len(result) == KEY_SIZE) else "✗"
        print(f"    {key}: {ok} {elapsed_us:.0f}us")
        if result and len(result) == KEY_SIZE:
            hot_latencies.append(elapsed_us)

    # 冷 key 应从 SSD 读（延迟高）
    print("\n  --- 冷 key 延迟 (应 >100us, SSD 命中) ---")
    cold_latencies = []
    cold_test_keys = [f"cold_key_{i}" for i in [5, 55, 105, 155, 195]]
    for key in cold_test_keys:
        t1 = time.time()
        result = store.get(key)
        elapsed_us = (time.time() - t1) * 1_000_000
        ok = "✓" if (result and len(result) == KEY_SIZE) else "✗"
        print(f"    {key}: {ok} {elapsed_us:.0f}us")
        if result and len(result) == KEY_SIZE:
            cold_latencies.append(elapsed_us)

    # ── 判断 ──
    print("\n" + "=" * 60)
    print("实验结论")
    print("=" * 60)

    avg_hot = sum(hot_latencies) / len(hot_latencies) if hot_latencies else 0
    avg_cold = sum(cold_latencies) / len(cold_latencies) if cold_latencies else 0

    print(f"  热 key 平均延迟: {avg_hot:.0f}us ({len(hot_latencies)} keys)")
    print(f"  冷 key 平均延迟: {avg_cold:.0f}us ({len(cold_latencies)} keys)")

    stats = fetch_metrics()
    if stats:
        mem_used = stats.get("master_allocated_bytes", 0)
        ssd_used = stats.get("master_allocated_file_size_bytes", 0)
        print(f"  DDR 使用: {mem_used/1024/1024:.0f}MB")
        print(f"  SSD 使用: {ssd_used/1024/1024:.0f}MB")

    if hot_latencies and cold_latencies and avg_hot < avg_cold * 0.5:
        print(f"\n  ✓ 热 key 延迟显著低于冷 key — promotion 生效")
        print(f"  ✓ 冷热交换机制工作正常: 冷数据 SSD, 热数据 DDR")
    elif avg_hot > 0 and avg_cold > 0:
        print(f"\n  ⚠ 热冷延迟差距不够明显 (hot={avg_hot:.0f}us, cold={avg_cold:.0f}us)")
        print(f"    可能 promotion 尚未完成或 DDR 仍有缓存")
    else:
        print(f"\n  ✗ 读取失败 — 检查 Master 日志和 promotion_on_hit 开关")

    print(f"\n按回车退出...")
    try:
        input()
    except EOFError:
        pass
    store.close()


if __name__ == "__main__":
    main()
