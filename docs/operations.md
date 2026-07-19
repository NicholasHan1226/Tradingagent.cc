# TradingAgent V1 本地与服务器旁路运行、验收及回滚

> 本文是 A 股 V1 **simulation-only** 候选在本地与服务器旁路环境中的唯一现役操作入口。服务器sidecar只可在任务级明确授权后，以版本化、隔离、无公网切换的方式执行；该授权不自动扩展到merge/main、现役源码/API/页面切换、网络数据联调、broker、真实交易、邮件、GUI、scheduler/cron或生产密钥。仓库模板、fixture、本地测试、候选分支和服务器旁路成功均不代表 Git 主线、SharedSignals runtime 或现役生产已生效。当前授权与执行证据只见 [STATUS.md](../STATUS.md)。

## 1. 不可突破的边界

- `REAL_TRADING_ENABLED=false`；不得由环境变量或 fixture 覆盖。
- 该系统仅供 Nicholas 个人内部使用。前端/API默认只绑定`127.0.0.1`；`tradingagent.cc`远程入口必须先通过Cloudflare Access或等价单用户认证，禁止匿名公网访问和API直出。DNS、Tunnel/Pages与Access policy分别验收。
- TradingAgent 只消费显式配置的 `GET /v1/catalog` 与 `POST /v1/query` 契约；不读取 SharedSignals 数据库，不实现其服务端，不使用旧专用接口或数据商回退。
- HTTP 成功不代表数据可用。每个 dataset 独立检查 `state`、`degraded`、`freshness`、`quality`、`lineage`、`receipt_id`、`data_through`、`observed_at` 和 `reasons`；impaired state 允许后四项为 null，TA 不补造。无完整 source proof 时固定 fail closed；只有证据完整且 policy 明确允许的 impaired evidence 才可降权。
- A 股个股只允许沪深主板普通股。创业板、科创板及北京市场个股不得进入候选、预测、目标仓位、订单、成交或持仓；双创指数与全市场行业聚合只作 `context_only` 环境证据。
- 当前唯一订单决策模型是冻结的 rank-score Champion。机会雷达/append-only Ledger、多期限forecast和三风格router已是本地隔离shadow合同，只能产生反事实研究artifact，不能影响候选、rank、仓位、风险或订单。默认关闭的DeepSeek HTTPS transport已是本地候选；2026-07-18仅有一次隔离真实请求到达provider后被本地evidence schema拒绝，accepted evidence、稳定认证和生产激活仍未验证。live paper scheduler仍是计划项。
- 模拟日即使阻断新增风险，也必须尽量继续减仓/退出、对账、账本、学习到期检查和报告，并以 `completed_with_blocks` 明示结束；不得伪装成功，也不得切回旧链。

## 1.1 服务器旁路候选部署

服务器旁路部署只用于回答“冻结候选能否在目标服务器环境安装、测试、构建和运行”。它不改变现役代码、服务、定时任务、网页、路由或任何authority。每次执行都必须有独立授权、精确提交SHA和新的版本化目录；禁止把本节变成默认自动发布路径。

目录约定：

```text
/opt/investment/tradingagent                         # 现役工作树不切换；Git管理元数据仅作受控fetch/worktree登记
/opt/investment/tradingagent-candidates/<release-id> # detached候选代码
/opt/investment/tradingagent-venvs/<release-id>      # 候选专用Python环境
/opt/investment/tradingagent-canary-output/<run-id>  # fixture/canary输出
/opt/investment/release-evidence/tradingagent/<id>   # 受限发布证据
```

### 1.1.1 部署前冻结与取证

在创建候选目录前，至少保存并校验：

- 现役仓HEAD、remote ref与完整`git status --porcelain=v1 --untracked-files=all`；
- `tradingagent-front-api.service` unit、状态、PID与`127.0.0.1:8787/healthz`；
- `marketgraph`用户crontab及其哈希；
- 现役未跟踪运行资产、回滚目录和磁盘余量；
- 候选远端分支的精确SHA、工作树干净状态和回退目录。

生产仓可能包含不受Git跟踪的append-only运行证据和前端回滚副本。禁止`git clean`、`reset --hard`、覆盖式checkout或`rsync --delete`；也禁止把现役仓切到候选分支。只允许从精确SHA创建detached worktree，例如：

```bash
set -euo pipefail
umask 077

ACTIVE=/opt/investment/tradingagent
RELEASE_SHA='<approved-full-commit-sha>'
APPROVED_BRANCH='<approved-candidate-branch>'
RELEASE_ID="ta-v1-data-client-$(printf '%s' "$RELEASE_SHA" | cut -c1-7)"
CANDIDATE="/opt/investment/tradingagent-candidates/$RELEASE_ID"
VENV="/opt/investment/tradingagent-venvs/$RELEASE_ID"
EVIDENCE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$RELEASE_ID"
EVIDENCE="/opt/investment/release-evidence/tradingagent/$EVIDENCE_ID"

[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]
test ! -e "$CANDIDATE"
test ! -e "$VENV"
sudo install -d -o marketgraph -g marketgraph /opt/investment/tradingagent-candidates
sudo install -d -o marketgraph -g marketgraph /opt/investment/tradingagent-venvs
sudo install -d -m 0700 -o marketgraph -g marketgraph "$EVIDENCE"
sudo -u marketgraph git -C "$ACTIVE" fetch --no-tags origin "$APPROVED_BRANCH"
FETCHED_SHA="$(sudo -u marketgraph git -C "$ACTIVE" rev-parse FETCH_HEAD)"
test "$FETCHED_SHA" = "$RELEASE_SHA"
sudo -u marketgraph git -C "$ACTIVE" cat-file -e "$RELEASE_SHA^{commit}"
sudo -u marketgraph git -C "$ACTIVE" worktree add --detach "$CANDIDATE" "$RELEASE_SHA"
test "$(sudo -u marketgraph git -C "$CANDIDATE" rev-parse HEAD)" = "$RELEASE_SHA"
CANDIDATE_STATUS="$(sudo -u marketgraph git -C "$CANDIDATE" status --porcelain)"
test -z "$CANDIDATE_STATUS"
```

### 1.1.2 隔离安装与验收

候选使用自己的venv与`front/node_modules`，不得借用或修改现役依赖。服务器验收至少包括：

```bash
set -euo pipefail
umask 077
: "${CANDIDATE:?}" "${VENV:?}" "${EVIDENCE:?}"

MARKETGRAPH_HOME="$(getent passwd marketgraph | cut -d: -f6)"
SAFE_PATH=/opt/investment/tools/node-v24.4.1/bin:/usr/local/bin:/usr/bin:/bin
SAFE_ENV=(
  env -i
  HOME="$MARKETGRAPH_HOME"
  PATH="$SAFE_PATH"
  LANG=C.UTF-8
  TZ=Asia/Shanghai
  REAL_TRADING_ENABLED=false
  TRADINGAGENT_LLM_NETWORK_ENABLED=false
  SHAREDSIGNALS_API_URL=
  MARKETGRAPH_API_URL=
  PYTHONDONTWRITEBYTECODE=1
)

sudo -u marketgraph "${SAFE_ENV[@]}" python3 -m venv "$VENV"
sudo -u marketgraph "${SAFE_ENV[@]}" \
  "$VENV/bin/python" -m pip install -r "$CANDIDATE/requirements.txt"
sha256sum "$CANDIDATE/requirements.txt" > "$EVIDENCE/requirements.sha256"
sudo -u marketgraph "${SAFE_ENV[@]}" \
  "$VENV/bin/python" -m pip freeze > "$EVIDENCE/python-freeze.txt"

cd "$CANDIDATE"
sudo -u marketgraph "${SAFE_ENV[@]}" \
  "$VENV/bin/python" -m pytest -q
PYCACHE_ROOT="$(mktemp -d /tmp/ta-pycache.XXXXXX)"
sudo chown marketgraph:marketgraph "$PYCACHE_ROOT"
sudo -u marketgraph "${SAFE_ENV[@]}" PYTHONPYCACHEPREFIX="$PYCACHE_ROOT" \
  "$VENV/bin/python" -m compileall -q shared Ashare tools
sudo rm -rf -- "$PYCACHE_ROOT"

cd "$CANDIDATE/front"
sha256sum package-lock.json > "$EVIDENCE/package-lock.sha256"
sudo -u marketgraph "${SAFE_ENV[@]}" npm ci
sudo -u marketgraph "${SAFE_ENV[@]}" npm test
sudo -u marketgraph "${SAFE_ENV[@]}" npm run lint
sudo -u marketgraph "${SAFE_ENV[@]}" npm run build:all
sudo -u marketgraph "${SAFE_ENV[@]}" node --version > "$EVIDENCE/node-version.txt"
sudo -u marketgraph "${SAFE_ENV[@]}" npm --version > "$EVIDENCE/npm-version.txt"
```

`env -i`只保留上面白名单变量，因此不会继承`BASH_ENV`、代理、现役workspace root、SharedSignals catalog/dataset/auth或DeepSeek credential。`SHAREDSIGNALS_API_URL`与`MARKETGRAPH_API_URL`在旁路验收中必须显式为空，避免未退役旧reader把“变量缺失”解释为localhost默认地址并读取现役服务；这两个空值不是V1联调配置。依赖范围未完全锁hash时，receipt必须保存Python/pip/Node/npm版本、完整`pip freeze`、requirements与`package-lock.json`哈希；未保存这些证据不得声称复现了同一环境。

只读API canary必须使用非现役、loopback-only端口，显式保持`REAL_TRADING_ENABLED=false`，记录精确PID并在停止前核对其cmdline指向候选`dist-server`。禁止通配`pkill`或占用8787。以下生命周期在同一个fail-fast Bash进程中执行；`FINANCE_WORKSPACE_ROOT`只指向候选的显式别名，不读取现役workspace：

```bash
set -euo pipefail
umask 077
: "${CANDIDATE:?}" "${EVIDENCE:?}" "${VENV:?}" "${SAFE_PATH:?}" "${MARKETGRAPH_HOME:?}"

CANARY_PORT=18787
test "$CANARY_PORT" -ne 8787
! ss -ltn | grep -Fq "127.0.0.1:$CANARY_PORT"
CANARY_OUTPUT="/opt/investment/tradingagent-canary-output/${RELEASE_ID}-api"
WORKSPACE_LINK="$CANARY_OUTPUT/TradingAgent"
PID_FILE="$EVIDENCE/canary.pid"
SERVER_JS="$CANDIDATE/front/dist-server/server/tradingAgentSnapshotHttp.js"
NODE_BIN="$(sudo -u marketgraph env -i PATH="$SAFE_PATH" sh -c 'command -v node')"
test -f "$SERVER_JS"
test ! -e "$CANARY_OUTPUT"
sudo install -d -m 0700 -o marketgraph -g marketgraph "$CANARY_OUTPUT"
sudo -u marketgraph ln -s "$CANDIDATE" "$WORKSPACE_LINK"

CANARY_PID=''
SUDO_PID=''
cleanup_canary() {
  local stopped=0
  if [[ -n "${CANARY_PID:-}" && -r "/proc/$CANARY_PID/cmdline" ]] &&
     tr '\0' '\n' < "/proc/$CANARY_PID/cmdline" | grep -Fxq "$SERVER_JS"; then
    kill "$CANARY_PID"
    stopped=1
  fi
  if [[ "$stopped" -eq 1 && -n "${SUDO_PID:-}" ]]; then
    wait "$SUDO_PID" 2>/dev/null || true
  fi
}
trap cleanup_canary EXIT

sudo -u marketgraph env -i \
  HOME="$MARKETGRAPH_HOME" PATH="$SAFE_PATH" LANG=C.UTF-8 TZ=Asia/Shanghai \
  REAL_TRADING_ENABLED=false TRADINGAGENT_LLM_NETWORK_ENABLED=false \
  FINANCE_WORKSPACE_ROOT="$WORKSPACE_LINK" \
  TRADING_AGENT_SNAPSHOT_HOST=127.0.0.1 \
  TRADING_AGENT_SNAPSHOT_PORT="$CANARY_PORT" \
  sh -c 'set -eu; printf "%s\n" "$$" > "$1"; exec "$2" "$3"' \
  sh "$PID_FILE" "$NODE_BIN" "$SERVER_JS" \
  > "$EVIDENCE/canary-api.log" 2>&1 &
SUDO_PID=$!

for _ in $(seq 1 50); do
  test -s "$PID_FILE" && break
  sleep 0.1
done
CANARY_PID="$(cat "$PID_FILE")"
[[ "$CANARY_PID" =~ ^[0-9]+$ ]]
test -r "/proc/$CANARY_PID/cmdline"
tr '\0' '\n' < "/proc/$CANARY_PID/cmdline" | grep -Fxq "$SERVER_JS"

for _ in $(seq 1 50); do
  curl -fsS "http://127.0.0.1:$CANARY_PORT/healthz" >/dev/null && break
  sleep 0.1
done
curl -fsS "http://127.0.0.1:$CANARY_PORT/healthz" >/dev/null
curl -fsS -D "$EVIDENCE/canary-snapshot-headers.txt" \
  "http://127.0.0.1:$CANARY_PORT/api/trading-agent/snapshot" \
  > "$EVIDENCE/canary-snapshot.json"
test -r "/proc/$CANARY_PID/cmdline"
tr '\0' '\n' < "/proc/$CANARY_PID/cmdline" | grep -Fxq "$SERVER_JS"
LISTENER="$(ss -ltnp | grep -F "127.0.0.1:$CANARY_PORT")"
printf '%s\n' "$LISTENER" | grep -Fq "pid=$CANARY_PID,"
grep -Eiq '^cache-control:.*no-store' "$EVIDENCE/canary-snapshot-headers.txt"
test "$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:$CANARY_PORT/api/trading-agent/snapshot")" = 405
test "$(curl -sS -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:$CANARY_PORT/not-a-route")" = 404

"$VENV/bin/python" - "$EVIDENCE/canary-snapshot.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert payload.get("mode") == "simulated"
paper_day = payload.get("paperDayRun")
if isinstance(paper_day, dict):
    assert paper_day.get("realTradingEnabled") is False
    assert paper_day.get("productionVerified") is False

def visit(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.replace("_", "").lower()
            if normalized in {"realtradingenabled", "livetradingenabled"}:
                assert child is False
            visit(child)
    elif isinstance(value, list):
        for child in value:
            visit(child)

visit(payload)
PY

cleanup_canary
trap - EXIT
for _ in $(seq 1 50); do
  if ! ss -ltn | grep -Fq "127.0.0.1:$CANARY_PORT"; then
    break
  fi
  sleep 0.1
done
! ss -ltn | grep -Fq "127.0.0.1:$CANARY_PORT"
curl -fsS http://127.0.0.1:8787/healthz >/dev/null
```

验收至少证明：

- `GET /healthz`与只读snapshot可用，`Cache-Control: no-store`；
- 顶层`mode=simulated`且所有真实交易标志为false；
- snapshot的POST返回405，未知路由返回404；
- canary停止后备用端口无监听；
- 现役8787服务始终健康。

冻结fixture必须写到独立canary output root，至少完成同根幂等重放和跨根字节一致性检查；输出必须保持`non_authority`、`local_candidate`、`production_verified=false`、`real_trading_enabled=false`。不得写正式SampleJournal、活动runtime根或前端投影根。

### 1.1.3 最终readback与回滚

部署完成后重新读取并逐字节或逐哈希比较现役仓状态、systemd unit、crontab和健康检查；同时确认候选精确SHA、候选工作树干净、备用端口已关闭。发布receipt必须把`server_sidecar_canary`与`active_production_activated=false`明确写开，不能用“已部署”省略层级。证据目录内除最终manifest自身外的文件应生成排序后的SHA-256清单，再单独记录该清单的SHA-256；receipt至少保存候选/现役SHA、依赖版本、测试结果、canary状态、fixture状态、现役变更布尔值和未验证项。

因为sidecar从未接管现役服务，回滚只需停止候选进程并保留证据。候选worktree、venv与输出目录只有在留存期结束且获得清理授权后才可移除；不得删除现役未跟踪资产、append-only账本、模拟样本或既有回滚目录。若未来要切换现役源码/API、cron、页面或公网路由，必须重新进行独立发布授权、备份、原子切换和真实回退演练，不能沿用本次sidecar授权。

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

### 2.1 SharedSignals V1 接入验收器

轻量的 `sharedsignals_v1_gate.py` 继续负责任务启动前逐 dataset 的即时可用性门；`sharedsignals_v1_integration_probe.py` 负责首次接入、SS 发布或 catalog/profile 变化、消费者切换和故障恢复后的完整只读验收。二者均是 TA consumer，不实现或验收 SS 服务端；两者输出的 reason code 都由 TA 本地状态机推导，上游 `metadata.reasons` 自由文本只保存哈希，不能伪装成本地门禁结论或进入日志。

模板见 [sharedsignals_v1_integration_probe.example.json](examples/sharedsignals_v1_integration_probe.example.json)。模板中的 `.invalid` 地址、`fixture.*` dataset ID、catalog 与 policy 只用于说明结构，不是生产默认值。SS owner 正式交接后，应复制到仓外绝对路径并逐项替换；manifest 只允许保存 base URL 与访问策略**身份**，禁止写 API key、token、密码或其它 credential。真实认证协议尚未冻结，验收器不会自行发明 Bearer/Header 或读取 `.env` 密钥。

首批显式功能角色为：

- `trade_calendar`：交易日历，执行必需；
- `equity_master`：主板证券主数据与历史可交易状态，执行必需；
- `daily_bars`：主板日线与行级 PIT，执行必需；
- `industry_context`：全市场行业及创业板/科创板指数聚合，只作环境上下文，不允许输出双创个股。

获批只读联调时，从目标 TA 隔离工作树执行：

```bash
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export REAL_TRADING_ENABLED=false

python3 -m shared.runtime_test.sharedsignals_v1_integration_probe \
  --manifest /absolute/path/sharedsignals-integration.json \
  --output /absolute/path/evidence/sharedsignals-integration-receipt.json \
  --json
```

验收器只调用一次 `GET /v1/catalog`，随后对每个数据角色用同一显式 `as_of`、fields、filters、schema major、limit 与默认 registry order 连续执行两次 `POST /v1/query`。它复用 `DataEvidenceGate` 与 `ResearchDataProfile` 检查 metadata、source proof、精确字段投影、最小行数和行级 `event_time / available_time / revision_id / receipt_id`，并比较排除 transport request ID 后的完整响应语义哈希。缺少显式字段或出现未声明字段均阻断；后者只保存数量与字段集合哈希，避免行业聚合响应夹带个股字段或把未知字段写进回执。

当前跨页 receipt、默认排序快照和拼页 identity 仍由 SS owner 待冻结。因此任一响应出现 `next_cursor != null` 时，首版固定返回 `pagination_contract_unfrozen` 并阻断；不会抓第二页后自行拼成研究快照。SS 合同补齐前，不得通过增大 limit、截取第一页或本地排序绕过。

回执固定标注 `authority=non_authority`、`production_verified=false`、`real_trading_enabled=false`，隐藏 base URL、access policy 值、cursor、异常原文与上游自由文本 reason，只保存其 authority/config 哈希、catalog/query trace、dataset evidence、双跑一致性、PIT/内容哈希和 TA 受控 reason codes；上游 reason 原文只参与哈希。退出码为：`0=通过`、`2=数据或合同阻断`、`64=manifest/transport配置无效`、`74=回执落盘失败`。回执通过只证明该次显式只读输入满足 TA 接入合同，不证明 SS 服务端整体通过、生产 runtime 已切换、旧链 parity 已完成、每日数据持续健康或交易获授权。

未来可把该命令放在自动模拟盘启动前作为 fail-closed 前置门，但当前没有注册 scheduler/cron，也未调用任何 live SS 地址。每次 catalog/dataset/schema、PIT 字段或 access policy identity 变化都必须生成新 manifest 与新回执，不能复用旧 PASS。

DeepSeek 已有默认关闭的官方HTTPS transport本地候选；以下仍是安全默认，不会联网：

`TRADINGAGENT_LLM_API_KEY_ENV`只能取固定值`DEEPSEEK_API_KEY`；它不是让系统选择任意密钥变量的开关。任意模型映射只允许作为`fixture_only`离线测试路由，不能替代严格配置或授权网络出口。

```bash
export TRADINGAGENT_LLM_PROVIDER=deepseek
export TRADINGAGENT_LLM_BASE_URL=https://api.deepseek.com
export TRADINGAGENT_LLM_API_KEY_ENV=DEEPSEEK_API_KEY
export TRADINGAGENT_LLM_FLASH_MODEL=deepseek-v4-flash
export TRADINGAGENT_LLM_PRO_MODEL=deepseek-v4-pro
export TRADINGAGENT_LLM_NETWORK_ENABLED=false
```

默认Gateway不会读取`DEEPSEEK_API_KEY`值，也不会自行安装HTTP transport。只把`TRADINGAGENT_LLM_NETWORK_ENABLED`改成`true`会因缺少进程内显式授权而fail closed；ambient `DEEPSEEK_API_KEY`也不会被transport读取。HTTP候选必须同时满足两个独立门：

1. `DeepSeekProviderConfig.from_environment(..., allow_network_transport=True)`显式批准validated router；
2. 调用方显式构造`DeepSeekHTTPTransportConfig(network_enabled=True, credential=DeepSeekCredentialFile(...))`并注入精确的`DeepSeekHTTPTransport`类型。

这两步只完成安全装配，不授权直接调用transport。公开`DeepSeekHTTPTransport.send(...)`以及脱离`LLMEvidenceGateway`的HTTP Adapter调用固定在读取credential/创建socket前失败；只有Gateway完成request/source proof、Prompt注入、全树DLP和router authority复核后，才会内部铸造以进程内HMAC绑定全部关键字段的验证egress capability并进入wire path。

任何后续网络canary取得新的独立授权后，装配形状固定如下；本段是代码合同，不是可直接复制执行的运行命令：

```python
from pathlib import Path

from shared.llm import (
    DeepSeekCredentialFile,
    DeepSeekHTTPTransport,
    DeepSeekHTTPTransportConfig,
    DeepSeekProviderConfig,
)

provider_config = DeepSeekProviderConfig.from_environment(
    {"TRADINGAGENT_LLM_NETWORK_ENABLED": "true"},
    allow_network_transport=True,
)
transport = DeepSeekHTTPTransport(
    DeepSeekHTTPTransportConfig(
        network_enabled=True,
        credential=DeepSeekCredentialFile(
            Path("<absolute-path-to-rotated-raw-secret>")
        ),
    )
)
```

raw-secret必须是显式绝对路径、当前进程euid所有的regular file，禁止symlink且link count必须为1，权限只能为`0400`或`0600`，ASCII内容必须是单一`sk-...`值且不超过512 bytes；不得有换行、NUL、`=`或`DEEPSEEK_API_KEY=`前缀。任何在聊天、工单、日志或提交中暴露过的key都必须先在供应商侧废止并轮换。历史服务器候选中的env格式秘密文件只保留为旧证据，不能直接复用为raw-secret。新值不得写入仓库、`.env.example`、测试、RunBundle、receipt或文档。

密钥父路径也是安全边界：客户端使用目录descriptor逐级打开，拒绝任一symlink父目录、非root/当前进程用户所有目录以及group/world-writable目录，避免最终文件合格但父目录可被替换。

transport固定`POST https://api.deepseek.com/chat/completions`，使用系统TLS验证，禁环境代理、重定向、自动重试和fallback。禁止把上面的`transport`对象直接作为通用HTTP客户端；它只能注入`LLMEvidenceGateway`的DeepSeek Adapter。2026-07-18一次旧A股v1 Prompt的隔离真实请求到达HTTP 200 provider envelope，但evidence binding以`llm_evidence_schema_invalid`失败；没有accepted `ProviderTransportReceipt`、Journal或生产切换。该历史canary早于当前`ProviderRejectedAttemptReceipt`，不得追溯包装为新typed receipt。A股v2只通过离线fixture合同测试，没有进行第二次真实调用。

后续真实canary必须使用`LLMEvidenceGateway.analyze_with_provenance()`并通过显式`LLMEvidenceProvenanceRecorder`路由，固定单次请求、稳定request ID、无应用层retry、无fallback，失败后不得自动补发。调用方只选择仓外受限目录中的显式绝对accepted锚点，再用`llm_provenance_journal_paths()`确定性得到accepted、rejected和provider-invocation三条路径；禁止另配invocation锁、相对路径或自定义伴随路径。三条Journal及`.head`共六个端点不得相同或互为Unicode NFC/NFD、大小写、真实路径或物理文件别名，recorder的source verifier必须与DeepSeek Adapter绑定同一对象。invocation Journal以不包含调用方request ID的逻辑内容键在网络前先落`in_flight`，并持有跨进程锁直到唯一`accepted/rejected/no_receipt`终态提交；同一canonical family内的逻辑内容轮换ID或同ID异内容均在副作用前fail closed。Gateway返回前会重算原request/source/material摘要并精确复核canonical observation字段集；额外字段、元数据重绑和内容hash漂移均拒绝。若精确HTTPS路径已验证HTTP 200、MIME/JSON和provider envelope，但evidence schema或Gateway observation binding失败，只可把`ProviderRejectedAttemptReceipt`的脱敏descriptor写入独立rejected audit Journal；不得保存Prompt、响应正文、parsed/normalized evidence、credential或credential fingerprint。该回执固定`audit_only=true`、`evidence_journal_eligible=false`且全部authority为false，绝不能写入accepted Journal、样本、成熟度或交易链。三类Journal都要求single-link、当前euid、`0600`及path/FD inode一致；任一readback、CAS、身份或持久化失败必须在provider调用前阻断，或把已得到的available观察降为invalid，不得旁路返回。只有已持久化唯一终态的同一request ID+内容可复用本地观察且不再次调用provider；若provider调用后进程中断且没有可验证终态，`in_flight`保持未知并禁止自动补发，必须人工裁决。三类回读都只消费非权威、深层不可变的descriptor校验视图，不得重建typed HTTPS receipt。其它网络、协议、DLP、敏感输出或前置门禁失败不伪造rejected receipt。任何后续真实网络启用仍是新的独立授权与验收任务；本地Journal不构成生产durable authority。

最小装配合同如下；这里的路径和ID是占位符，不是已部署配置：

```python
accepted_path, rejected_path, invocation_path = llm_provenance_journal_paths(
    Path("/absolute/restricted/llm-evidence.jsonl")
)
accepted = LLMEvidenceJournal(accepted_path)
rejected = LLMRejectedAttemptAuditJournal(rejected_path)
invocations = LLMProviderInvocationJournal(invocation_path)
recorder = LLMEvidenceProvenanceRecorder(
    accepted_journal=accepted,
    rejected_attempt_journal=rejected,
    provider_invocation_journal=invocations,
    source_authority_verifier=adapter.source_authority_verifier,
)
observation = debate(
    symbol,
    scores,
    gateway=gateway,
    artifacts=artifacts,
    provenance_recorder=recorder,
    request_id=f"LLM-DEBATE-{immutable_decision_id}",
)
```

`immutable_decision_id`必须由调用方已有的不可变run/decision identity确定性提供；同一次逻辑请求重试时保持不变，payload、artifact或route变化时必须生成新ID。它不是唯一幂等门：recorder还会以不依赖该ID的逻辑内容键拒绝换ID重发。所有worker还必须使用同一accepted锚点；canonical family检查能拒绝单个recorder内部错配，但当前尚无production runtime启动证明，不能由本地合同推断跨主机唯一调用。当前paper composition尚未装配这条provider路径，示例不代表scheduler、网络、服务器或production authority已启用。

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
7. DeepSeek若启用，会话中曾暴露的credential必须先由供应商侧revoke/rotate，新值不得入仓；还需真实模型/请求字段readback、quota/限流/幂等/数据留存核验、敏感数据门、提示注入语义/编码变体、引用绑定、typed receipt持久化、成本/延迟和冻结增量评测，且仍保持evidence-only。首版固定单次调用、无自动重试；未来是否保留或变更该策略必须另立评审，不能在运行时静默开启；
8. 独立发布授权、preflight、回退方案，以及本地、Git、远端、生产文件、生产 runtime 和外部路由分别验收。

当前服务器sidecar没有提供上述外部authority证据，因此业务能力仍只能是`local_isolated_candidate / simulation-only / nonpromotion`；`server_validated_non_authority_simulation_only`只描述目标服务器环境的安装与旁路运行证据，不能提升为现役生产状态。
