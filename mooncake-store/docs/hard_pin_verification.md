# HardPin 功能人工验证方法

本文档描述如何通过启动实际的 Master + Client 进程，观察 HardPin 策略的行为。

## 前置条件

- 编译完成 mooncake_store（含 `mooncake_master` 可执行文件，需在 `hard-pin` 分支编译）
- 编译完成 mooncake-wheel（含 Python `mooncake.store` 模块）
- 安装 Python 3 + torch + numpy

## 验证脚本

验证脚本位于 `mooncake-wheel/tests/verify_hard_pin.py`，支持 5 个测试场景：

```
python verify_hard_pin.py --test <test_name>
```

| test_name | 验证内容 |
|-----------|---------|
| `offload_only` | **基础测试**：写入 400MB 数据，验证 DDR→SSD offload 管线是否工作（应首先运行此测试） |
| `ssd_full_reject` | SSD 水位不足时拒绝写入（不 fallback） |
| `eviction_protection` | 无 LOCAL_DISK 副本的 MEMORY 不被驱逐 |
| `ssd_eviction_rejected` | SSD (LOCAL_DISK) 副本不能被驱逐 |
| `full_lifecycle` | 完整 写入→offload→驱逐→读取 生命周期 |

## 关键环境变量

| 环境变量 | 作用 | 示例 |
|---------|------|------|
| `MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES` | **SSD 容量上限**，Client 通过 heartbeat 上报给 Master，Master 用此值计算水位 | `85899345920` (80GB) |
| `MOONCAKE_OFFLOAD_FILE_STORAGE_PATH` | SSD 数据存储目录 | `/tmp/mooncake_ssd_test` |
| `MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS` | offload 心跳间隔（默认 10s，测试时建议设为 1） | `1` |

HardPin 的水位计算公式：
```
effective_capacity = MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES - DDR总容量
effective_free_ratio = effective_free / effective_capacity
```

默认规模：DDR=20GB, SSD=80GB, Key=4MB
- effective_capacity = 80GB - 20GB = 60GB
- watermark=0.15 → effective_free < 9GB 时拒绝
- 即 SSD 已用 > 51GB 时拒绝新写入

## 注意事项

- **SSD 显示 infinity 的原因**：如果未设置 `MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES`，默认为 2TB，表现为 "infinity"。脚本会检查此环境变量，未设置时报错退出。
- **put() 不抛异常**：`store.put()` 返回整数状态码（0=成功, 非0=失败），不会抛异常。脚本已用返回码判断成功/失败。
- **进程会等待退出**：脚本结束时打印 `>>> 按回车键退出`，方便查看 Master 日志后再退出。
- **offload 需要足够数据量**：实测发现 offload 在写入量较小时可能不触发，脚本已设计为写入足够多的数据（≥400MB）。
- **每次插入间等 0.01s**：避免写入过快导致问题。

---

## 验证 0：Offload 管线基础测试（应首先运行）

这是最基本的测试：写入 400MB 数据（100 个 4MB key），等待 30 秒，验证 offload 管线工作。
如果此测试失败，说明 DDR→SSD offload 管线本身有问题，后续测试都会失败。

```bash
TEST_DIR="/tmp/mooncake_hardpin_offload_test"
rm -rf $TEST_DIR && mkdir -p $TEST_DIR

mooncake_master \
    --port=50053 \
    --http_metadata_server_port=8880 \
    --enable_http_metadata_server=true \
    --metrics_port=9104 \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --default_kv_lease_ttl=2000 \
    --log_dir=$TEST_DIR \
    2>&1 | tee master_offload.log &
MASTER_PID=$!
sleep 2

# SSD=80GB, DDR=20GB → effective=60GB (足够大不会触发水位)
MC_METADATA_SERVER=http://127.0.0.1:8880/metadata \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=85899345920 \
MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=$TEST_DIR \
python mooncake-wheel/tests/verify_hard_pin.py --test offload_only

kill $MASTER_PID
rm -rf $TEST_DIR
```

### 预期观察

- 写入 100 个 4MB key（400MB）后等待 30 秒
- SSD 路径出现文件，metrics 中 SSD used > 0
- 随机 key 仍可读取（从 SSD 读回）
- Master 日志中可观察到 heartbeat 处理

### 如果失败

- 检查 Master 日志中是否有 `PushOffloadingQueue` 相关错误
- 检查 `MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS` 是否设为 1
- 检查 SSD 路径是否有写权限
- 检查 `--enable_offload=true` 是否设置
- 检查写入量是否足够（可能需要更多数据触发 offload）

---

## 验证 1：SSD 全满时拒绝写入（不 fallback）

场景设置：SSD=80GB, DDR=20GB → effective_capacity=60GB, 水位线=15% (9GB)
阶段 1：写入 100 个 4MB（400MB），等 30s 验证 offload 正常
阶段 2：分批持续写入，每批 100 个，批间等 10s 让 offload 排空 DDR
约 13000 个 key（约 51GB）后 effective_free < 9GB，SSD 水位触发拒绝。
拒绝时 DDR 应远未满（offload 持续排空），确认是 SSD 水位而非 DDR 满。

```bash
TEST_DIR="/tmp/mooncake_hardpin_test"
rm -rf $TEST_DIR && mkdir -p $TEST_DIR

mooncake_master \
    --port=50053 \
    --http_metadata_server_port=8880 \
    --enable_http_metadata_server=true \
    --metrics_port=9104 \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --ssd_watermark_ratio=0.15 \
    --default_kv_lease_ttl=2000 \
    --log_dir=$TEST_DIR \
    2>&1 | tee master.log &
MASTER_PID=$!
sleep 2

# SSD=80GB, DDR=20GB → effective=60GB
MC_METADATA_SERVER=http://127.0.0.1:8880/metadata \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=85899345920 \
MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=$TEST_DIR \
python mooncake-wheel/tests/verify_hard_pin.py --test ssd_full_reject

# 脚本退出后检查 Master 日志
echo "--- 检查 Master 日志 ---"
grep "Refusing allocation" master.log && echo "✓ 正确：拒绝了写入" || echo "✗ 未观察到拒绝"
grep "Falling back" master.log && echo "✗ 错误：仍然在 fallback！" || echo "✓ 没有 fallback"

# 清理
kill $MASTER_PID
rm -rf $TEST_DIR
```

### 预期观察

- 阶段 1：写入 100 个 4MB 后等 30s，offload 排空 DDR，验证管线正常
- 阶段 2：分批写入，每批 100 个后等 10s，约 130 批后 SSD 水位触发拒绝
- 首次拒绝时 DDR 占用应较低（offload 持续排空），确认是 SSD 水位
- Master 日志：`[HARD_PIN] ... Refusing allocation to guarantee data safety.`
- Client 日志：`Failed to start put operation ... NO_AVAILABLE_HANDLE`（`client_service.cpp:1211`，**预期行为**）
- **不应出现** `Falling back to allocation without SSD filter`
- **不应出现** 大量 `EVICT-TRIGGER` 日志（因为 DDR 不满）

---

## 验证 2：驱逐保护（DDR 中无 LOCAL_DISK 副本的数据不被驱逐）

此测试使用小 DDR（4MB）以便快速触发驱逐，不受默认 20GB 影响。

```bash
TEST_DIR="/tmp/mooncake_hardpin_evict_test"
rm -rf $TEST_DIR && mkdir -p $TEST_DIR

mooncake_master \
    --port=50053 \
    --http_metadata_server_port=8880 \
    --enable_http_metadata_server=true \
    --metrics_port=9104 \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --default_kv_lease_ttl=500 \
    --log_dir=$TEST_DIR \
    2>&1 | tee master_evict.log &
MASTER_PID=$!
sleep 2

# SSD=80GB, DDR=4MB（脚本内部覆盖为 4MB）
MC_METADATA_SERVER=http://127.0.0.1:8880/metadata \
DEFAULT_KV_LEASE_TTL=500 \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=85899345920 \
MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=$TEST_DIR \
python mooncake-wheel/tests/verify_hard_pin.py --test eviction_protection

kill $MASTER_PID
rm -rf $TEST_DIR
```

### 预期观察

- 脚本写入 `protected_key` 后，继续填满 DDR
- `protected_key`（没有 LOCAL_DISK 副本）**不被驱逐**
- DDR 写满后新写入失败
- Master 日志：`[HARD_PIN] Memory eviction skipped: no LOCAL_DISK replica`

---

## 验证 3：SSD 驱逐被拒绝

```bash
TEST_DIR="/tmp/mooncake_hardpin_ssd_test"
rm -rf $TEST_DIR && mkdir -p $TEST_DIR

mooncake_master \
    --port=50053 \
    --http_metadata_server_port=8880 \
    --enable_http_metadata_server=true \
    --metrics_port=9104 \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --default_kv_lease_ttl=2000 \
    --log_dir=$TEST_DIR \
    2>&1 | tee master_ssd.log &
MASTER_PID=$!
sleep 2

MC_METADATA_SERVER=http://127.0.0.1:8880/metadata \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=85899345920 \
MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=$TEST_DIR \
python mooncake-wheel/tests/verify_hard_pin.py --test ssd_eviction_rejected

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
    --port=50053 \
    --http_metadata_server_port=8880 \
    --enable_http_metadata_server=true \
    --metrics_port=9104 \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --default_kv_lease_ttl=2000 \
    --log_dir=$TEST_DIR \
    2>&1 | tee master_lifecycle.log &
MASTER_PID=$!
sleep 2

# SSD=80GB, DDR=20GB → effective=60GB
MC_METADATA_SERVER=http://127.0.0.1:8880/metadata \
DEFAULT_KV_LEASE_TTL=2000 \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=85899345920 \
MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=$TEST_DIR \
python mooncake-wheel/tests/verify_hard_pin.py --test full_lifecycle

kill $MASTER_PID
rm -rf $TEST_DIR
```

### 预期观察

1. `put("lifecycle_key", data)` → 成功（SSD 有空间）
2. 等待 30 秒 offload 完成（heartbeat 驱动 DDR→SSD 传输）
3. 等 lease 过期后压力写入 5500 个 4MB key（22GB > 20GB DDR）触发驱逐 → `lifecycle_key` 的 MEMORY 副本被驱逐（因为已有 LOCAL_DISK）
4. `get("lifecycle_key")` → 成功（从 SSD 读取）

---

## 关于 client_service.cpp:1211 的日志

验证过程中 Client 端会出现这样的 WARNING：

```
client_service.cpp:1211] Failed to start put operation for key=xxx due to insufficient space...
```

这是 **预期的正确行为**，说明 HardPin SSD 水位检查生效：
1. HardPin 策略检测到 SSD effective_free_ratio < 水位线
2. 返回 `NO_AVAILABLE_HANDLE` 给 Client
3. Client 在 `client_service.cpp:1211` 打出这行 WARNING

如果**没有**看到这行日志反而说明修复未生效（仍在 fallback 写入）。

---

## 快速验证

如果只想快速确认修复是否生效，用 Master 日志即可：

```bash
TEST_DIR="/tmp/mooncake_ssd_quick"
rm -rf $TEST_DIR && mkdir -p $TEST_DIR

mooncake_master \
    --port=50053 \
    --http_metadata_server_port=8880 \
    --enable_http_metadata_server=true \
    --metrics_port=9104 \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --ssd_watermark_ratio=0.15 \
    --default_kv_lease_ttl=2000 \
    --log_dir=$TEST_DIR \
    2>&1 | tee master.log &

sleep 2

MC_METADATA_SERVER=http://127.0.0.1:8880/metadata \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=85899345920 \
MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=$TEST_DIR \
python mooncake-wheel/tests/verify_hard_pin.py --test ssd_full_reject

echo "--- 检查 Master 日志 ---"
grep "Refusing allocation" master.log && echo "✓ 正确：拒绝了写入" || echo "✗ 未观察到拒绝"
grep "Falling back" master.log && echo "✗ 错误：仍然在 fallback！" || echo "✓ 没有 fallback"

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
| `client_service.cpp:1211 ... NO_AVAILABLE_HANDLE` | **正确**：Client 端收到了水位拒绝 |
