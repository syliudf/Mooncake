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
| `offload_only` | **基础测试**：写入 200MB 数据，验证 DDR→SSD offload 管线是否工作（应首先运行此测试） |
| `ssd_full_reject` | SSD 水位不足时拒绝写入（不 fallback） |
| `eviction_protection` | 无 LOCAL_DISK 副本的 MEMORY 不被驱逐 |
| `ssd_eviction_rejected` | SSD (LOCAL_DISK) 副本不能被驱逐 |
| `full_lifecycle` | 完整 写入→offload→驱逐→读取 生命周期 |

## 默认规模

DDR=4GB, SSD=16GB, Key=4MB
- effective_capacity = 16GB - 4GB = 12GB
- watermark=0.15 → effective_free < 1.8GB 时拒绝
- 即 SSD 已用 > 10.2GB 时拒绝新写入
- 注：实际触发可能更低，因 pending（offloading_objects 中尚未持久化的数据）计入 used

## 注意事项

- **SSD 显示 infinity 的原因**：如果未设置 `MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES`，默认为 2TB，表现为 "infinity"。脚本会检查此环境变量，未设置时报错退出。
- **put() 不抛异常**：`store.put()` 返回整数状态码（0=成功, 非0=失败），不会抛异常。
- **进程会等待退出**：脚本结束时打印 `>>> 按回车退出`，方便查看 Master 日志后再退出。
- **offload 需要足够数据量**：实测发现 offload 在写入量较小时可能不触发，脚本已设计为写入足够多的数据（≥200MB）。
- **每次插入间等 0.01s**：避免写入过快导致问题。
- **Duplicate Key 警告**：运行 `ssd_full_reject` 时可能出现 `Duplicate key detected in BatchOffload` 警告，这是 mooncake offload 管线的已知问题，不影响 HardPin 验证结果。

---

## 验证 0：Offload 管线基础测试（应首先运行）

**Terminal 1** — 启动 Master：

```bash
mooncake_master \
    --port=50053 \
    --http_metadata_server_port=8880 \
    --enable_http_metadata_server=true \
    --metrics_port=9104 \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --default_kv_lease_ttl=2000
```

**Terminal 2** — 运行验证脚本：

```bash
MC_METADATA_SERVER=http://127.0.0.1:8880/metadata \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=17179869184 \
MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=/tmp/mooncake_hardpin_offload_test \
python mooncake-wheel/tests/verify_hard_pin.py --test offload_only
```

### 预期观察

- 写入 50 个 4MB key（200MB）后等待 20 秒
- SSD 路径出现文件，metrics 中 SSD used > 0
- 随机 key 仍可读取（从 SSD 读回）

---

## 验证 1：SSD 全满时拒绝写入（不 fallback）

SSD=16GB, DDR=4GB → effective_capacity=12GB, 水位线=15% (1.8GB)
分批写入，每批 100 个，批间等 5s 让 offload 排空 DDR
约 2600 个 key（约 10.2GB）后 effective_free < 1.8GB，SSD 水位触发拒绝。

**Terminal 1** — 启动 Master：

```bash
mooncake_master \
    --port=50053 \
    --http_metadata_server_port=8880 \
    --enable_http_metadata_server=true \
    --metrics_port=9104 \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --ssd_watermark_ratio=0.15 \
    --default_kv_lease_ttl=2000
```

**Terminal 2** — 运行验证脚本：

```bash
MC_METADATA_SERVER=http://127.0.0.1:8880/metadata \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=17179869184 \
MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=/tmp/mooncake_hardpin_test \
python mooncake-wheel/tests/verify_hard_pin.py --test ssd_full_reject
```

### 预期观察

- 分批写入，每批 100 个后等 5s，约 26 批后 SSD 水位触发拒绝
- 首次拒绝时 DDR 占用应较低（offload 持续排空），确认是 SSD 水位
- Master 日志：`[HARD_PIN] ... Refusing allocation to guarantee data safety.`
- Client 日志：`Failed to start put operation ... NO_AVAILABLE_HANDLE`（**预期行为**）
- **不应出现** `Falling back to allocation without SSD filter`

### 判断标准

Master 回显中搜索：
```bash
# 在 Master terminal 中观察以下日志：
# ✓ 正确：Refusing allocation to guarantee data safety
# ✗ 错误：Falling back to allocation without SSD filter
```

---

## 验证 2：驱逐保护（DDR 中无 LOCAL_DISK 副本的数据不被驱逐）

此测试使用小 DDR（4MB）以便快速触发驱逐。

**Terminal 1** — 启动 Master：

```bash
mooncake_master \
    --port=50053 \
    --http_metadata_server_port=8880 \
    --enable_http_metadata_server=true \
    --metrics_port=9104 \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --default_kv_lease_ttl=500
```

**Terminal 2** — 运行验证脚本：

```bash
MC_METADATA_SERVER=http://127.0.0.1:8880/metadata \
DEFAULT_KV_LEASE_TTL=500 \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=17179869184 \
MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=/tmp/mooncake_hardpin_evict_test \
python mooncake-wheel/tests/verify_hard_pin.py --test eviction_protection
```

### 预期观察

- `protected_key`（没有 LOCAL_DISK 副本）**不被驱逐**
- DDR 写满后新写入失败
- Master 日志：`[HARD_PIN] Memory eviction skipped: no LOCAL_DISK replica`

---

## 验证 3：SSD 驱逐被拒绝

**Terminal 1** — 启动 Master：

```bash
mooncake_master \
    --port=50053 \
    --http_metadata_server_port=8880 \
    --enable_http_metadata_server=true \
    --metrics_port=9104 \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --default_kv_lease_ttl=2000
```

**Terminal 2** — 运行验证脚本：

```bash
MC_METADATA_SERVER=http://127.0.0.1:8880/metadata \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=17179869184 \
MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=/tmp/mooncake_hardpin_ssd_test \
python mooncake-wheel/tests/verify_hard_pin.py --test ssd_eviction_rejected
```

### 预期观察

- 写入 + offload 完成后数据可读取
- 6 次读取全部一致，SSD 副本安全

---

## 验证 4：完整生命周期（正常写入 → offload → 驱逐 DDR → 从 SSD 读取）

**Terminal 1** — 启动 Master：

```bash
mooncake_master \
    --port=50053 \
    --http_metadata_server_port=8880 \
    --enable_http_metadata_server=true \
    --metrics_port=9104 \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --default_kv_lease_ttl=2000
```

**Terminal 2** — 运行验证脚本：

```bash
MC_METADATA_SERVER=http://127.0.0.1:8880/metadata \
DEFAULT_KV_LEASE_TTL=2000 \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=17179869184 \
MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1 \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=/tmp/mooncake_hardpin_lifecycle_test \
python mooncake-wheel/tests/verify_hard_pin.py --test full_lifecycle
```

### 预期观察

1. `put("lifecycle_key", data)` → 成功
2. 等待 20 秒 offload 完成
3. 等 lease 过期后压力写入 1100 个 4MB key（4.4GB > 4GB DDR）触发驱逐
4. `get("lifecycle_key")` → 成功（从 SSD 读取）

---

## 关于 client_service.cpp:1211 的日志

验证过程中 Client 端会出现这样的 WARNING：

```
client_service.cpp:1211] Failed to start put operation for key=xxx due to insufficient space...
```

这是 **预期的正确行为**，说明 HardPin SSD 水位检查生效。

---

## 判断标准

| 日志关键词 | 含义 |
|-----------|------|
| `Refusing allocation to guarantee data safety` | **正确**：SSD 水位拒绝写入（修复生效） |
| `Falling back to allocation without SSD filter` | **错误**：仍在 fallback（修复未生效） |
| `Memory eviction skipped: no LOCAL_DISK` | **正确**：驱逐保护生效 |
| `SSD eviction rejected` | **正确**：SSD 副本受保护 |
| `client_service.cpp:1211 ... NO_AVAILABLE_HANDLE` | **正确**：Client 端收到了水位拒绝 |
| `Duplicate key detected in BatchOffload` | mooncake 已有问题，不影响 HardPin 验证 |
