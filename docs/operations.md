# TradingAgent V1 本地运行、验收与回滚

> 本文是 A 股 V1 **本地隔离、simulation-only** 候选的唯一现役操作入口。当前用户授权只允许把已验证候选提交并推送到隔离分支；仍不授权网络联调、merge/main、broker、真实交易、邮件、GUI、scheduler/cron、生产密钥或部署。仓库模板、fixture、本地测试和候选分支成功均不代表 Git 主线、SharedSignals runtime 或生产已生效。当前证据见 [STATUS.md](../STATUS.md)。

## 1. 不可突破的边界

- `REAL_TRADING_ENABLED=false`；不得由环境变量或 fixture 覆盖。
- 该系统仅供 Nicholas 个人内部使用。前端/API默认只绑定`127.0.0.1`；`tradingagent.cc`远程入口必须先通过Cloudflare Access或等价单用户认证，禁止匿名公网访问和API直出。DNS、Tunnel/Pages与Access policy分别验收。
- TradingAgent 只消费显式配置的 `GET /v1/catalog` 与 `POST /v1/query` 契约；不读取 SharedSignals 数据库，不实现其服务端，不使用旧专用接口或数据商回退。
- HTTP 成功不代表数据可用。每个 dataset 独立检查 `state`、`degraded`、`freshness`、`quality`、`lineage`、`receipt_id`、`data_through`、`observed_at` 和 `reasons`；impaired state 允许后四项为 null，TA 不补造。无完整 source proof 时固定 fail closed；只有证据完整且 policy 明确允许的 impaired evidence 才可降权。
- A 股个股只允许沪深主板普通股。创业板、科创板及北京市场个股不得进入候选、预测、目标仓位、订单、成交或持仓；双创指数与全市场行业聚合只作 `context_only` 环境证据。
- 当前唯一订单决策模型是冻结的 rank-score Champion。机会雷达/append-only Ledger、多期限forecast和三风格router已是本地隔离shadow合同，只能产生反事实研究artifact，不能影响候选、rank、仓位、风险或订单。真实DeepSeek transport和live paper scheduler仍是计划项。
- 模拟日即使阻断新增风险，也必须尽量继续减仓/退出、对账、账本、学习到期检查和报告，并以 `completed_with_blocks` 明示结束；不得伪装成功，也不得切回旧链。

## 2. 安全环境与显式配置

从目标隔离 worktree 根目录运行：

```bash
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export REAL_TRADING_ENABLED=false
```

V1 不提供 SharedSignals 默认地址。仅在上游合同真正冻结并由获批联调任务提供时，才可显式设置：

```bash
export SHAREDSIGNALS_API_URL='<explicit-http-or-https-base-url>'
export SHAREDSIGNALS_CATALOG_VERSION='<explicit-frozen-catalog-version>'
export SHAREDSIGNALS_ACCESS_POLICY_ID='<explicit-read-only-policy-id>'
export SHAREDSIGNALS_MARKET_PULSE_DATASET_IDS_JSON='<explicit-market-to-dataset-json>'
export SHAREDSIGNALS_SCHEMA_MAJOR='<explicit-positive-schema-major>'
export SHAREDSIGNALS_RUNTIME_TRANSPORT='http-json-v1'
```

缺任一配置时保持 unavailable；不得猜测 localhost、生产地址、catalog version、schema major 或 dataset ID。`http-json-v1` 只表示显式 TA consumer transport，拒绝 30x 重定向，且不能解除未迁移业务 reader 的 retirement block；当前不授权配置或运行 live endpoint。

DeepSeek 当前仅登记2026-07-16已从官方公开文档核对的路由目标，且网络固定关闭：

`TRADINGAGENT_LLM_API_KEY_ENV`只能取固定值`DEEPSEEK_API_KEY`；它不是让系统选择任意密钥变量的开关。任意模型映射只允许作为`fixture_only`离线测试路由，不能替代严格配置或授权网络出口。

```bash
export TRADINGAGENT_LLM_PROVIDER=deepseek
export TRADINGAGENT_LLM_BASE_URL=https://api.deepseek.com
export TRADINGAGENT_LLM_API_KEY_ENV=DEEPSEEK_API_KEY
export TRADINGAGENT_LLM_FLASH_MODEL=deepseek-v4-flash
export TRADINGAGENT_LLM_PRO_MODEL=deepseek-v4-pro
export TRADINGAGENT_LLM_NETWORK_ENABLED=false
```

本地候选不会读取`DEEPSEEK_API_KEY`值，也没有HTTP transport；只接受同时绑定request与最终outbound hash的显式离线fixture transport。官方文档核对不替代认证`/models` readback或真实canary。任何在聊天、工单、日志或提交中暴露过的key都必须先在供应商侧废止并轮换，新的值只能注入未跟踪的本机环境，不能写入`.env.example`、测试、RunBundle或文档。启用真实network transport属于新的独立授权与验收任务，不得通过把`TRADINGAGENT_LLM_NETWORK_ENABLED`改成`true`绕过。

## 3. 唯一聚焦候选检查

测试清单的唯一事实源是 [`tests/ta_v1_candidate_manifest.txt`](../tests/ta_v1_candidate_manifest.txt)。在仓库根执行：

```bash
export REAL_TRADING_ENABLED=false
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest \
  $(sed '/^#/d;/^$/d' tests/ta_v1_candidate_manifest.txt) -q
```

该命令只验证本地合同、fixture、故障负例与文档防漂移；它不访问真实上游，不制造真实市场样本。完整冻结前还必须执行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q
python3 -m compileall -q shared Ashare tools
python3 -m ruff check shared Ashare tools tests
python3 -m ruff format --check shared Ashare tools tests
git diff --check

cd front
npm test
npm run lint
npm run build:all
```

若本机没有项目声明的工具依赖，报告“未运行及原因”，不得用较小检查替代完整检查并宣称通过。

## 4. 离线 fixture 闭环

当前唯一可执行入口是冻结 fixture 的本地、非权威 composition；它不是通用 paper-day CLI、实时模拟盘或 scheduler：

```bash
export REAL_TRADING_ENABLED=false
OUTPUT_ROOT="$(mktemp -d /private/tmp/ta-phase1-paper-fixture.XXXXXX)"

python3 tools/run_phase1_paper_fixture.py \
  --fixture tests/fixtures/phase1_paper/paper_day.json \
  --output-root "$OUTPUT_ROOT" \
  --real-trading-enabled false

python3 - "$OUTPUT_ROOT" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]) / "shared" / "runtime_test" / "phase1_paper_fixture"
latest = root / "run_bundles" / "latest.json"
raw = latest.read_bytes()
payload = json.loads(raw)
assert payload["_projection"]["environment"] == "local_candidate"
assert payload["_projection"]["production_verified"] is False
assert payload["context"]["real_trading_enabled"] is False
print(latest)
print(hashlib.sha256(raw).hexdigest())
PY
```

用同一 fixture 与 output root 重跑，必须获得稳定 run/bundle identity、字节稳定投影与 `idempotent=true`，且不得重复追加 ledger 事件；用两个不同真实 output root 运行时，业务 bundle SHA 与最终 artifact bytes 也必须相同，只有 CLI 顶层可操作绝对路径不同。输出只允许位于显式临时 root；macOS 应使用真实 `/private/tmp`，因为 `/tmp` 是 symlink 并会被安全门拒绝。仓库、项目根、正式 review/runtime 根及其 symlink 别名必须被拒绝。

fixture 记录必须同时满足：

- `source_class=fixture`；
- `promotion_eligible=false`；
- `network_enabled=false`、`live_execution_enabled=false`、`real_trading_enabled=false`；
- 数据、Universe、资本、成本、漂移和模型 receipt 均内容绑定；
- 不写正式 SampleJournal，不发布到前端活动根，不计入正期望、概率校准或晋级样本。

### 4.1 Shadow机会、forecast与三风格router

仓库当前没有把`OpportunityRadar -> forecast -> style router`接入现役day loop的命令，也不得新增临时脚本绕过这条隔离。它们只能由对应contract tests构造内容寻址fixture，输出必须保持`shadow_only/nonpromotion/no position effect/no order effect`。尤其禁止：

- 把旧`funnel_events.jsonl`当作OpportunityLedger，或让旧writer重新排入cron；
- 把未校准quantile/hazard score命名成概率；
- 把detached calibrated research artifact回写原forecast或Champion；
- 把`open_candidate` shadow intent转换成`TargetPosition`、`TradeIntent`或订单；
- 用多个style重复计算同一evidence group，或在冲突时取消abstain。

只有未来独立任务完成真实shadow publisher、只读投影、冻结样本验收和consumer合同变更后，才可登记运行命令；这仍不等于进入订单链。

### 4.2 A股 forward-label / sample-ops 计划门（合同检查，不是现役 V1 runner）

A股 forward-label CLI 与 sample-ops CLI 合同都要求：

```text
--validation-plan-path <externally-created-ashare-validation-plan-v1.json>
```

该文件必须是预测前由外部calendar verifier生成并冻结的内容寻址`ashare_validation_plan_v1` artifact。CLI只加载、重建并校验canonical payload、plan/calendar/proof SHA和时点绑定；它不会调用verifier、不会自签proof，也不会从当天bar生成交易会话。缺参数、symlink、缺proof、hash漂移或非canonical payload时，会在读取行情前阻断。

这两个模块当前仍位于`runtime_test`且默认reader尚在旧消费者退役清单中，所以不得把参数合同写成已接通SS V1的实时运行入口，也不得对默认review/Journal根执行。现阶段只允许通过注入reader的测试或显式隔离fixture验证；待同`as_of` V1 cutover、受信artifact registry和生产calendar readback完成后，才能另行登记scheduler命令。顶层fixture tier、`production_eligible=false`和内容hash都不能自行证明calendar来源真实。

## 5. 每个模拟日的验收顺序

固定阶段为：

```text
preopen
-> evidence_ready
-> universe_ready
-> decision_ready
-> risk_checked
-> orders_simulated
-> reconciled
-> learning_recorded
-> reported
```

逐层检查：

1. `decision_as_of` 带时区，并与 `trade_date` 的 `Asia/Shanghai` 交易日一致。
2. SS V1 请求包含必填 `schema_major`；`order` 省略时由 registry 默认排序。catalog、逐 dataset metadata 与 receipt 逐项验证；不可用数据和 null source proof 不能被其它健康 dataset 洗白。
3. CoverageReceipt 的分母、taxonomy、有效时间、来源 generation/receipt/hash 经外部注入 verifier 复核；缺 verifier 时只能 `partial_market + degraded`。
4. 账户可交易池只含主板普通股；市场环境可含双创指数和全市场行业聚合，但始终 `context_only`。
5. 小账户计划绑定50,000 CNY policy、独立账户proof、买入整手/卖出零股例外、持仓/T+1、模拟费用、现金顺序、最少经济订单、无交易区与authority generation；本地逻辑重算positions/gross/content hash、费用和计划数值。Champion score必须绑定当前selection manifest、artifact/model/spec及经独立port复核的数值PIT特征快照，rank只排序且不参与sizing。fixture verifier只证明所给输入的绑定；canonical-capital测试路径从同一模拟ledger head派生并复读current generation/lineage。两者都不证明真实账户、Champion registry、feature authority或broker事实。
6. 六维论点风险必须显式注入人工复核policy、逐候选/持仓/pending detached proof与完整exposure-set proof；运行时无默认verifier且不得自签。当前持仓和所有open/increase pending预约必须先进入pre exposure，pending卖出不得重复计入；同一股票candidate、position与pending group必须连续。optimizer与day loop分别复算每笔notional变化和最终`industry/thesis/raw_material/policy_event/crowding/model_family` exposure map，day loop另把重签plan中的group绑定回权威receipt。超cap只阻断open/increase，合法reduce/exit继续；缺失、重复、过期、篡改、改换group、重新签名或跨决策归零均fail closed。外层stage不可晋级不能掩盖嵌套proof为可晋级。当前仅有不可晋级fixture authority，不是生产行业分类、pending book或上限readback。
7. 非空持仓mark与非空订单quote必须嵌入精确`MarketEvidenceAuthority`，绑定dataset/catalog/source receipt/lineage、calendar receipt、capital generation、execution lineage与时点；fixture verification hash只证明本地内容绑定，不是签名或live市场authority。执行port还必须显式注入`TrustedExecutionClock`，并在`sim_submit`和`capital_commit`紧邻副作用前分别重新验证quote freshness/session；commit时钟通过后、账务写入前再次复核drift/Champion authority。所有证据与副作用时点保留原始微秒精度，模拟fill/terminal使用submit副作用时点，commit不得早于submit；任何TOCTOU、时钟倒退或跨交易日异常都保留坏reading、释放预约且不提交capital ledger/outbox。日循环与对账端复用同一session/30秒TTL和严格失败合同；`not_committed`必须先重证`data_through <= available <= execution <= submit`、submit时quote仍在30秒内且execution session匹配声明，再按唯一原因优先级复核精确terminal，并再次验证`quote <= submit <= fill/terminal <= commit <= reconcile`。它只接受明确的commit前市场失效、零成交、完整残量、无fill/commit ID且释放语义一致的回执。commit后、settlement前崩溃时，只有pending intent/receipt seed与canonical ledger中同一commit完全绑定且commit API返回幂等，才先恢复settlement；intent-before-commit仍服从当前收紧门。当前没有默认或生产时钟，也没有把最终authority复核与未来外部账务提交原子化的生产机制。
8. Risk 输入不得预带当前或legacy capital reservation字段。`open/increase`预约证明必须由本轮wrapper生成，execution拒绝买单夹带legacy别名；`reduce/exit`在risk与execution两层均禁止携带预约字段，卖出失败不会释放预约。买入零成交释放前要向canonical ledger验证同一run/order/reference、reservation event、authority/generation、execution lineage、risk unit与lineage，并要求订单reserved cash/exposure等于canonical完整剩余值；首次释放还必须通过effect guard。释放后精确event必须立即使预约`terminal=true`且remaining cash/exposure/margin全零；幂等重放只恢复同一reference的既有终态event。对账以回执中的预约证明把ledger重放到精确release event，逐项核对金额、原因和reference，并拒绝部分释放或依靠后续事件才归零的预约。任何不匹配在写入close reconcile前fail closed。
9. 漂移指标必须同时具有 metrics v2 artifact 与 detached verification receipt；本地verifier固定implementation trust root并复核完整artifact/receipt、label/cost snapshot、window/horizon/regime、journal/model及source receipts，producer 自报 lineage 不可用。该hash不是签名，也不替代真实独立metrics重算。漂移latch只能保持或收紧；每笔reserve、sim submit、capital commit和reservation release前都重读最新drift与Champion authority。open/increase被阻断时，合法reduce/exit、必要reservation清理、reconcile和report仍继续。
10. 每个候选都写入 Decision Ledger：`PAPER_FILLED`、`PAPER_NOT_FILLED`、`REJECTED` 或 `OBSERVATION_ONLY`，不得只保存成交。
11. RunBundle 与最新投影读回重算 hash；临时文件、中断写、跨 run order identity 或不一致 receipt 必须 fail closed。

## 6. `completed_with_blocks` 与恢复

以下情况至少阻断新增风险，但不应中止审计闭环：

- 单个或多个 dataset degraded/stale/failed；
- 全市场覆盖 authority 未验证或聚合缺口；
- 资本、持仓、费用或漂移 authority 无法证明；
- Champion selection、数值PIT特征、论点风险policy/exposure set、market evidence或trusted clock无法证明；
- 模型证据过期、OOD、校准恶化或有效样本不足；
- 订单因T+1、买入整手/卖出零股规则、现金、费用、流动性或硬风险不可行。

恢复流程：

```text
冻结新增风险
-> 保存当前不可变事实与 reason codes
-> 修复或等待权威证据
-> 使用同一 run identity 做幂等重放
-> 对账与投影读回
-> 仅由显式人工复核解除负向 latch
```

不得删除 append-only 事实、手改投影、修改历史 receipt、自动清除 quarantine，或借“恢复服务”扩大风险。

## 7. 旧代码退役

旧 A 股 adapter、数据 reader、screening/research、runtime-test 与 wrapper 只作为 time-boxed 迁移证据。旧机会漏斗writer同样已经退役并固定退出78，且不在仓库cron模板中；两个历史JSONL路径只允许冻结法证读取，不能驱动current readiness或实时心跳。现役 V1 不调用这些旧入口；对应旧 wrapper不能由环境恢复。

每批退役都按同一顺序完成：

```text
登记消费者与 owner
-> 新旧同 as_of 只读 parity
-> 切换一个边界清晰的消费者
-> 验证 V1 失败时无 fallback
-> 同批删除旧 import / URL / env / wrapper / test / doc 引用
-> 更新 legacy inventory、机器状态、STATUS 与文档
```

旧链失败或新链未冻结时停止新增风险，不恢复兼容路径。历史细节从 Git 与冻结证据审计，不在现役操作文档复制旧命令。

## 8. 发布前的外部阻塞

即使本地全部通过，以下证据缺一不可：

1. SharedSignals owner 冻结的 base URL、catalog version、dataset IDs、auth/receipt authority 和 live readback；
2. 所有 A 股消费者的同 `as_of` parity、V1 cutover、旧引用清零和 runtime no-fallback 负例；
3. 每个predictive dataset的首次可见时间、release/revision链、first-seen receipt和训练时vintage；无法还原历史回填版本的数据不得进入历史训练；
4. PIT证券主数据覆盖上市/退市、板块迁移、ST/风险警示、停复牌和历史指数/行业成员，证明没有用当前存续集合回填过去Universe；
5. 生产market-evidence verifier、Champion/数值特征registry verifier、独立metrics重算authority与长驻可信时钟，以及真实交易会话中的自动模拟盘、crash/restart、对账和 20 个以上交易日运行证据；
6. 60–120 个交易日影子/模拟观察、费用后统计置信度、回撤与状态分层；
7. DeepSeek若启用，会话中曾暴露的credential必须先由供应商侧revoke/rotate，新值不得入仓；还需真实模型/请求字段readback、quota/限流/重试/幂等/数据留存核验、敏感数据门、提示注入语义/编码变体、引用绑定、typed receipt持久化、成本/延迟和冻结增量评测，且仍保持evidence-only；
8. 独立发布授权、preflight、回退方案，以及本地、Git、远端、生产文件、生产 runtime 和外部路由分别验收。

本轮没有这些证据，因此状态只能是 `local_isolated_candidate / simulation-only / nonpromotion`。
