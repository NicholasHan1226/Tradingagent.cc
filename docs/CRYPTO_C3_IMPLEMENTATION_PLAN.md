# Crypto C3 实现计划（退役快照）

> **Retired 2026-08-22.** 本文件保留早期问题拆解与负结果，不再是活动任务、
> 模拟准入门禁或架构前置。当前执行队列只看 `BACKLOG.md`；成熟路径只看
> `EVOLUTION_PROGRAM.md`。不得先建设 Registry API、服务或平台来阻塞已有完整
> segment 的 delayed-paper、费用后基线和 resolved-outcome 积累。

## 目标

实现 evidence-bound registry/receipt authority，让 Crypto 从只读证据待处理语义迁移到科学的模拟优先演进控制器。

## 当前状态

### 已实现
- ✅ Champion promotion receipt 生成（`_champion_promotion_receipt`）
- ✅ Promotion 写入 `champion_promotions/` 目录
- ✅ 内容寻址、不可变的 receipt 文件

### 待实现
- ❌ Champion/Challenger registry（注册表）
- ❌ Demotion（降级）机制
- ❌ Deterministic rollback（确定性回滚）
- ❌ Registry 查询 API

## 实现计划

### 阶段 1：Registry 数据结构设计

**目标**：设计 append-only registry，跟踪所有 champion/challenger 历史

**数据结构**：
```python
# Crypto/registry.py
class ChampionRecord:
    champion_id: str  # content-addressed ID
    strategy_id: str
    symbol: str
    promoted_at: str  # ISO timestamp
    receipt_sha256: str  # promotion receipt 的 SHA256
    evidence_summary: dict  # 晋级时的证据摘要
    status: str  # "active" | "demoted" | "retired"
    demoted_at: str | None
    demotion_reason: str | None

class CryptoChampionRegistry:
    """Append-only registry for Crypto champions"""
    
    def register_champion(self, record: ChampionRecord) -> None:
        """Register a new champion"""
        
    def get_active_champion(self, symbol: str, strategy_type: str) -> ChampionRecord | None:
        """Get active champion for symbol/strategy"""
        
    def get_champion_history(self, symbol: str, strategy_type: str) -> list[ChampionRecord]:
        """Get all champion records for symbol/strategy"""
        
    def demote_champion(self, champion_id: str, reason: str) -> None:
        """Demote a champion"""
```

**存储位置**：`shared/review/crypto/champion_registry.jsonl`

### 阶段 2：Demotion 机制

**目标**：实现基于证据的自动降级

**降级条件**：
1. 连续 N 个评估周期表现低于阈值
2. 新的 challenger 显著优于当前 champion
3. 证据质量下降（数据缺失、样本不足）

**实现**：
```python
# Crypto/demotion.py
class DemotionEvaluator:
    def should_demote(self, champion: ChampionRecord, latest_evidence: dict) -> tuple[bool, str]:
        """Evaluate if champion should be demoted"""
        
    def evaluate_performance_decay(self, champion: ChampionRecord) -> tuple[bool, str]:
        """Check if performance has decayed significantly"""
        
    def evaluate_better_challenger(self, champion: ChampionRecord, challengers: list) -> tuple[bool, str]:
        """Check if a challenger is significantly better"""
```

### 阶段 3：Deterministic Rollback

**目标**：实现可重现的回滚机制

**实现**：
```python
# Crypto/rollback.py
class RollbackManager:
    def rollback_to_champion(self, champion_id: str) -> RollbackReceipt:
        """Rollback to a specific champion version"""
        
    def generate_rollback_receipt(self, from_id: str, to_id: str) -> RollbackReceipt:
        """Generate immutable rollback receipt"""
        
    def validate_rollback(self, receipt: RollbackReceipt) -> bool:
        """Validate rollback receipt integrity"""
```

### 阶段 4：集成到评估流程

**目标**：将 registry 集成到 `ten_symbol_factor_strategy_evaluation.py`

**修改点**：
1. 在 `_champion_promotion_receipt` 后，调用 `registry.register_champion()`
2. 在评估开始时，调用 `demotion_evaluator.should_demote()`
3. 如果需要降级，调用 `registry.demote_champion()`
4. 生成 demotion receipt

### 阶段 5：测试和验证

**目标**：确保 registry 的正确性和可重现性

**测试**：
1. Registry append-only 特性
2. Promotion/demotion 流程
3. Rollback 可重现性
4. Receipt 完整性验证

## 实现顺序

1. **阶段 1**：Registry 数据结构（1-2 天）
2. **阶段 2**：Demotion 机制（1-2 天）
3. **阶段 3**：Rollback 机制（1 天）
4. **阶段 4**：集成到评估流程（1 天）
5. **阶段 5**：测试和验证（1-2 天）

**总时间**：5-8 天

## 风险和挑战

1. **数据迁移**：需要将现有的 champion_promotions 迁移到 registry
2. **向后兼容**：需要确保不影响现有的评估流程
3. **性能**：Registry 查询需要高效
4. **测试覆盖**：需要全面的测试覆盖

## 下一步

1. 确认实现计划
2. 开始阶段 1：实现 Registry 数据结构
3. 编写单元测试
4. 集成到评估流程
