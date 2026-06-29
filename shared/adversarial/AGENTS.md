# adversarial/

## 目标
多空对辩 + 压力测试 + 历史类比。对每只候选股做对抗式质疑, 输出 belief_score (0-1)。
不排除标的, 仅调整仓位权重。

## 文件
- `bull_bear_debate.py` — LLM 驱动的多空对辩。bull case 来自六维打分, bear case 来自风险/估值陷阱/负向信号。输出 {bull_case, bear_case, belief_score}。
- `stress_test.py` — 情景压力测试。三情景: regime 反转 / 板块逻辑证伪 / 大盘 -10%。输出 {scenario, max_drawdown, recovery_time}。
- `historical_analogy.py` — 历史相似条件检索。从 memory/global/event_analogies.jsonl 读取, 返回 {date, outcome, return} 列表。

## 原则
- belief_score ∈ [0, 1], 默认 0.5 (中性)
- 不排除标的, belief_score 仅影响 position_sizer 的权重
- bear case 必须覆盖: 估值陷阱 / 逻辑证伪 / 流动性风险 / 政策反转
- 压力测试用 worst-case, 不用 expected-case
- 历史类比不预测, 只提供先验分布参考

## 接口
```python
from adversarial.bull_bear_debate import debate
from adversarial.stress_test import stress_test
from adversarial.historical_analogy import find_analogies
```

## 依赖
- DeepSeek API (DEEPSEEK_API_KEY env)
- memory/global/event_analogies.jsonl
