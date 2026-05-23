# HardPin 功能人工验证方法

## 前置条件

- 编译完成 mooncake_store（含 `mooncake_master` 可执行文件，需在 `hard-pin` 分支编译）
- 编译完成 mooncake-wheel（含 Python `mooncake.store` 模块）
- 安装 Python 3 + torch + numpy

## 一键验证

```bash
# 验证 SSD 水位拒绝（默认）
bash mooncake-wheel/tests/verify_hard_pin.sh ssd_full_reject

# 验证驱逐保护
bash mooncake-wheel/tests/verify_hard_pin.sh eviction_protection

# 验证 SSD 副本不可驱逐
bash mooncake-wheel/tests/verify_hard_pin.sh ssd_eviction_rejected

# 验证完整生命周期
bash mooncake-wheel/tests/verify_hard_pin.sh full_lifecycle
```

脚本会自动：启动 Master → 等待就绪 → 运行验证 → 展示结果 → 清理进程。
Master 日志输出到文件，不会刷屏。

## 验证项目与设计文档对应

| 脚本参数 | 设计文档章节 | 核心验证点 |
|---------|------------|-----------|
| `ssd_full_reject` | 4.2 SSD 水位控制 | effective_free_ratio < watermark → 拒绝，不 fallback |
| `eviction_protection` | 4.1 驱逐保护 | 无 LOCAL_DISK 的 MEMORY 不被驱逐 |
| `ssd_eviction_rejected` | SSD 不可驱逐 | LOCAL_DISK 副本永不丢失 |
| `full_lifecycle` | 5. 数据流转 | DDR→SSD→驱逐→读取 完整流程 |

## 关键环境变量

| 环境变量 | 作用 |
|---------|------|
| `MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES` | SSD 容量上限，Client 上报给 Master 用于计算水位 |
| `MOONCAKE_OFFLOAD_FILE_STORAGE_PATH` | SSD 数据存储目录 |

水位计算：`effective_capacity = SSD总容量 - DDR总容量`，`effective_free_ratio = effective_free / effective_capacity`

脚本已内置这些变量，无需手动设置。

## 注意事项

- `store.put()` 返回整数状态码（0=成功, 非0=失败），不抛异常
- 脚本结束时会暂停等待按回车，方便查看日志后再退出
- Master 日志保存在 `/tmp/mooncake_hardpin_verify_<PID>/master.log`

---

## 验证 1：SSD 水位拒绝

```bash
bash mooncake-wheel/tests/verify_hard_pin.sh ssd_full_reject
```

场景：SSD=128MB, DDR=64MB → effective=64MB, 水位=15% (9.6MB)
分批写入：每批 4 个 4MB，批间等 5 秒让 offload 排空 DDR
约 14 个对象后水位触发，拒绝时 DDR 仅 ~12% → 确认是 SSD 水位而非 DDR 满

### 预期

- 分批写入 ~14 个后首次拒绝
- Master 日志：`Refusing allocation to guarantee data safety`
- Client 日志：`NO_AVAILABLE_HANDLE`（`client_service.cpp:1211`，预期行为）
- 无 `Falling back`
- 无大量 `EVICT-TRIGGER`（DDR 不满）

---

## 验证 2：驱逐保护

```bash
bash mooncake-wheel/tests/verify_hard_pin.sh eviction_protection
```

场景：DDR=4MB, SSD=256MB, KV_TTL=500ms
写入 protected_key → 等 lease 过期 → 填满 DDR → protected_key 不被驱逐

### 预期

- DDR 满后 protected_key 仍可读取
- Master 日志：`Memory eviction skipped: no LOCAL_DISK`

---

## 验证 3：SSD 副本不可驱逐

```bash
bash mooncake-wheel/tests/verify_hard_pin.sh ssd_eviction_rejected
```

### 预期

- 写入 + offload 后反复读取数据一致
- SSD 副本始终安全

---

## 验证 4：完整生命周期

```bash
bash mooncake-wheel/tests/verify_hard_pin.sh full_lifecycle
```

### 预期

1. 写入 DDR → 成功
2. 等 offload → DDR+SSD 共存
3. DDR 满 → MEMORY 被驱逐（有 LOCAL_DISK）
4. 从 SSD 读取 → 成功

---

## 关于 client_service.cpp:1211

验证中 Client 端出现 `Failed to start put operation ... NO_AVAILABLE_HANDLE` 是**预期行为**，说明 SSD 水位拒绝已传达给 Client。

## 判断标准

| 日志关键词 | 含义 |
|-----------|------|
| `Refusing allocation to guarantee data safety` | SSD 水位拒绝（正确） |
| `Falling back to allocation without SSD filter` | 仍在 fallback（错误） |
| `Memory eviction skipped: no LOCAL_DISK` | 驱逐保护（正确） |
| `SSD eviction rejected` | SSD 副本受保护（正确） |
| `EVICT-TRIGGER` 大量刷屏 | DDR 满+驱逐失败，说明写入太快 |
