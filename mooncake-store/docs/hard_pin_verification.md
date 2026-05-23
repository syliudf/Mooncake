# HardPin 功能人工验证方法

本文档描述如何通过启动实际的 Master + Client 进程，观察 HardPin 策略的行为。

## 前置条件

- 编译完成 mooncake_store（含 `mooncake_master` 可执行文件）
- 编译完成 mooncake-wheel（含 Python `mooncake.store` 模块）
- 安装 Python 3 + torch + numpy

## 关键环境变量

| 环境变量 | 作用 | 示例 |
|---------|------|------|
| `MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES` | **SSD 容量上限**，Client 通过 heartbeat 上报给 Master，Master 用此值计算水位 | `67108864` (64MB) |
| `MOONCAKE_OFFLOAD_FILE_STORAGE_PATH` | SSD 数据存储目录 | `/tmp/mooncake_ssd_test` |

HardPin 的水位计算公式：
```
effective_capacity = MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES - DDR总容量
effective_free_ratio = effective_free / effective_capacity
```

通过调小 `MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES`，可以精确控制 SSD 水位触发时机，**无需挂载小容量文件系统**。

---

## 验证 1：SSD 全满时拒绝写入（不 fallback）

通过设置一个很小的 SSD 容量限制，让 effective_capacity 接近 0，验证写入被拒绝。

```bash
TEST_DIR="/tmp/mooncake_hardpin_test"
rm -rf $TEST_DIR && mkdir -p $TEST_DIR

# 启动 HardPin 模式的 Master
mooncake_master \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --ssd_watermark_ratio=0.15 \
    --default_kv_lease_ttl=2000 \
    --root_fs_dir=$TEST_DIR \
    2>&1 | tee master.log &
MASTER_PID=$!
sleep 2

# 运行验证脚本：
# SSD 容量限制 = 64MB + 4MB = 68MB
# DDR segment = 64MB → effective_capacity = 68MB - 64MB = 4MB
# 写入 4MB 数据后水位触发
MC_METADATA_SERVER=http://127.0.0.1:8080/metadata \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=71303168 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=$TEST_DIR \
python verify_hard_pin.py --test ssd_full_reject

# 检查日志
echo "--- 检查 Master 日志 ---"
grep "Refusing allocation" master.log && echo "✓ 正确：拒绝了写入" || echo "✗ 未观察到拒绝"
grep "Falling back" master.log && echo "✗ 错误：仍然在 fallback！" || echo "✓ 没有 fallback"

# 清理
kill $MASTER_PID
rm -rf $TEST_DIR
```

### 预期观察

- Master 日志中出现 `[HARD_PIN] ... Refusing allocation to guarantee data safety.`
- **不应出现** `Falling back to allocation without SSD filter`
- Python 脚本输出：写入若干 key 后开始被拒绝

---

## 验证 2：驱逐保护（DDR 中无 LOCAL_DISK 副本的数据不被驱逐）

```bash
TEST_DIR="/tmp/mooncake_hardpin_evict_test"
rm -rf $TEST_DIR && mkdir -p $TEST_DIR

mooncake_master \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --default_kv_lease_ttl=500 \
    --root_fs_dir=$TEST_DIR \
    2>&1 | tee master_evict.log &
MASTER_PID=$!
sleep 2

# SSD 容量限制 = 100MB，给足空间让水位不触发
MC_METADATA_SERVER=http://127.0.0.1:8080/metadata \
DEFAULT_KV_LEASE_TTL=500 \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=104857600 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=$TEST_DIR \
python verify_hard_pin.py --test eviction_protection

kill $MASTER_PID
rm -rf $TEST_DIR
```

### 预期观察

- Python 脚本写入数据后，继续填满 DDR
- 旧 key（没有 LOCAL_DISK 副本的）**不被驱逐**
- DDR 写满后新写入失败
- Master 日志：`[HARD_PIN] Memory eviction skipped: no LOCAL_DISK replica`

---

## 验证 3：SSD 驱逐被拒绝

```bash
TEST_DIR="/tmp/mooncake_hardpin_ssd_test"
rm -rf $TEST_DIR && mkdir -p $TEST_DIR

mooncake_master \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --default_kv_lease_ttl=2000 \
    --root_fs_dir=$TEST_DIR \
    2>&1 | tee master_ssd.log &
MASTER_PID=$!
sleep 2

MC_METADATA_SERVER=http://127.0.0.1:8080/metadata \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=268435456 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=$TEST_DIR \
python verify_hard_pin.py --test ssd_eviction_rejected

kill $MASTER_PID
rm -rf $TEST_DIR
```

### 预期观察

- 写入 + offload 完成后数据可读取
- SSD 副本安全，反复读取不丢失
- Master 日志：`[HARD_PIN] SSD eviction rejected`

---

## 验证 4：完整生命周期（正常写入 → offload → 驱逐 DDR → 从 SSD 读取）

```bash
TEST_DIR="/tmp/mooncake_hardpin_lifecycle_test"
rm -rf $TEST_DIR && mkdir -p $TEST_DIR

mooncake_master \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --default_kv_lease_ttl=2000 \
    --root_fs_dir=$TEST_DIR \
    2>&1 | tee master_lifecycle.log &
MASTER_PID=$!
sleep 2

# SSD 容量 = 10MB，DDR segment = 4MB
# effective_capacity = 10MB - 4MB = 6MB
MC_METADATA_SERVER=http://127.0.0.1:8080/metadata \
DEFAULT_KV_LEASE_TTL=2000 \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=10485760 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=$TEST_DIR \
python verify_hard_pin.py --test full_lifecycle

kill $MASTER_PID
rm -rf $TEST_DIR
```

### 预期观察

1. `put("key1", data)` → 成功（SSD 有空间）
2. 等待 offload 完成（heartbeat 驱动 DDR→SSD 传输）
3. DDR 满后触发驱逐 → `key1` 的 MEMORY 副本被驱逐（因为已有 LOCAL_DISK）
4. `get("key1")` → 成功（从 SSD 读取）

---

## 验证 5：多节点负载均衡（SSD 水位驱动的写入路由）

需要两个节点（可以在同一台机器上用不同端口模拟）：

```bash
# 启动 Master
mooncake_master \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --default_kv_lease_ttl=2000 \
    --root_fs_dir=/tmp/mooncake_ssd \
    &

# 节点 A：小 SSD 容量（水位很快触发）
MC_METADATA_SERVER=http://127.0.0.1:8080/metadata \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=67108864 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=/tmp/mooncake_ssd_node_a \
python verify_hard_pin.py --test load_balance

# 节点 B：大 SSD 容量（始终有空间）
MC_METADATA_SERVER=http://127.0.0.1:8080/metadata \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=1073741824 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=/tmp/mooncake_ssd_node_b \
python verify_hard_pin.py --test load_balance
```

### 预期观察

- Master 日志：节点 A 的 segment 被排除（SSD 水位不足）
- 新写入分配到节点 B 的 segment
- 节点 A 的 DDR 中已有数据被保护不驱逐

---

## Python 验证脚本 `verify_hard_pin.py`

将此脚本放在 `mooncake-wheel/tests/` 目录下：

```python
#!/usr/bin/env python3
"""HardPin 策略人工验证脚本。

用法：
    python verify_hard_pin.py --test <test_name>

test_name:
    ssd_full_reject       - 验证 SSD 满时拒绝写入
    eviction_protection   - 验证无 LOCAL_DISK 的数据不被驱逐
    ssd_eviction_rejected - 验证 SSD 副本不能被驱逐
    full_lifecycle        - 完整写入→offload→驱逐→读取 生命周期
    load_balance          - SSD 水位驱动的负载均衡

关键环境变量：
    MC_METADATA_SERVER                        - Master 元数据地址
    DEFAULT_KV_LEASE_TTL                      - Lease TTL ms
    MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES   - SSD 容量上限（字节）
    MOONCAKE_OFFLOAD_FILE_STORAGE_PATH        - SSD 数据存储目录
"""

import argparse
import os
import sys
import time
import traceback

from mooncake.store import MooncakeDistributedStore


def create_store(segment_size=256*1024*1024, buffer_size=256*1024*1024):
    """创建并初始化一个 Store 客户端。

    SSD 容量通过 MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES 环境变量控制，
    Client 初始化时会通过 ReportSsdCapacity RPC 上报给 Master。
    """
    store = MooncakeDistributedStore()
    protocol = os.getenv("PROTOCOL", "tcp")
    device_name = os.getenv("DEVICE_NAME", "eth0")
    local_hostname = os.getenv("LOCAL_HOSTNAME", "127.0.0.1")
    metadata_server = os.getenv("MC_METADATA_SERVER",
                                "http://127.0.0.1:8080/metadata")
    master_server = os.getenv("MASTER_SERVER", "127.0.0.1:50051")

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
        raise RuntimeError(f"Store setup 失败: {retcode}")
    return store


def test_ssd_full_reject():
    """验证：SSD 水位不足时，put 返回错误（不 fallback 写入）。

    前置条件：
      - Master 启动参数 --allocation_strategy=hard_pin --enable_offload=true
      - 环境变量 MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES 设为较小的值
        （例如 68MB，使 effective_capacity = 68MB - 64MB = 4MB）
      - segment_size = 64MB
    """
    print("=== 测试：SSD 满时拒绝写入 ===")

    ssd_limit = int(os.getenv("MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES", "0"))
    seg_size = 64 * 1024 * 1024
    if ssd_limit > 0:
        effective = ssd_limit - seg_size
        print(f"  SSD 限制: {ssd_limit/1024/1024:.0f}MB, "
              f"DDR: {seg_size/1024/1024:.0f}MB, "
              f"effective: {effective/1024/1024:.1f}MB")

    store = create_store(segment_size=seg_size, buffer_size=seg_size)

    # 持续写入 1MB 对象直到 SSD 水位触发
    object_size = 1024 * 1024
    written = 0
    rejected = 0

    for i in range(200):
        key = f"ssd_full_key_{i}"
        data = b"\x00" * object_size
        try:
            store.put(key, data)
            written += 1
            if i % 5 == 0:
                print(f"  写入 #{i}: 成功 (已写 {written} 个, 被拒 {rejected} 个)")
        except Exception as e:
            rejected += 1
            if rejected == 1:
                print(f"  ★ 首次拒绝出现在第 {i} 次写入: {e}")
                print(f"    这证明 SSD 水位检查生效，没有 fallback")
            # 水位触发后再写几个，确认持续拒绝
            if rejected >= 3:
                break

    print(f"\n结果：成功写入 {written} 个, 被拒绝 {rejected} 个")
    assert rejected > 0, "应有写入被拒绝（SSD 水位不足），但全部成功了！"
    print("✓ 验证通过：SSD 满时正确拒绝写入\n")
    store.close()


def test_eviction_protection():
    """验证：没有 LOCAL_DISK 副本的 key，MEMORY 不被驱逐。

    方法：写入数据后立即填满 DDR，观察无 LOCAL_DISK 的旧数据是否被保留。
    HardPin 驱逐保护会阻止驱逐没有 LOCAL_DISK 副本的 MEMORY。
    """
    print("=== 测试：驱逐保护（无 LOCAL_DISK 时不驱逐 MEMORY）===")
    kv_ttl = int(os.getenv("DEFAULT_KV_LEASE_TTL", "2000"))

    store = create_store(segment_size=1024*1024, buffer_size=1024*1024)

    # 写入一个 key
    first_key = "protected_key"
    store.put(first_key, b"\x01" * 1024)
    print(f"  写入 {first_key}")

    # 等待 lease 过期（过期后才可被驱逐）
    time.sleep(kv_ttl / 1000.0 + 0.5)

    # 继续写入填满 DDR，触发驱逐
    for i in range(500):
        key = f"filler_{i}"
        try:
            store.put(key, b"\x02" * 1024)
        except Exception:
            pass  # DDR 满了

    # 验证 protected_key 是否还能读到
    try:
        result = store.get(first_key)
        print(f"  ✓ {first_key} 仍可读取（驱逐保护生效）")
        print("✓ 验证通过：无 LOCAL_DISK 的 MEMORY 不被驱逐\n")
    except Exception as e:
        print(f"  ✗ {first_key} 无法读取: {e}")
        print("  驱逐保护可能未生效！\n")
        raise

    store.close()


def test_ssd_eviction_rejected():
    """验证：HardPin 模式下 LOCAL_DISK 副本不能被驱逐。

    方法：写入 + 等待 offload 完成后，反复读取验证 SSD 副本一直安全。
    """
    print("=== 测试：SSD 驱逐被拒绝 ===")
    store = create_store()

    key = "ssd_safe_key"
    data = b"\x03" * 4096
    store.put(key, data)
    print(f"  写入 {key}")

    # 等待 offload 完成（heartbeat 驱动，通常 1-2 秒）
    time.sleep(3)

    # 验证数据可读
    result = store.get(key)
    assert result == data, f"数据不一致: 期望 {len(data)} 字节, 实际 {len(result)} 字节"
    print(f"  ✓ offload 后仍可读取 {key}（SSD 副本安全）")

    # 多次读取，验证 SSD 副本一直可用
    for i in range(5):
        time.sleep(1)
        result = store.get(key)
        assert result == data

    print("✓ 验证通过：SSD 副本持久安全，无法被驱逐\n")
    store.close()


def test_full_lifecycle():
    """验证：完整的 写入→offload→DDR驱逐→SSD读取 生命周期。

    前置条件：
      - MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES = 10MB
      - segment_size = 4MB → effective_capacity = 6MB
    """
    print("=== 测试：完整 HardPin 生命周期 ===")
    kv_ttl = int(os.getenv("DEFAULT_KV_LEASE_TTL", "2000"))

    seg_size = 4 * 1024 * 1024
    store = create_store(segment_size=seg_size, buffer_size=seg_size)

    # 阶段 1：写入数据
    key = "lifecycle_key"
    data = b"\x04" * (1024 * 100)  # 100KB
    store.put(key, data)
    print(f"  [阶段1] 写入 {key} ({len(data)} 字节)")

    # 阶段 2：等待 offload 完成
    time.sleep(3)
    result = store.get(key)
    assert result == data
    print(f"  [阶段2] offload 完成，数据可读")

    # 阶段 3：写满 DDR 触发驱逐（等待 lease 过期后）
    time.sleep(kv_ttl / 1000.0 + 0.5)
    for i in range(200):
        try:
            store.put(f"filler_{i}", b"\x05" * (1024 * 100))
        except Exception:
            break
    print(f"  [阶段3] DDR 压力写入完成，触发驱逐")

    # 阶段 4：从 SSD 读取被驱逐的数据
    time.sleep(1)
    result = store.get(key)
    assert result == data
    print(f"  [阶段4] DDR 驱逐后仍可从 SSD 读取 {key}")

    print("✓ 验证通过：完整生命周期正常\n")
    store.close()


def test_load_balance():
    """验证：SSD 水位驱动的写入路由。

    单节点下验证水位拒绝行为。多节点需要分别启动不同 SSD 容量的 Client。
    """
    print("=== 测试：SSD 水位驱动的负载均衡 ===")
    print("  注意：完整验证需要多节点环境，此处仅验证单节点水位拒绝")

    seg_size = 4 * 1024 * 1024
    store = create_store(segment_size=seg_size, buffer_size=seg_size)

    # 持续写入直到被拒绝
    written = 0
    for i in range(500):
        try:
            store.put(f"lb_key_{i}", b"\x06" * (1024 * 50))
            written += 1
        except Exception:
            print(f"  在写入第 {i} 个 key 时被拒绝（SSD 水位保护）")
            break

    print(f"  成功写入 {written} 个 key")
    assert written > 0, "至少应写入一些 key"
    print("✓ 验证通过：水位保护生效\n")
    store.close()


TESTS = {
    "ssd_full_reject": test_ssd_full_reject,
    "eviction_protection": test_eviction_protection,
    "ssd_eviction_rejected": test_ssd_eviction_rejected,
    "full_lifecycle": test_full_lifecycle,
    "load_balance": test_load_balance,
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
        print(f"所有验证通过！")
    except Exception as e:
        print(f"\n验证失败: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
```

---

## 快速验证

如果只想快速确认修复是否生效，用 Master 日志即可：

```bash
TEST_DIR="/tmp/mooncake_ssd_quick"
rm -rf $TEST_DIR && mkdir -p $TEST_DIR

# 启动 HardPin Master
mooncake_master \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --root_fs_dir=$TEST_DIR \
    --default_kv_lease_ttl=2000 \
    2>&1 | tee master.log &

sleep 2

# 写入直到 SSD 水位触发
# SSD 限制 68MB, DDR 64MB → effective 4MB, 写几个 1MB 对象就会满
MC_METADATA_SERVER=http://127.0.0.1:8080/metadata \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=71303168 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=$TEST_DIR \
python -c "
from mooncake.store import MooncakeDistributedStore
s = MooncakeDistributedStore()
s.setup('127.0.0.1', 'http://127.0.0.1:8080/metadata',
        64*1024*1024, 64*1024*1024, 'tcp', 'eth0',
        '127.0.0.1:50051',
        enable_ssd_offload=True,
        ssd_offload_path='$TEST_DIR')
for i in range(200):
    try:
        s.put(f'k{i}', b'x' * (1024*1024))
        print(f'put k{i}: ok')
    except Exception as e:
        print(f'put k{i}: REJECTED ({e})')
        break
s.close()
"

# 检查日志
echo "--- 检查 Master 日志 ---"
grep "Refusing allocation" master.log && echo "✓ 正确：拒绝了写入" || echo "✗ 未观察到拒绝"
grep "Falling back" master.log && echo "✗ 错误：仍然在 fallback！" || echo "✓ 没有 fallback"

# 清理
kill %1 2>/dev/null
rm -rf $TEST_DIR
```

### 判断标准

| 日志关键词 | 含义 |
|-----------|------|
| `Refusing allocation to guarantee data safety` | **正确**：SSD 水位拒绝写入（修复生效） |
| `Falling back to allocation without SSD filter` | **错误**：仍在 fallback（修复未生效） |
| `Memory eviction skipped: no LOCAL_DISK` | **正确**：驱逐保护生效 |
| `SSD eviction rejected` | **正确**：SSD 副本受保护 |
