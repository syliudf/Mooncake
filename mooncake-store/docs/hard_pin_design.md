# HardPin 分配策略设计文档

## 1. 概述

HardPin 是 Mooncake Store 的一种存储分配策略，核心目标：

- **数据安全**：SSD 数据在任何情况下不被驱逐
- **SSD 全量备份**：每个写入 DDR 的 key 最终必须出现在 SSD 中
- **负载均衡**：SSD 快满的节点不再接收新写入，写入落到有空闲 SSD 的节点

### 适用场景

- 数据可靠性要求高，不允许 SSD 数据丢失
- 每个节点同时提供 DDR（高速缓存）和 SSD（持久存储）
- 多节点集群需要基于 SSD 容量做负载均衡

### 配置要求

```bash
# Master 启动参数
--enable_offload=true
--allocation_strategy=hard_pin
--ssd_watermark_ratio=0.15     # SSD 有效空闲比例阈值

# Client 启动参数
enable_ssd_offload=True
ssd_offload_path=/path/to/ssd
```

不使用 `--offload_on_evict`（默认 false），即 PutEnd 后立即推入 offload 队列。

---

## 2. 架构总览

```mermaid
graph TB
    subgraph Client
        APP[应用程序]
        SDK[Store SDK]
        FS[FileStorage]
    end

    subgraph Master
        PS[PutStart<br/>分配策略]
        PE[PutEnd<br/>推入 offload 队列]
        ET[驱逐线程]
        OOH[OffloadObjectHeartbeat]
        NOS[NotifyOffloadSuccess]
        AS[HardPinAllocationStrategy<br/>SSD 水位检查]
    end

    subgraph Storage
        DDR[DDR 内存<br/>MEMORY 副本]
        SSD[本地 SSD<br/>LOCAL_DISK 副本]
    end

    APP -->|写入| SDK
    SDK -->|1. PutStart| PS
    PS -->|SSD 水位检查| AS
    AS -->|分配 DDR 空间| SDK
    SDK -->|2. 写入数据| DDR
    SDK -->|3. PutEnd| PE
    PE -->|refcnt++| DDR

    SDK -->|4. 心跳| OOH
    OOH -->|返回 offload 列表| FS
    FS -->|5. DDR→SSD 传输| SSD
    FS -->|6. NotifyOffloadSuccess| NOS
    NOS -->|refcnt--, 加 LOCAL_DISK| DDR

    ET -->|检查 LOCAL_DISK| DDR
    ET -->|有 LOCAL_DISK → 驱逐| DDR
```

---

## 3. 数据写入与 Offload 流程

```mermaid
sequenceDiagram
    participant App as 应用程序
    participant Client as Store Client
    participant Master as Master
    participant DDR as DDR
    participant SSD as SSD

    App->>Client: put(key, value)
    Client->>Master: PutStart(key, size)
    Master->>Master: HardPinAllocationStrategy<br/>检查目标节点 SSD 水位
    alt SSD 水位不足
        Master-->>Client: NO_AVAILABLE_HANDLE
        Client-->>App: 写入失败
    else SSD 有空间
        Master-->>Client: 分配 DDR 空间
    end

    Client->>DDR: 写入数据（MEMORY 副本）
    Client->>Master: PutEnd(key)
    Master->>Master: PushOffloadingQueue<br/>offloading_objects[key] = size<br/>MEMORY refcnt++

    Note over Client,SSD: 异步 Offload 阶段

    Client->>Master: 心跳 → OffloadObjectHeartbeat
    Master-->>Client: 返回 offloading_objects（拷贝）

    Client->>DDR: 读取源数据
    Client->>SSD: 写入 SSD（LOCAL_DISK）

    Client->>Master: NotifyOffloadSuccess(key)
    Master->>Master: 加 LOCAL_DISK 副本<br/>offloading_objects.erase(key)<br/>MEMORY refcnt--

    Note over DDR: MEMORY refcnt=0 + LOCAL_DISK 存在<br/>→ 可被驱逐
```

---

## 4. 三大核心机制

### 4.1 无条件驱逐保护

驱逐线程遍历 key 时，**只要没有 LOCAL_DISK 副本，MEMORY 副本就不能被驱逐**。不检查 SSD 水位、不检查 refcnt。

```mermaid
flowchart TD
    START[驱逐线程选取 key] --> LD{有 LOCAL_DISK 副本?}
    LD -->|否| BLOCK[跳过驱逐<br/>数据保留在 DDR]
    LD -->|是| RCNT{MEMORY refcnt == 0?}
    RCNT -->|否| BLOCK2[跳过驱逐<br/>offload 进行中]
    RCNT -->|是| EVICT[驱逐 MEMORY 副本<br/>释放 DDR 空间]
```

### 4.2 SSD 水位分配控制

PutStart 分配时，检查目标节点的 SSD 有效空闲比例。有效容量预留了 DDR 总容量，确保 SSD 物理上始终能容纳 DDR 全量数据。

```mermaid
flowchart TD
    PS[PutStart 请求] --> HPAS[HardPinAllocationStrategy::Allocate]
    HPAS --> LOOP[遍历所有 segment]
    LOOP --> CALC[计算 SSD 有效空闲比例]
    CALC --> FORMULA

    subgraph FORMULA[SSD 水位计算]
        direction TB
        A[有效容量 = SSD 总容量 - DDR 总容量] --> B[有效空闲 = 有效容量 - SSD 已用 - pending]
        B --> C[空闲比例 = 有效空闲 / 有效容量]
    end

    FORMULA --> CHECK{空闲比例 >= watermark?}
    CHECK -->|否| EXCLUDE[排除该 segment]
    CHECK -->|是| CANDIDATE[加入候选集]
    EXCLUDE --> LOOP
    CANDIDATE --> LOOP
    LOOP --> DONE{候选集为空?}
    DONE -->|是| FAIL[返回 NO_AVAILABLE_HANDLE]
    DONE -->|否| ALLOC[按 free-ratio-first 分配]
```

**举例**（DDR 1G, SSD 2G, watermark 0.15）：

| SSD 已用 | 有效容量 | 有效空闲 | 空闲比例 | 结果 |
|----------|---------|---------|---------|------|
| 0.5G | 1G | 0.5G | 50% | 允许分配 |
| 0.85G | 1G | 0.15G | 15% | 刚好到水位线 |
| 0.9G | 1G | 0.1G | 10% | 拒绝分配 |

水位触发时 SSD 物理剩余 = 2G - 0.85G = **1.15G**，足以容纳 DDR 全量数据（≤1G）。

### 4.3 Offload 失败重试

`OffloadObjectHeartbeat` 返回 `offloading_objects` 的**拷贝**（非 move），失败的 key 保留在 map 中供下次心跳重试。

```mermaid
flowchart TD
    HB[客户端心跳] --> OOH[OffloadObjectHeartbeat]
    OOH --> COPY[返回 offloading_objects 拷贝<br/>原 map 保留]
    COPY --> OFFLOAD[客户端执行 offload]
    OFFLOAD --> RESULT{offload 结果}
    RESULT -->|成功| NOS[NotifyOffloadSuccess]
    NOS --> ERASE[offloading_objects.erase(key)]
    NOS --> REFCNT[MEMORY refcnt--]
    RESULT -->|失败| RETRY[key 留在 offloading_objects<br/>下次心跳重试]
```

---

## 5. DDR-SSD 数据流转全貌

```mermaid
stateDiagram-v2
    [*] --> 写入DDR: PutStart 成功

    state 写入DDR {
        [*] --> MEMORY_only
        MEMORY_only: MEMORY 副本 (refcnt > 0)
        MEMORY_only: 未进入 offload 队列
    }

    写入DDR --> 排队中: PutEnd<br/>PushOffloadingQueue

    state 排队中 {
        [*] --> MEMORY_pinned
        MEMORY_pinned: MEMORY 副本 (refcnt > 0)
        MEMORY_pinned: 在 offloading_objects 中
        MEMORY_pinned: 驱逐保护：无条件跳过
    }

    排队中 --> Offloading: 心跳触发<br/>DDR → SSD 传输

    state Offloading {
        [*] --> transferring
        transferring: 数据在 DDR 和 SSD 之间传输
    }

    Offloading --> DDR_SSD共存: NotifyOffloadSuccess

    state DDR_SSD共存 {
        [*] --> both_replicas
        both_replicas: MEMORY 副本 (refcnt = 0)
        both_replicas: LOCAL_DISK 副本 (COMPLETE)
        both_replicas: 可安全驱逐 MEMORY
    }

    DDR_SSD共存 --> SSD仅存: 驱逐线程回收 DDR

    state SSD仅存 {
        [*] --> LOCAL_DISK_only
        LOCAL_DISK_only: 仅 LOCAL_DISK 副本
        LOCAL_DISK_only: DDR 空间已释放
        LOCAL_DISK_only: 数据安全持久化
    }

    SSD仅存 --> [*]

    note right of 排队中
        如果 offload 失败
        → 留在 offloading_objects
        → 下次心跳重试
        → DDR 数据不丢失
    end note
```

---

## 6. 多节点负载均衡

```mermaid
graph LR
    subgraph Master
        AS[HardPinAllocationStrategy]
    end

    subgraph Node_A[节点 A]
        DDR_A[DDR 1G<br/>已用 80%]
        SSD_A[SSD 2G<br/>有效空闲 5%<br/>❌ 水位不足]
    end

    subgraph Node_B[节点 B]
        DDR_B[DDR 1G<br/>已用 40%]
        SSD_B[SSD 4G<br/>有效空闲 60%<br/>✓ 有空间]
    end

    APP[写入请求] --> AS
    AS -->|排除| SSD_A
    AS -->|分配到| SSD_B
```

关键行为：
- 每个 segment 独立计算 SSD 水位
- SSD 满的节点被排除，不参与分配
- 新写入自动落到有空闲 SSD 的节点
- 所有节点 SSD 都满时 → 全局拒绝写入

---

## 7. SSD 满时的系统行为

```mermaid
flowchart TD
    FULL[SSD 水位触发] --> NO_NEW[拒绝新写入<br/>PutStart → NO_AVAILABLE_HANDLE]
    FULL --> CONTINUE[已有 offload 继续进行]

    CONTINUE --> CHECK_DDR{DDR 中有 key<br/>没有 LOCAL_DISK?}
    CHECK_DDR -->|是| PROTECT[驱逐保护<br/>MEMORY 不可驱逐]
    CHECK_DDR -->|否| FREE[DDR 空间可释放]

    PROTECT --> RETRY[offload 重试<br/>拷贝机制保证]
    RETRY --> SUCCESS{SSD 物理空间<br/>足够写入?}
    SUCCESS -->|是| OFFLOAD_OK[offload 成功<br/>加 LOCAL_DISK]
    SUCCESS -->|否| STUCK[系统暂停<br/>等待 SSD 空间释放]

    OFFLOAD_OK --> EVICT_MEM[驱逐 MEMORY<br/>释放 DDR]
```

**关键保证**：保守预留策略（`有效容量 = SSD总容量 - DDR总容量`）确保水位触发时，SSD 物理剩余空间 ≥ DDR 总容量，足以容纳所有待 offload 的数据。

---

## 8. 配置参数参考

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--allocation_strategy` | random | 设为 `hard_pin` 启用本策略 |
| `--enable_offload` | false | 必须设为 true |
| `--ssd_watermark_ratio` | 0.15 | SSD 有效空闲比例阈值 |
| `--offload_on_evict` | false | 建议保持 false（PutEnd 立即 offload） |
| `--eviction_high_watermark_ratio` | 0.95 | DDR 驱逐触发水位 |
| `--port` | 50051 | Master RPC 端口 |
| `--http_metadata_server_port` | 8080 | HTTP 元数据端口 |
| `--metrics_port` | 9003 | 指标端口 |

---

## 9. 故障场景分析

| 场景 | 系统行为 | 数据是否安全 |
|------|---------|-------------|
| DDR 满，SSD 有空间 | 驱逐无 LOCAL_DISK 的 key 被阻止，等待 offload | ✓ 安全 |
| SSD 水位触发 | 拒绝新写入，已有 offload 继续 | ✓ 安全 |
| Offload 传输失败 | key 留在 offloading_objects，下次心跳重试 | ✓ 安全 |
| 所有节点 SSD 满 | 全局拒绝写入，系统暂停 | ✓ 安全 |
| 单节点 SSD 物理满 | 该节点 offload 失败重试，DDR 数据被保护不驱逐 | ✓ 安全 |
| Master 重启 | 元数据恢复，offloading_tasks 丢失但驱逐保护仍在 | ✓ DDR 数据安全 |

---

## 10. 限制与注意事项

1. **SSD 容量必须大于 DDR 容量**：否则有效容量 ≤ 0，无法分配
2. **写入速度受 offload 速度限制**：DDR 填满后，写入吞吐取决于 DDR→SSD 的传输速率
3. **offloading_tasks 超时（600s）**：任务超时后 refcnt 归零，但驱逐保护仍会阻止驱逐（无 LOCAL_DISK 的 key 不会被驱逐），数据不会丢
4. **不支持 SSD 数据驱逐**：HardPin 模式下 `BatchEvictDiskReplica(LOCAL_DISK)` 无条件拒绝，存储后端内部 LRU 驱逐被阻止
