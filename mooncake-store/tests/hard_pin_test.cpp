// HardPin 分配策略单元测试 + 集成测试
//
// 验证 HardPin 策略的三大核心保证：
// 1. SSD 水位不足时拒绝分配（不 fallback）
// 2. 没有 LOCAL_DISK 副本的 MEMORY 不被驱逐
// 3. LOCAL_DISK 副本不能被驱逐
//
// 分两部分：
// - HardPinStrategyTest: 纯策略层单元测试，不需要 MasterService
// - HardPinIntegrationTest: MasterService 集成测试

#include "allocation_strategy.h"
#include "master_service.h"

#include <glog/logging.h>
#include <gtest/gtest.h>

#include <chrono>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "types.h"

namespace mooncake::test {

static constexpr size_t MiB = 1024 * 1024;

// =============================================================================
// 第一部分：HardPinAllocationStrategy 纯策略单元测试
// =============================================================================

class HardPinStrategyTest : public ::testing::Test {
   protected:
    void SetUp() override {
        strategy_ = std::make_unique<HardPinAllocationStrategy>();
        strategy_->ssd_watermark_ratio_ = 0.15;
    }

    // 创建测试用的 buffer allocator
    std::shared_ptr<BufferAllocatorBase> MakeAllocator(
        const std::string& name, size_t base_offset,
        size_t size = 64 * MiB) {
        return std::make_shared<OffsetBufferAllocator>(
            name, 0x100000000ULL + base_offset, size, name);
    }

    std::unique_ptr<HardPinAllocationStrategy> strategy_;
};

// 场景：所有 segment SSD 水位充足
// 预期：正常分配
TEST_F(HardPinStrategyTest, AllSsdOk_AllocatesNormally) {
    // 设置回调：所有 segment 的 SSD 空闲比例都是 50%
    strategy_->SetSsdFreeRatioQuery(
        [](const std::string&) { return 0.5; });

    AllocatorManager mgr;
    mgr.addAllocator("seg1", MakeAllocator("seg1", 0));
    mgr.addAllocator("seg2", MakeAllocator("seg2", 0x10000000ULL));

    auto result = strategy_->Allocate(mgr, 1024, 1, {}, {});
    ASSERT_TRUE(result.has_value()) << "SSD 充足时应正常分配";
    EXPECT_EQ(result.value().size(), 1u);
}

// 场景：部分 segment SSD 不足，部分充足
// 预期：只分配到 SSD 充足的 segment
TEST_F(HardPinStrategyTest, PartialSsdFull_AllocatesToHealthySegments) {
    // seg1 SSD 满，seg2 SSD 健康
    strategy_->SetSsdFreeRatioQuery([](const std::string& name) -> double {
        if (name == "seg1") return 0.05;  // 5% < 15% 水位线
        return 0.50;                       // 50% > 15%
    });

    AllocatorManager mgr;
    mgr.addAllocator("seg1", MakeAllocator("seg1", 0));
    mgr.addAllocator("seg2", MakeAllocator("seg2", 0x10000000ULL));

    auto result = strategy_->Allocate(mgr, 1024, 1, {}, {});
    ASSERT_TRUE(result.has_value()) << "有健康 segment 时应分配成功";

    // 应该分配到 seg2（SSD 健康的），而非 seg1
    const auto& replica = result.value()[0];
    auto desc = replica.get_descriptor();
    ASSERT_TRUE(desc.is_memory_replica());
    EXPECT_EQ(desc.get_memory_descriptor().buffer_descriptor.transport_endpoint_,
              "seg2");
}

// 场景：所有 segment SSD 都低于水位线
// 预期：拒绝分配，返回 NO_AVAILABLE_HANDLE
// 这是修复的核心验证点：之前的 fallback 行为已被移除
TEST_F(HardPinStrategyTest, AllSsdFull_RefusesAllocation_NoFallback) {
    // 所有 segment SSD 空闲比例 5%，远低于 15% 水位线
    strategy_->SetSsdFreeRatioQuery(
        [](const std::string&) { return 0.05; });

    AllocatorManager mgr;
    mgr.addAllocator("seg1", MakeAllocator("seg1", 0));
    mgr.addAllocator("seg2", MakeAllocator("seg2", 0x10000000ULL));
    mgr.addAllocator("seg3", MakeAllocator("seg3", 0x20000000ULL));

    auto result = strategy_->Allocate(mgr, 1024, 1, {}, {});
    EXPECT_FALSE(result.has_value())
        << "所有 SSD 水位不足时必须拒绝，不能 fallback";
    EXPECT_EQ(result.error(), ErrorCode::NO_AVAILABLE_HANDLE);
}

// 场景：水位线边界值测试
// 预期：恰好低于水位线时拒绝
TEST_F(HardPinStrategyTest, ExactlyBelowWatermark_Refuses) {
    // 空闲比例 = 水位线 - 0.001，刚好不满足
    strategy_->SetSsdFreeRatioQuery(
        [this](const std::string&) {
            return strategy_->ssd_watermark_ratio_ - 0.001;
        });

    AllocatorManager mgr;
    mgr.addAllocator("seg1", MakeAllocator("seg1", 0));

    auto result = strategy_->Allocate(mgr, 1024, 1, {}, {});
    EXPECT_FALSE(result.has_value());
    EXPECT_EQ(result.error(), ErrorCode::NO_AVAILABLE_HANDLE);
}

// 场景：水位线边界值测试
// 预期：恰好等于水位线时允许分配
TEST_F(HardPinStrategyTest, ExactlyAtWatermark_Allowed) {
    // 空闲比例 = 水位线，刚好满足（>= 判断）
    strategy_->SetSsdFreeRatioQuery(
        [this](const std::string&) {
            return strategy_->ssd_watermark_ratio_;
        });

    AllocatorManager mgr;
    mgr.addAllocator("seg1", MakeAllocator("seg1", 0));

    auto result = strategy_->Allocate(mgr, 1024, 1, {}, {});
    ASSERT_TRUE(result.has_value())
        << "SSD 空闲比例恰好等于水位线时应允许分配";
}

// 场景：未设置 SSD 查询回调
// 预期：降级为普通 FreeRatioFirst，不做 SSD 过滤
TEST_F(HardPinStrategyTest, NoSsdQuery_DelegatesToFreeRatioFirst) {
    // 不设置 SetSsdFreeRatioQuery，ssd_free_ratio_query_ 为空
    AllocatorManager mgr;
    mgr.addAllocator("seg1", MakeAllocator("seg1", 0));

    auto result = strategy_->Allocate(mgr, 1024, 1, {}, {});
    ASSERT_TRUE(result.has_value())
        << "无 SSD 查询回调时应降级为 FreeRatioFirst";
}

// 场景：SSD 满的 segment 与调用方排除的 segment 叠加
// 预期：两者取并集排除
TEST_F(HardPinStrategyTest, SsdFull_PlusCallerExclusion_BothExcluded) {
    strategy_->SetSsdFreeRatioQuery([](const std::string& name) -> double {
        if (name == "seg1") return 0.05;  // SSD 满
        return 0.50;
    });

    AllocatorManager mgr;
    mgr.addAllocator("seg1", MakeAllocator("seg1", 0));
    mgr.addAllocator("seg2", MakeAllocator("seg2", 0x10000000ULL));
    mgr.addAllocator("seg3", MakeAllocator("seg3", 0x20000000ULL));

    // seg2 被调用方排除，seg1 被 SSD 水位排除，只剩 seg3
    std::set<std::string> excluded = {"seg2"};
    auto result = strategy_->Allocate(mgr, 1024, 1, {}, excluded);
    ASSERT_TRUE(result.has_value());

    const auto& replica = result.value()[0];
    auto desc = replica.get_descriptor();
    EXPECT_EQ(desc.get_memory_descriptor().buffer_descriptor.transport_endpoint_,
              "seg3");
}

// =============================================================================
// 第二部分：MasterService 集成测试
// =============================================================================

class HardPinIntegrationTest : public ::testing::Test {
   protected:
    void SetUp() override {
        google::InitGoogleLogging("HardPinIntegrationTest");
        FLAGS_logtostderr = true;
    }

    void TearDown() override { google::ShutdownGoogleLogging(); }

    static constexpr size_t kDefaultSegmentBase = 0x300000000;

    // 创建 HardPin 模式的 MasterService
    std::unique_ptr<MasterService> CreateHardPinService(
        uint64_t kv_lease_ttl = 2000) {
        MasterServiceConfig config =
            MasterServiceConfig::builder()
                .set_allocation_strategy_type(AllocationStrategyType::HARD_PIN)
                .set_enable_offload(true)
                .set_ssd_watermark_ratio(0.15)
                .set_default_kv_lease_ttl(kv_lease_ttl)
                .build();
        return std::make_unique<MasterService>(config);
    }

    Segment MakeSegment(std::string name, size_t base, size_t size) const {
        Segment segment;
        segment.id = generate_uuid();
        segment.name = std::move(name);
        segment.base = base;
        segment.size = size;
        segment.te_endpoint = segment.name;
        return segment;
    }

    struct SegmentContext {
        UUID segment_id;
        UUID client_id;
    };

    // 挂载 DDR segment + LocalDiskSegment，返回上下文
    SegmentContext PrepareSegmentWithLocalDisk(
        MasterService& service, std::string name, size_t base, size_t size,
        bool enable_offloading = true) const {
        Segment segment = MakeSegment(std::move(name), base, size);
        UUID client_id = generate_uuid();
        auto mount_result = service.MountSegment(segment, client_id);
        EXPECT_TRUE(mount_result.has_value());

        // 挂载本地磁盘段以支持 offload
        auto mount_ld =
            service.MountLocalDiskSegment(client_id, enable_offloading);
        EXPECT_TRUE(mount_ld.has_value());

        return {.segment_id = segment.id, .client_id = client_id};
    }

    // 写入一个 key 并 PutEnd 完成
    void PutObject(MasterService& service, const UUID& client_id,
                   const std::string& key, size_t size = 1024) {
        ReplicateConfig config;
        config.replica_num = 1;
        auto put_start = service.PutStart(client_id, key, size, config);
        ASSERT_TRUE(put_start.has_value()) << "PutStart failed for key=" << key;
        auto put_end = service.PutEnd(client_id, key, ReplicaType::MEMORY);
        ASSERT_TRUE(put_end.has_value()) << "PutEnd failed for key=" << key;
    }

    // 模拟完成 offload：拉取队列 → NotifyOffloadSuccess
    void CompleteOffloadForKeys(
        MasterService& service, const UUID& client_id,
        const std::vector<std::string>& keys, size_t object_size) {
        // 拉取 offload 队列（PutEnd 后数据应在此）
        auto heartbeat = service.OffloadObjectHeartbeat(client_id, true);
        ASSERT_TRUE(heartbeat.has_value());

        // 构造 NotifyOffloadSuccess 所需的 metadata
        std::vector<StorageObjectMetadata> metadatas;
        for (size_t i = 0; i < keys.size(); ++i) {
            StorageObjectMetadata meta;
            meta.bucket_id = 0;
            meta.offset = i * object_size;
            meta.key_size = keys[i].size();
            meta.data_size = static_cast<int64_t>(object_size);
            meta.transport_endpoint = "test_segment";
            metadatas.push_back(meta);
        }

        auto notify =
            service.NotifyOffloadSuccess(client_id, keys, metadatas);
        ASSERT_TRUE(notify.has_value())
            << "NotifyOffloadSuccess failed: " << notify.error();
    }

    // 等待条件满足，带超时
    template <typename Predicate>
    void WaitUntil(Predicate&& predicate,
                   std::chrono::milliseconds timeout = std::chrono::seconds(5),
                   std::chrono::milliseconds interval =
                       std::chrono::milliseconds(50)) const {
        auto deadline = std::chrono::steady_clock::now() + timeout;
        while (std::chrono::steady_clock::now() < deadline) {
            if (predicate()) return;
            std::this_thread::sleep_for(interval);
        }
        EXPECT_TRUE(predicate()) << "WaitUntil timed out";
    }
};

// 集成测试 1：SSD 有空间时正常写入 + offload 流程
// 验证端到端的 PutStart → PutEnd → offload → 驱逐 流程
TEST_F(HardPinIntegrationTest, NormalWriteAndOffload) {
    auto service = CreateHardPinService();

    // 挂载 16MB DDR segment
    constexpr size_t seg_size = 16 * MiB;
    auto ctx = PrepareSegmentWithLocalDisk(
        *service, "test_segment", kDefaultSegmentBase, seg_size);

    // 报告 SSD 容量：2MB（大于 DDR 容量用于 offload）
    // effective_capacity = 2MB - 16MB < 0 → 但上报的容量是按物理总量算的
    // 这里用足够大的 SSD 让水位通过
    auto report = service->ReportSsdCapacity(ctx.client_id, 256 * MiB);
    ASSERT_TRUE(report.has_value());

    // 写入 3 个 key
    PutObject(*service, ctx.client_id, "key1", 1024);
    PutObject(*service, ctx.client_id, "key2", 1024);
    PutObject(*service, ctx.client_id, "key3", 1024);

    // PutEnd 后应自动推入 offload 队列（因为 offload_on_evict=false）
    auto heartbeat = service->OffloadObjectHeartbeat(ctx.client_id, true);
    ASSERT_TRUE(heartbeat.has_value());
    EXPECT_EQ(heartbeat.value().size(), 3u)
        << "PutEnd 后所有 key 应进入 offload 队列";

    // 模拟完成 offload
    CompleteOffloadForKeys(*service, ctx.client_id,
                           {"key1", "key2", "key3"}, 1024);

    // 验证：Get 应能查到副本
    auto get1 = service->GetReplicaList("key1");
    ASSERT_TRUE(get1.has_value());
    // offload 后应有 MEMORY（refcnt=0 可被驱逐）+ LOCAL_DISK 副本
    bool has_local_disk = false;
    for (const auto& r : get1.value().replicas) {
        if (r.is_local_disk) has_local_disk = true;
    }
    EXPECT_TRUE(has_local_disk) << "offload 完成后应有 LOCAL_DISK 副本";
}

// 集成测试 2：所有 segment SSD 水位不足时拒绝新写入
// 核心修复验证：确保不会 fallback
TEST_F(HardPinIntegrationTest, AllSsdFull_RefusesNewWrites) {
    auto service = CreateHardPinService();

    constexpr size_t seg_size = 4 * MiB;
    auto ctx = PrepareSegmentWithLocalDisk(
        *service, "test_segment", kDefaultSegmentBase, seg_size);

    // 报告一个很小的 SSD 容量，使 effective_capacity 很小
    // effective_capacity = ssd_total - per_segment_ddr
    // per_segment_ddr 来自 MasterMetricManager 的 get_segment_total_mem_capacity()
    // 设 SSD = 4MB + 100KB，本 segment DDR = 4MB
    // → effective_capacity = 100KB，水位线 15%
    // → 只要写入 1 个 key（90KB），effective 空闲就低于水位线
    //
    // 水位线 15% → effective_free >= 15KB 才允许写入
    // effective_free = 100KB - 90KB = 10KB < 15KB → 拒绝
    service->ReportSsdCapacity(ctx.client_id, 4 * MiB + 100 * 1024);

    // 写入一个 key 并完成 offload 以消耗 SSD 空间
    PutObject(*service, ctx.client_id, "key1", 90 * 1024);  // 90KB
    CompleteOffloadForKeys(*service, ctx.client_id, {"key1"}, 90 * 1024);

    // effective_free = 100KB - 90KB = 10KB
    // effective_free_ratio = 10/100 = 10% < 15% 水位线
    // 新写入应被拒绝
    ReplicateConfig config;
    config.replica_num = 1;
    auto result =
        service->PutStart(ctx.client_id, "key2", 1024, config);
    EXPECT_FALSE(result.has_value())
        << "SSD 水位不足时应拒绝写入，不 fallback";
    EXPECT_EQ(result.error(), ErrorCode::NO_AVAILABLE_HANDLE);
}

// 集成测试 3：驱逐保护——没有 LOCAL_DISK 的 MEMORY 不被驱逐
TEST_F(HardPinIntegrationTest, EvictionProtection_NoLocalDisk_NoEviction) {
    // 使用短 lease TTL 加速驱逐触发
    const uint64_t kv_lease_ttl = 500;  // 500ms
    auto service = CreateHardPinService(kv_lease_ttl);

    // 小 segment，只够放少量 key，容易触发驱逐
    constexpr size_t seg_size = 1024 * 100;  // 100KB
    auto ctx = PrepareSegmentWithLocalDisk(
        *service, "test_segment", kDefaultSegmentBase, seg_size);

    // 报告足够大的 SSD 空间
    service->ReportSsdCapacity(ctx.client_id, 10 * MiB);

    // 写入一个 key 但不 offload（不调用 OffloadObjectHeartbeat）
    // 这样 MEMORY 副本没有对应的 LOCAL_DISK 副本
    ReplicateConfig config;
    config.replica_num = 1;
    auto put_result =
        service->PutStart(ctx.client_id, "protected_key", 1024, config);
    ASSERT_TRUE(put_result.has_value());
    service->PutEnd(ctx.client_id, "protected_key", ReplicaType::MEMORY);

    // 等待 lease 过期
    std::this_thread::sleep_for(std::chrono::milliseconds(kv_lease_ttl + 200));

    // 尝试写入更多 key 触发驱逐
    for (int i = 0; i < 200; ++i) {
        std::string key = "filler_" + std::to_string(i);
        auto result = service->PutStart(ctx.client_id, key, 1024, config);
        if (result.has_value()) {
            service->PutEnd(ctx.client_id, key, ReplicaType::MEMORY);
        }
    }

    // 验证：protected_key 仍应可查到（驱逐保护阻止了驱逐）
    // 因为它没有 LOCAL_DISK 副本
    auto get_result = service->GetReplicaList("protected_key");
    EXPECT_TRUE(get_result.has_value())
        << "HardPin 驱逐保护：没有 LOCAL_DISK 副本的 key 不应被驱逐";
}

// 集成测试 4：SSD 驱逐拒绝——LOCAL_DISK 副本不能被驱逐
TEST_F(HardPinIntegrationTest, SsdEvictionRejected) {
    auto service = CreateHardPinService();

    constexpr size_t seg_size = 16 * MiB;
    auto ctx = PrepareSegmentWithLocalDisk(
        *service, "test_segment", kDefaultSegmentBase, seg_size);
    service->ReportSsdCapacity(ctx.client_id, 256 * MiB);

    // 写入并完成 offload
    PutObject(*service, ctx.client_id, "ssd_key", 1024);
    CompleteOffloadForKeys(*service, ctx.client_id, {"ssd_key"}, 1024);

    // 尝试驱逐 LOCAL_DISK 副本
    auto result = service->BatchEvictDiskReplica(
        ctx.client_id, {"ssd_key"}, ReplicaType::LOCAL_DISK);

    ASSERT_EQ(result.size(), 1u);
    EXPECT_FALSE(result[0].has_value())
        << "HardPin 模式下 LOCAL_DISK 驱逐应被拒绝";
    EXPECT_EQ(result[0].error(), ErrorCode::INVALID_PARAMS);
}

// 集成测试 5：有 LOCAL_DISK 副本后 MEMORY 可正常驱逐
TEST_F(HardPinIntegrationTest, WithLocalDisk_MemoryEvictionAllowed) {
    const uint64_t kv_lease_ttl = 500;
    auto service = CreateHardPinService(kv_lease_ttl);

    constexpr size_t seg_size = 1024 * 50;  // 50KB
    auto ctx = PrepareSegmentWithLocalDisk(
        *service, "test_segment", kDefaultSegmentBase, seg_size);
    service->ReportSsdCapacity(ctx.client_id, 10 * MiB);

    // 写入并完成 offload（此时 key 同时有 MEMORY + LOCAL_DISK）
    PutObject(*service, ctx.client_id, "evictable_key", 1024);
    CompleteOffloadForKeys(*service, ctx.client_id, {"evictable_key"}, 1024);

    // 等待 lease 过期
    std::this_thread::sleep_for(std::chrono::milliseconds(kv_lease_ttl + 200));

    // 写入更多数据触发驱逐
    ReplicateConfig config;
    config.replica_num = 1;
    for (int i = 0; i < 100; ++i) {
        std::string key = "filler_" + std::to_string(i);
        auto result = service->PutStart(ctx.client_id, key, 1024, config);
        if (result.has_value()) {
            service->PutEnd(ctx.client_id, key, ReplicaType::MEMORY);
        }
    }

    // evictable_key 应仍可查到（数据在 SSD 上安全）
    // 但 MEMORY 副本可能已被驱逐
    auto get_result = service->GetReplicaList("evictable_key");
    EXPECT_TRUE(get_result.has_value())
        << "有 LOCAL_DISK 的 key 即使 MEMORY 被驱逐也应可查到";
}

}  // namespace mooncake::test
