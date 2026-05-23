#!/bin/bash
# HardPin 策略一键验证脚本
# 用法: ./verify_hard_pin.sh [test_name]
#   test_name: ssd_full_reject | eviction_protection | ssd_eviction_rejected | full_lifecycle
#   默认运行 ssd_full_reject
#
# 此脚本自动启动 Master、运行验证、展示结果、清理进程。
# Master 日志输出到 master_test.log，不会刷屏。
set -e

TEST_NAME="${1:-ssd_full_reject}"
TEST_DIR="/tmp/mooncake_hardpin_verify_$$"
MASTER_LOG="${TEST_DIR}/master.log"
MASTER_PID=""

# 端口配置
MASTER_PORT=50053
METADATA_PORT=8880
METRICS_PORT=9104

cleanup() {
    if [ -n "$MASTER_PID" ]; then
        kill "$MASTER_PID" 2>/dev/null || true
        # 等待进程退出，最多 5 秒
        for i in $(seq 1 10); do
            if ! kill -0 "$MASTER_PID" 2>/dev/null; then
                break
            fi
            sleep 0.5
        done
        # 如果还没退出，强制杀
        kill -9 "$MASTER_PID" 2>/dev/null || true
    fi
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

mkdir -p "$TEST_DIR"

echo "============================================================"
echo "HardPin 验证: $TEST_NAME"
echo "============================================================"
echo ""

# ---- 根据测试类型选择参数 ----
case "$TEST_NAME" in
    ssd_full_reject)
        SSD_LIMIT=134217728      # 128MB
        SEG_SIZE=67108864        # 64MB
        KV_TTL=2000
        ;;
    eviction_protection)
        SSD_LIMIT=268435456      # 256MB
        SEG_SIZE=4194304         # 4MB
        KV_TTL=500
        ;;
    ssd_eviction_rejected)
        SSD_LIMIT=268435456      # 256MB
        SEG_SIZE=67108864        # 64MB
        KV_TTL=2000
        ;;
    full_lifecycle)
        SSD_LIMIT=268435456      # 256MB
        SEG_SIZE=67108864        # 64MB
        KV_TTL=2000
        ;;
    *)
        echo "未知测试: $TEST_NAME"
        echo "可用: ssd_full_reject, eviction_protection, ssd_eviction_rejected, full_lifecycle"
        exit 1
        ;;
esac

# ---- 启动 Master ----
echo "[1/3] 启动 Master (日志 → $MASTER_LOG) ..."
mooncake_master \
    --port=$MASTER_PORT \
    --http_metadata_server_port=$METADATA_PORT \
    --enable_http_metadata_server=true \
    --metrics_port=$METRICS_PORT \
    --allocation_strategy=hard_pin \
    --enable_offload=true \
    --ssd_watermark_ratio=0.15 \
    --default_kv_lease_ttl=$KV_TTL \
    --root_fs_dir=$TEST_DIR \
    > "$MASTER_LOG" 2>&1 &
MASTER_PID=$!

# 等待 Master 就绪
echo "       等待 Master 就绪 ..."
for i in $(seq 1 20); do
    if curl -s "http://127.0.0.1:$METADATA_PORT/metadata" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$MASTER_PID" 2>/dev/null; then
        echo "[ERROR] Master 启动失败，查看日志:"
        cat "$MASTER_LOG"
        exit 1
    fi
    sleep 0.5
done

if ! kill -0 "$MASTER_PID" 2>/dev/null; then
    echo "[ERROR] Master 启动失败，查看日志:"
    cat "$MASTER_LOG"
    exit 1
fi

echo "       Master 就绪 (PID=$MASTER_PID)"
echo ""

# ---- 运行验证脚本 ----
echo "[2/3] 运行验证脚本 ..."
echo "       SSD=${SSD_LIMIT}, DDR=${SEG_SIZE}, KV_TTL=${KV_TTL}"
echo ""

MC_METADATA_SERVER="http://127.0.0.1:$METADATA_PORT/metadata" \
DEFAULT_KV_LEASE_TTL=$KV_TTL \
MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES=$SSD_LIMIT \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=$TEST_DIR \
python "$(dirname "$0")/verify_hard_pin.py" --test "$TEST_NAME"
VERIFY_RESULT=$?

echo ""

# ---- 展示 Master 日志中的关键信息 ----
echo "[3/3] Master 日志关键信息:"
echo "------------------------------------------------------------"
if grep -q "Refusing allocation" "$MASTER_LOG"; then
    grep "Refusing allocation" "$MASTER_LOG" | tail -3
    echo "  ✓ SSD 水位拒绝写入（修复生效）"
else
    echo "  (未发现 'Refusing allocation')"
fi

if grep -q "Falling back" "$MASTER_LOG"; then
    echo "  ✗ 发现 'Falling back'！修复未生效"
else
    echo "  ✓ 没有 fallback"
fi

if grep -q "Memory eviction skipped.*no LOCAL_DISK" "$MASTER_LOG"; then
    grep "Memory eviction skipped" "$MASTER_LOG" | tail -2
    echo "  ✓ 驱逐保护生效"
fi

if grep -q "SSD eviction rejected" "$MASTER_LOG"; then
    grep "SSD eviction rejected" "$MASTER_LOG" | tail -2
    echo "  ✓ SSD 副本受保护"
fi
echo "------------------------------------------------------------"
echo ""
echo "完整 Master 日志: $MASTER_LOG"
echo ""

if [ $VERIFY_RESULT -eq 0 ]; then
    echo "验证通过！"
else
    echo "验证失败！"
fi

exit $VERIFY_RESULT
