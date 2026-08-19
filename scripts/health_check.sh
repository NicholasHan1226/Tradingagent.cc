#!/usr/bin/env bash
# TradingAgent 健康检查脚本
# 用于检查服务器和服务的运行状态

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=== TradingAgent 健康检查 ==="
echo ""

# 检查 front-api 服务
echo "1. 检查 front-api 服务..."
if systemctl is-active --quiet tradingagent-front-api.service; then
    echo -e "${GREEN}✓ front-api 服务运行正常${NC}"
else
    echo -e "${RED}✗ front-api 服务未运行${NC}"
fi

# 检查健康端点
echo ""
echo "2. 检查健康端点..."
HEALTH_RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8787/healthz 2>/dev/null || echo "000")
if [ "$HEALTH_RESPONSE" = "200" ]; then
    echo -e "${GREEN}✓ 健康检查通过 (HTTP 200)${NC}"
else
    echo -e "${RED}✗ 健康检查失败 (HTTP $HEALTH_RESPONSE)${NC}"
fi

# 检查系统资源
echo ""
echo "3. 检查系统资源..."
CPU_LOAD=$(cat /proc/loadavg | awk '{print $1}')
CPU_CORES=$(nproc)
CPU_THRESHOLD=$(echo "$CPU_CORES * 0.8" | bc)

if (( $(echo "$CPU_LOAD < $CPU_THRESHOLD" | bc -l) )); then
    echo -e "${GREEN}✓ CPU 负载正常 ($CPU_LOAD / $CPU_CORES cores)${NC}"
else
    echo -e "${YELLOW}⚠ CPU 负载较高 ($CPU_LOAD / $CPU_CORES cores)${NC}"
fi

MEMORY_USAGE=$(free | grep Mem | awk '{printf "%.1f", $3/$2 * 100}')
if (( $(echo "$MEMORY_USAGE < 80" | bc -l) )); then
    echo -e "${GREEN}✓ 内存使用正常 ($MEMORY_USAGE%)${NC}"
else
    echo -e "${YELLOW}⚠ 内存使用较高 ($MEMORY_USAGE%)${NC}"
fi

DISK_USAGE=$(df / | tail -1 | awk '{print $5}' | sed 's/%//')
if [ "$DISK_USAGE" -lt 80 ]; then
    echo -e "${GREEN}✓ 磁盘使用正常 ($DISK_USAGE%)${NC}"
else
    echo -e "${YELLOW}⚠ 磁盘使用较高 ($DISK_USAGE%)${NC}"
fi

# 检查 Timer 任务
echo ""
echo "4. 检查 Timer 任务..."
TIMER_COUNT=$(systemctl list-timers --all | grep -c "tradingagent" || echo "0")
echo -e "${GREEN}✓ 运行中 Timer 任务: $TIMER_COUNT${NC}"

# 检查部署版本
echo ""
echo "5. 检查部署版本..."
if [ -f /opt/investment/releases/tradingagent/current/.deployed-sha ]; then
    DEPLOYED_SHA=$(cat /opt/investment/releases/tradingagent/current/.deployed-sha)
    echo -e "${GREEN}✓ 当前部署版本: ${DEPLOYED_SHA:0:8}${NC}"
else
    echo -e "${RED}✗ 无法读取部署版本${NC}"
fi

echo ""
echo "=== 健康检查完成 ==="
