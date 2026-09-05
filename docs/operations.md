# TradingAgent 多市场 fixture 与服务器旁路运行、验收及回滚

> 本文是 **simulation-only** 本地、服务器旁路及既有内部运行服务的操作入口；CNFutures 当前暂停，不据本文启用。正常发布的 standing authorization 及例外以 [项目规则](../AGENTS.md#验收与发布边界) 为准：范围明确，测试、独立审计、preflight、回退与新鲜读回路径成立后，可继续提交、PR/合并、不可变 release 及既有内部 sim-only localhost 服务/定时任务的安装、启用或切换。公开入口、生产密钥/账号/权限、破坏性数据操作、broker、真实交易与其它例外不因此获授权。仓库模板、fixture、本地测试、候选分支和旁路成功均不代表 Git 主线、TradingDatas runtime 或现役生产已生效。当前执行证据以本轮运行读回为准；AutoDev 状态是入口，[STATUS.md](../STATUS.md) 是带时间戳快照。

## 1. 不可突破的边界

- `REAL_TRADING_ENABLED=false`；不得由环境变量或 fixture 覆盖。
- 该系统仅供 Nicholas 个人内部使用。前端/API默认只绑定`127.0.0.1`；`tradingagent.cc`远程入口必须先通过Cloudflare Access或等价单用户认证，禁止匿名公网访问和API直出。DNS、Tunnel/Pages与Access policy分别验收。
- TradingAgent 只消费显式配置的 TradingDatas `GET /v1/catalog` 与 `POST /v1/query` 契约；不读取 TradingDatas 数据库，不实现其服务端，不使用旧专用接口或数据商回退。
- HTTP 成功不代表数据可用。每个 dataset 独立检查 `state`、`degraded`、`freshness`、`quality`、`lineage`、`receipt_id`、`data_through`、`observed_at` 和 `reasons`；impaired state 允许后四项为 null，TA 不补造。无完整 source proof 时固定 fail closed；只有证据完整且 policy 明确允许的 impaired evidence 才可降权。

### 1.0.1 TradingDatas 能力分层：不降低真实性，不用稳定门禁阻塞开发

| 层级 | 最小证据 | 允许范围 | 不允许推导 |
|---|---|---|---|
| `contract_ready` | capability/mapping、编译与失败测试 | fixture/mock、兼容测试、候选 PR、内部集成准备 | token 已发放、provider 可用、当前市场事实或生产切换 |
| `observed` | 一次有界 `catalog/query` 回读，含完整 source proof | current-observation、non-authority 的内部研究/Copilot 试读 | 历史 PIT、连续健康、自动调度、模型/资金/订单 authority |
| `stable` | 跨适用 cadence 连续成功且该消费者已 readback | 稳定消费与相应常规运行声明 | 全部无关 dataset/consumer 一并完成 |

开发、合同和候选发布以 `contract_ready` 推进；一次真实试读以 `observed` 推进；只有需要声明稳定运行时才要求 `stable`。任何 dataset 的 source proof 失败仍只阻断该 dataset/窗口，记录 coverage debt 后继续其它独立能力；不得为绕过或降低等待成本新增数据集专用 route、UI、collector、timer、service、表或 provider fallback。
- Crypto 的 runtime token 位于 tmpfs，重启后只允许使用
  `Crypto/systemd/tradingagent-crypto-read-token.tmpfiles.conf` 从发布侧既有
  root-owned canonical source 做 scoped copy。不得打印或读取 token，不得
  复用 A股 token，也不得无参执行全局 `systemd-tmpfiles`。恢复后必须读回
  parent 无 symlink、leaf 为 `tradingagent:tradingagent 0600` regular
  single-link file，并完成 authenticated catalog 与相邻两个 5 分钟核心轮次；
  任一失败保持 sim-only fail closed。
- A股 session initializer 读取上一交易日参考价时，每个
  `cn.equity.daily` 请求最多包含 10 个 `ts_code`。每批必须独立完成双跑、
  ready/fresh/valid/non-degraded、游标终止和身份守恒，再按 symbol 绑定该批
  envelope metadata；任一批 503、缺行、重复或证据拒绝都不创建当日 inputs，
  禁止退回无界查询或 SQLite。
- A股现役 TradingDatas 通用采集与 TA 分钟消费必须错峰：session initializer
  固定 09:18；48 个 delayed-paper 轮次固定在目标 5 分钟边界后 5 分钟（含
  timer 最多 10 秒随机延迟）。这正好落在 runner 的 5–5.5 分钟可消费窗口。
  `expected_available_bar_end` 仍减去一根 5 分钟 provider lag，因此错峰只避免
  SQLite/receipt 写入窗口的 HTTP 503，不改变被消费 bar、交易日、数量或顺序。
- A 股个股只允许沪深主板普通股。创业板、科创板及北京市场个股不得进入候选、预测、目标仓位、订单、成交或持仓；双创指数与全市场行业聚合只作 `context_only` 环境证据。
- 当前唯一订单决策模型是冻结的 rank-score Champion。机会雷达/append-only Ledger、多期限forecast和三风格router已是仓库合同层的shadow能力，只能产生反事实研究artifact，不能影响候选、rank、仓位、风险或订单。默认关闭的DeepSeek HTTPS transport也只是非生产仓库合同；2026-07-18仅有一次隔离真实请求到达provider后被本地evidence schema拒绝，accepted evidence、稳定认证和生产激活仍未验证。live paper scheduler仍是计划项。
- 模拟日即使阻断新增风险，也必须尽量继续减仓/退出、对账、账本、学习到期检查和报告，并以 `completed_with_blocks` 明示结束；不得伪装成功，也不得切回旧链。

## 1.1 GitHub 普通发布与主线冻结

服务器旁路只能使用已通过普通 PR 合并的 `main` 精确 SHA。候选字节变更后，
旧评审、旧测试与旧服务器回执全部只是复现线索，不再是当次 PASS。发布顺序固定为：

```bash
set -euo pipefail
git status --short --branch
git diff --check
git diff --stat

# 仅当这是已审定、无他人改动的隔离候选 worktree 时暂存完整候选范围；
# 混合 worktree 必须改用精确路径，禁止照抄下一行。
git add -A
git diff --cached --check
git diff --cached --stat
git commit -m '<scoped-release-message>'
git push -u origin "$(git branch --show-current)"

gh pr create --draft --base main --head "$(git branch --show-current)" \
  --title '<scoped-release-title>' --body-file '<absolute-pr-body-path>'
gh pr checks '<pr-url>' --watch
gh pr ready '<pr-url>'
gh pr merge '<pr-url>' --merge

git -C /Users/nicholashan/Projects/Finance/Tradingagent.cc fetch origin main
git -C /Users/nicholashan/Projects/Finance/Tradingagent.cc merge --ff-only origin/main
git -C /Users/nicholashan/Projects/Finance/Tradingagent.cc rev-parse HEAD origin/main
```

不删除远程分支，不强推，不改写历史。PR CI 同时覆盖 Python 全量测试与前端
`npm test/lint/build:all`；同一前端矩阵仍必须在本地与服务器旁路分别复验。只有当主工作树
`HEAD == origin/main == PR merge commit`、现有未跟踪资产未受影响时，才能进入旁路验收。

重写 `STATUS.md` 时保留既有导航章节与主线实时查询入口，并补跑
`python -m pytest -q tests/test_architecture_contract_guards.py`。
Markdown 渲染和局部运行测试不能代替这些文档/架构契约检查。

## 1.2 服务器旁路候选部署

服务器旁路部署只用于回答“冻结候选能否在目标服务器环境安装、测试、构建和运行”。它不改变现役代码、服务、定时任务、网页、路由或任何authority。它是正常发布后的默认非权威服务器验收路径，但每次仍必须使用精确提交SHA、新的版本化目录、隔离环境、完整receipt和现役未变读回；任何条件不满足都停止，不得切换现役。

目录约定：

```text
/opt/investment/tradingagent                         # 现役工作树不切换；Git管理元数据仅作受控fetch/worktree登记
/opt/investment/tradingagent-candidates/<release-id> # detached候选代码
/opt/investment/tradingagent-venvs/<release-id>      # 候选专用Python环境
/opt/investment/tradingagent-canary-output/<run-id>  # fixture/canary输出
/opt/investment/release-evidence/tradingagent/<id>   # 受限发布证据
```

### 1.2.1 部署前冻结与取证

在创建候选目录前，至少保存并校验：

- 现役仓HEAD、remote ref与完整`git status --porcelain=v1 --untracked-files=all`；
- `tradingagent-front-api.service` unit、状态、PID与`127.0.0.1:8787/healthz`；
- `root`与`marketgraph`两份用户crontab及其各自哈希；任一不可读都必须记为未验证，不能以另一份替代；
- 现役未跟踪运行资产、回滚目录和磁盘余量；
- 候选远端分支的精确SHA、工作树干净状态和回退目录。

生产仓可能包含不受Git跟踪的append-only运行证据和前端回滚副本。禁止`git clean`、`reset --hard`、覆盖式checkout或`rsync --delete`；也禁止把现役仓切到候选分支。source sync 先独立上传已验 hash 的 bundle/archive，再用只含 fetch/fast-forward 的命令更新；不要把 `rm`、通配清理或其它破坏性清理塞进同一 compound 命令。只允许从精确SHA创建detached worktree，例如：

```bash
set -euo pipefail
umask 077

ACTIVE=/opt/investment/tradingagent
RELEASE_SHA='<merged-main-full-commit-sha>'
APPROVED_BRANCH='main'
RELEASE_ID="ta-state-retirement-$(printf '%s' "$RELEASE_SHA" | cut -c1-7)"
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

### 1.2.2 隔离安装与验收

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
  TRADINGDATAS_API_URL=
  SHAREDSIGNALS_API_URL=
  MARKETGRAPH_API_URL=
  PYTHONDONTWRITEBYTECODE=1
)

sudo -u marketgraph "${SAFE_ENV[@]}" python3 -m venv "$VENV"
sudo -u marketgraph "${SAFE_ENV[@]}" \
  "$VENV/bin/python" -m pip install -r "$CANDIDATE/requirements.txt"
sudo -u marketgraph "${SAFE_ENV[@]}" \
  sha256sum "$CANDIDATE/requirements.txt" \
  | sudo -u marketgraph tee "$EVIDENCE/requirements.sha256" >/dev/null
sudo -u marketgraph "${SAFE_ENV[@]}" \
  "$VENV/bin/python" --version 2>&1 \
  | sudo -u marketgraph tee "$EVIDENCE/python-version.txt" >/dev/null
sudo -u marketgraph "${SAFE_ENV[@]}" \
  "$VENV/bin/python" -m pip --version \
  | sudo -u marketgraph tee "$EVIDENCE/pip-version.txt" >/dev/null
sudo -u marketgraph "${SAFE_ENV[@]}" \
  "$VENV/bin/python" -m pip freeze \
  | sudo -u marketgraph tee "$EVIDENCE/python-freeze.txt" >/dev/null

cd "$CANDIDATE"
sudo -u marketgraph "${SAFE_ENV[@]}" \
  "$VENV/bin/python" -m pytest -q
PYCACHE_ROOT="$(mktemp -d /tmp/ta-pycache.XXXXXX)"
sudo chown marketgraph:marketgraph "$PYCACHE_ROOT"
sudo -u marketgraph "${SAFE_ENV[@]}" PYTHONPYCACHEPREFIX="$PYCACHE_ROOT" \
  "$VENV/bin/python" -m compileall -q \
  shared Ashare CNFutures Crypto tools scripts
sudo rm -rf -- "$PYCACHE_ROOT"

CRYPTO_CANARY_OUTPUT="/opt/investment/tradingagent-canary-output/${RELEASE_ID}-crypto-fixture"
test ! -e "$CRYPTO_CANARY_OUTPUT"
sudo install -d -m 0700 -o marketgraph -g marketgraph "$CRYPTO_CANARY_OUTPUT"
sudo -u marketgraph "${SAFE_ENV[@]}" \
  "$VENV/bin/python" - "$CRYPTO_CANARY_OUTPUT" "$EVIDENCE" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

from Crypto.fixture_auto_sim import run_fixture_file

root = Path(sys.argv[1])
evidence = Path(sys.argv[2])
fixture = Path("Crypto/fixtures/auto_sim_spot_cycle_v1.json")
first_root = root / "first"
second_root = root / "second"
first = run_fixture_file(fixture, output_root=first_root)
replay = run_fixture_file(fixture, output_root=first_root)
second = run_fixture_file(fixture, output_root=second_root)
assert first["idempotent_replay"] is False
assert replay["idempotent_replay"] is True
assert first["bundle"] == replay["bundle"] == second["bundle"]
run_id = first["bundle"]["run_id"]
first_bytes = (first_root / "runs" / f"{run_id}.json").read_bytes()
second_bytes = (second_root / "runs" / f"{run_id}.json").read_bytes()
assert first_bytes == second_bytes
assert first["bundle"]["execution_eligible"] is False
assert first["bundle"]["execution_authority"] is False
assert first["bundle"]["durable_execution_receipt"] is False
readback = {
    "contract": "tradingagent.crypto.server_fixture_readback.v1",
    "run_id": run_id,
    "bundle_sha256": hashlib.sha256(first_bytes).hexdigest(),
    "same_root_idempotent_replay": True,
    "cross_root_bundle_bytes_equal": True,
    "generation_scope": first["bundle"]["capital_policy"]["generation_scope"],
    "execution_eligible": False,
    "execution_authority": False,
    "durable_execution_receipt": False,
    "real_trading_enabled": False,
    "production_verified": False,
}
(evidence / "crypto-fixture-readback.json").write_text(
    json.dumps(readback, ensure_ascii=False, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

cd "$CANDIDATE/front"
sudo -u marketgraph "${SAFE_ENV[@]}" sha256sum package-lock.json \
  | sudo -u marketgraph tee "$EVIDENCE/package-lock.sha256" >/dev/null
sudo -u marketgraph "${SAFE_ENV[@]}" npm ci
sudo -u marketgraph "${SAFE_ENV[@]}" npm test
sudo -u marketgraph "${SAFE_ENV[@]}" npm run lint
sudo -u marketgraph "${SAFE_ENV[@]}" npm run build:all
sudo -u marketgraph "${SAFE_ENV[@]}" node --version \
  | sudo -u marketgraph tee "$EVIDENCE/node-version.txt" >/dev/null
sudo -u marketgraph "${SAFE_ENV[@]}" npm --version \
  | sudo -u marketgraph tee "$EVIDENCE/npm-version.txt" >/dev/null
```

`env -i`只保留上面白名单变量，因此不会继承`BASH_ENV`、代理、现役workspace root、TradingDatas catalog/dataset/auth或DeepSeek credential。`TRADINGDATAS_API_URL`、旧名 tombstone `SHAREDSIGNALS_API_URL` 与 `MARKETGRAPH_API_URL` 在旁路验收中必须显式为空，避免任何未退役旧 reader 把“变量缺失”解释为 localhost 默认地址并读取现役服务；这些空值不是 V1 联调配置。依赖范围未完全锁 hash 时，receipt 必须保存 Python/pip/Node/npm 版本、完整 `pip freeze`、requirements 与 `package-lock.json` 哈希；未保存这些证据不得声称复现了同一环境。所有 evidence 文件必须由 `marketgraph` 创建或通过该用户的 `tee` 写入；若已出现 root-owned 文件，保留失败证据、停止当前验收，不能在事后改 owner 后把同一轮误报为通过。

从本地临时目录或 archive 落盘 immutable release 时，archive 顶层不得把临时目录的
`0700` 带入现役路径。切换 `current` 前，release 必须 root-owned、所有目录为 `0755`、
文件不可被 group/other 写入，并且以每个实际 service UID 分别验证其可进入
`WorkingDirectory`、可读取 `ExecStart` 与所需 Python 入口。前端重启后应以有界重试读取
`127.0.0.1:8787/healthz`；任何一次失败都必须在同一发布动作中原子切回先前 immutable
release 并重启前端，不能等待 service 的自动重启或以 `activating` 代替健康验证。

同一现役发布助手还负责收口五个 G5 round-trip unit、一个 40 标的 observation
unit 与四个 A股 unit
（minute-session、minute-paper、minute-scale500-session、minute-scale500-paper）
的 release 绑定。初始化和消费必须使用同一不可变版本，不能仅绑定基线 paper。
`current` 切换前，助手只允许用本次已校验 immutable release 内的
`deploy/release.sh` 原子替换 `/usr/local/sbin/tradingagent-release`；字节不同则替换后
重新进入，使同一次 `controller-accepted-deploy` 按该 SHA 的 unit 名单收口，包括把
`tradingagent-crypto-forty-symbol-observation.service` 从
`20-forty-symbol-release.conf` 收到 `99-tradingagent-release.conf` 并钉到该 SHA。
GitHub 读回必须同时核对 `current/.deployed-sha` 与该 40 标的 unit 的有效
`WorkingDirectory`/drop-in；只更新 symlink、仍钉在旧 SHA（例如 `7eb0e624`）视为失败。
切换前这些 one-shot
unit 必须全部已停止：正常状态为 `inactive`；上一轮失败遗留的 `failed` 仅在
`MainPID=0` 且 `ControlPID=0` 时允许切换，并保留失败事实，不执行 `reset-failed` 或手工
启动。助手保存现有有效 `TimeoutStartUSec`，只备份并移除
白名单内的旧 release drop-in，再为每个 unit 原子写入唯一的
`99-tradingagent-release.conf`。该文件只绑定本次不可变 `WorkingDirectory`、
`PYTHONPATH`、只读 release 路径和原有效 timeout；`daemon-reload` 后逐 unit 读回，旧
drop-in、路径漂移或运行中竞态均使整次发布失败。失败路径同时恢复前一 `current`、原
drop-in 集合和前端健康；不得手工删除其它 drop-in，也不得把 unit inactive 或配置读回
冒充自然 receipt、消费者或模拟结果。首次交付助手自刷新或新 unit 绑定时，须从已验收的
精确 release 重新安装 root-owned `/usr/local/sbin/tradingagent-release`，之后普通
`controller-accepted-deploy` 才会按新名单收口；陈旧助手不能静默成功。

不可变 staging 不能把 Git tree 中的 executable bit 统一抹成 `0444`。目录权限归一后，
普通文件可设为只读，但每个 Git mode `100755`（或经已验旧 immutable release 精确同路径
确认的可执行文件）必须保留 owner execute；切换前同时断言 release tree 无 group/other
写权限并逐个读取 systemd `ExecStart`/脚本入口。不要用递归 `chmod` 的“全文件同 mode”结果
冒充 immutable 验证。

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
CANARY_LOG="$EVIDENCE/canary-api.log"
SERVER_JS="$CANDIDATE/front/dist-server/server/tradingAgentSnapshotHttp.js"
NODE_BIN="$(sudo -u marketgraph env -i PATH="$SAFE_PATH" sh -c 'command -v node')"
FRONT_STATE="$(systemctl is-active tradingagent-front-api.service || true)"
FRONT_ENABLED="$(systemctl is-enabled tradingagent-front-api.service || true)"
FRONT_LISTENER_BEFORE="$(ss -ltn | grep -F '127.0.0.1:8787' || true)"
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
  sh -c 'set -eu; printf "%s\n" "$$" > "$1"; exec "$2" "$3" > "$4" 2>&1' \
  sh "$PID_FILE" "$NODE_BIN" "$SERVER_JS" "$CANARY_LOG" &
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
curl -fsS "http://127.0.0.1:$CANARY_PORT/healthz" \
  | sudo -u marketgraph tee "$EVIDENCE/canary-health.json" >/dev/null
curl -fsS -D - -o /dev/null \
  "http://127.0.0.1:$CANARY_PORT/api/trading-agent/snapshot" \
  | sudo -u marketgraph tee "$EVIDENCE/canary-snapshot-headers.txt" >/dev/null
curl -fsS "http://127.0.0.1:$CANARY_PORT/api/trading-agent/snapshot" \
  | sudo -u marketgraph tee "$EVIDENCE/canary-snapshot.json" >/dev/null
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
            if normalized in {
                "realtradingenabled",
                "livetradingenabled",
                "productionverified",
            }:
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
if [[ "$FRONT_STATE" = active ]]; then
  curl -fsS http://127.0.0.1:8787/healthz \
    | sudo -u marketgraph tee "$EVIDENCE/current-8787-health.json" >/dev/null
elif [[ "$FRONT_STATE" = inactive && "$FRONT_ENABLED" = disabled && -z "$FRONT_LISTENER_BEFORE" ]]; then
  # A deliberately retired front remains unavailable; candidate validation must
  # neither start it nor use it as an implicit fallback.
  test "$(systemctl is-active tradingagent-front-api.service || true)" = inactive
  test "$(systemctl is-enabled tradingagent-front-api.service || true)" = disabled
  ! ss -ltn | grep -Fq '127.0.0.1:8787'
else
  echo "unexpected front baseline: state=$FRONT_STATE enabled=$FRONT_ENABLED" >&2
  exit 1
fi
```

验收至少证明：

- `GET /healthz`与只读snapshot可用，`Cache-Control: no-store`；
- 顶层`mode=simulated`且所有真实交易标志为false；
- snapshot的POST返回405，未知路由返回404；
- canary停止后备用端口无监听；
- 若现役8787前端原本 active，始终健康；若它在 preflight 时已明确
  `inactive + disabled`，则整个候选过程保持该退役状态、没有监听，也不作为回退路径。

冻结fixture必须写到独立canary output root。A股基线fixture与Crypto `auto_sim_spot_cycle_v1.json`至少完成首次运行、同根幂等重放和跨根业务bundle字节一致性检查；CNFutures至少运行其fixture闭环聚焦测试。输出必须保持`non_authority`/`local_candidate`或`fixture_simulated`的精确市场语义，并明确`production_verified=false`、`real_trading_enabled=false`。Crypto还必须保持`execution_eligible=false`、`execution_authority=false`、`durable_execution_receipt=false`与`local_fixture_opening_baseline_only`。不得写正式SampleJournal、活动runtime根或前端投影根。

### 1.1.3 最终readback与回滚

部署完成后重新读取并逐字节或逐哈希比较现役仓状态、systemd unit、crontab和健康检查；同时确认候选精确SHA、候选工作树干净、备用端口已关闭。发布receipt必须把`server_sidecar_canary`与`active_production_activated=false`明确写开，不能用“已部署”省略层级。证据目录内除最终manifest自身外的文件应生成排序后的SHA-256清单，再单独记录该清单的SHA-256；receipt至少保存候选/现役SHA、依赖版本、测试结果、canary状态、fixture状态、现役变更布尔值和未验证项。

因为sidecar从未接管现役服务，回滚只需停止候选进程并保留证据。候选worktree、venv与输出目录只有在留存期结束且获得清理授权后才可移除；不得删除现役未跟踪资产、append-only账本、模拟样本或既有回滚目录。若未来要切换现役源码/API、cron、页面或公网路由，必须重新进行独立发布授权、备份、原子切换和真实回退演练，不能沿用本次sidecar授权。

## 2. 安全环境与显式配置

从目标隔离 worktree 根目录运行：

```bash
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export REAL_TRADING_ENABLED=false
```

### 2.0 多市场运行拓扑预检

任何单机服务新增、市场拆机、状态目录迁移或模型服务器接入前，先在冻结release运行：

```bash
python3 scripts/validate_runtime_topology.py
```

该命令只验证仓库中的逻辑拓扑，不探测服务器，也不产生部署授权。输出必须保持：

- `simulation_only=true`；
- market精确为`ashare/cn_futures/crypto`；
- data routes精确为`GET /v1/catalog`和`POST /v1/query`；
- `single_host_sim`、`split_market_sim`与
  `split_market_with_research_host_sim`均有效。

拆服务器按市场逐个迁移，不做一次性大切换：

```text
freeze release/config
-> 停止该市场旧scheduler并保存append-only head
-> fencing旧writer
-> 复制sealed snapshot/segments到目标主机
-> 以只读模式校验checksum、generation、execution lineage
-> 配置目标主机独立endpoint与token file
-> one-shot sim-only
-> reconcile与幂等重放
-> 启用目标scheduler
-> 确认旧主机仍无writer
```

每次只迁移一个市场。TradingDatas、另两个市场和front保持不动。失败时停止目标scheduler、
保留所有新事实，并在确认目标writer已fence后恢复旧writer；不得让两台主机同时重试。
禁止NFS/共享SQLite、复制明文token、使用8082或provider route临时绕过。learning worker可在
core验证后另行迁移，其失败不回滚core，也不能自动替换Champion。

V1 不提供 TradingDatas 默认地址。仅在 TradingDatas fresh handoff 与上游合同真正冻结、并由获批联调任务提供时，才可显式设置：

```bash
export TRADINGDATAS_API_URL='<explicit-https-or-loopback-ip-http-base-url>'
export TRADINGDATAS_CATALOG_VERSION='<explicit-frozen-catalog-version>'
export TRADINGDATAS_ACCESS_POLICY_ID='<explicit-read-only-policy-id>'
export TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON='<explicit-market-to-dataset-json>'
export TRADINGDATAS_SCHEMA_MAJOR='<explicit-positive-schema-major>'
export TRADINGDATAS_RUNTIME_TRANSPORT='http-json-v1'
export TRADINGDATAS_API_TOKEN_FILE='/run/secrets/tradingagent/tradingdatas-read.token'
```

缺任一配置时保持 unavailable；不得猜测 localhost、生产地址、catalog version、schema major 或 dataset ID。`TRADINGDATAS_API_TOKEN_FILE` 只保存 `/run/secrets/tradingagent` 下的仓外绝对路径；checkout/worktree/Git common repo 或任意其它目录中的文件一律拒绝。禁止在环境变量、manifest、fixture、日志或回执中写明文 token。目标文件必须由发布侧独立配置为 TA-scoped credential，精确 `0600`、可信 owner、普通单硬链接文件且整条路径无 symlink；不得复用 TradingDatas bootstrap token。`http-json-v1` transport 只接受无尾随斜杠、path、query、fragment、userinfo、控制字符或反斜杠的 canonical `scheme://host[:port]`，并只向该 authority 上精确的 `GET /v1/catalog` 与 `POST /v1/query` 注入 Bearer header；只接受通用客户端固定生成的 JSON `Accept`/`Content-Type`，调用方添加 Host、forwarding、proxy 或其它 header 会在网络前拒绝。远端 authority 必须使用 HTTPS，明文 HTTP 只接受 `127.0.0.0/8` 或 `::1` 的 IP 字面量 loopback，不接受 `localhost` 等 DNS 名称。不同 authority/path/query/method、调用方自带 Authorization 和 30x 重定向都在发送前拒绝。transport 为 single-flight，并发第二请求在网络前失败；401/403 不读取正文并锁住该 transport，后续请求、重试和任何 legacy/provider fallback 全部拒绝。当前合同完成不等于实际 token 已发放，也不授权配置或运行 live endpoint。

长期 A股 observation worker 的发布合同固定为专用 `tradingagent:tradingagent` 服务身份，叶文件路径为 `/run/secrets/tradingagent/tradingdatas-read.token`；`tradingagent-front-api.service` 不读取该 token。父目录只能由受控 tmpfiles 阶段校正，只有 credential publisher 可生成、注册并以 `tradingagent:tradingagent`、精确 `0600` 和无 symlink/硬链接别名的原子替换安装叶文件；重启后重建也必须走同一 freeze/publisher/readback 顺序，不能恢复旧叶。应用候选、测试、manifest、日志与任务消息均不得创建、读取或传递 token 值。旧 `marketgraph:marketgraph` 身份及其只读 token 只是 2026-07-22 一次性兼容验收的历史证据，不得在长驻 worker、front 或新 state root 中复用。发布前若 fresh TA-scoped 叶尚不存在，消费端必须保持 unavailable，不能自行降级到其它 credential、端口或数据源。

#### 2.0.1 专用服务身份与 installed unit 切换

仓库中的权威安装字节为：

- `deploy/systemd/tradingagent-runtime.sysusers.conf`：创建无登录权限的
  `tradingagent:tradingagent`；
- `deploy/systemd/tradingagent-runtime.tmpfiles.conf`：创建相互分离的
  state/runtime/log 与 secret parent，不创建 token leaf；
- `deploy/systemd/tradingagent-front-api.service`：localhost-only 前端只读 API；
- `deploy/systemd/tradingagent-ashare-observation.service`：一次性 A股观察 worker；
- `deploy/systemd/tradingagent-ashare-observation.timer`：不可 enable 的静态候选，
  本阶段不安装、不启用。

observation worker 不得再引用旧 `/opt/tradingagent/venv`。当前冻结解释器为
`/opt/investment/tools/venvs/tradingagent-observation-py312-pyyaml603-v1/bin/python3`：
它是 root-owned、无 symlink 路径、single-link、精确 `0555` 的 versioned
minimal runtime，父目录必须是 root-owned 且不可由 `tradingagent` 写入。
唯一第三方运行依赖由
`deploy/ashare-observation-requirements.txt` 精确版本和 wheel SHA-256
冻结；当前为 PyYAML 6.0.3。运行时审计必须实际导入并验证这个版本，不能仅凭文件
存在或系统全局包推断依赖可用。
`ExecStartPre` 会在读取 token metadata 和发起网络前复核该解释器；leaf、owner、
mode、link count 或路径任一漂移都阻断 worker。不得原地升级此 runtime；Python
版本或依赖变化必须建立新的 versioned 路径、更新 unit/测试并重新走 release
preflight。

服务器构建只能在受限临时目录使用系统 `/usr/bin/python3 -m venv --copies`，
随后用 `pip install --require-hashes` 安装上述最小依赖；确认依赖版本、以 UID
987 导入冻结 release 成功后，再 root-owned 原子安装到上述路径并冻结目录/文件
权限。构建日志必须记录 Python/PyYAML 版本、runtime tree digest、requirements
digest 和目标 release，但不得包含 token 或其哈希。该 runtime 安装不等于
observation service 安装，更不等于 timer/front/模拟或真实交易激活。失败回滚是
保持 worker/timer inactive、保留证据并修复前滚；禁止恢复旧
`/opt/tradingagent/venv` 依赖。

以下是 2026-07-22 credential 切换前的历史基线与不可逆顺序，不是当前运行态，
也不得因为后续 runtime/front 修复而整段重放。历史基线只做过 metadata-only
读回：`/run/secrets/tradingagent` 是
`0700 marketgraph:marketgraph` 目录，既有
`tradingdatas-read.token` 叶是 `0600 marketgraph:marketgraph` 的 regular file、
`nlink=1`。这只证明旧安装态文件身份，不证明其 scope、内容或可复用性；禁止读取、
备份、哈希或复制叶内容。针对这个已存在路径，切换顺序固定为：

1. **backup/preflight**：备份账户数据库、现役 unit/drop-in、root 与
   `marketgraph` 两份 crontab、现役仓 HEAD/status、immutable release/current
   指针和相关路径 metadata；确认 observation timer 未安装且未启用。credential
   内容不进入备份或证据。
2. **只落 sysusers identity**：安装 sysusers 配置并运行
   `systemd-sysusers`，随后读回 UID、GID、primary group、nologin 与 home；本步
   不运行 tmpfiles，不改 current/front，也不触碰 secret parent 或叶文件。
3. **先暂停 TA cron**：只用本仓 `tools/merge_tradingagent_crontab.py` 和 paused
   模板原子安装。读回必须同时证明：TA recurring market job 为 `0`；
   `# TRADINGAGENT_SCHEDULE_STATE=paused_until_tradingdatas_fresh_handoff` 恰好出现
   `1` 次；全部 non-TA 行的字节、相对顺序和有效环境赋值保持不变。timer 继续保持
   未安装、未启用；三项任一不满足都不是 paused PASS。
4. **停止并隔离 legacy front**：步骤 3 读回通过后，立即停止并在迁移窗口内隔离
   已安装的 `tradingagent-front-api.service`，阻止依赖或人工操作将其重启。其当前
   PID/cgroup 是由旧 `marketgraph` UID 运行的 TA legacy front；必须保存停止前身份，
   再读回 unit inactive、原 PID 退出且 cgroup 无成员。本步不启动新 front/canary。
5. **立即证明零 holder**：紧接 legacy front 停止读回，在任何其它动作、credential
   freeze 或 tmpfiles 变更前执行。扫描必须覆盖所有 TA process、所有已加载或遗留的
   TA service/cgroup 名称，并同时覆盖旧 `marketgraph` UID、新
   `tradingagent` UID 987 以及 root/其它 UID；不能只按 UID 987 或新 unit 名过滤。
   同时对相关 current/release、state 与 secret 路径完成 `/proc` 的 cwd、root、
   open-FD 和 mmap holder 读回，所有结果必须为零。仅看 cron、单一 service 状态或
   单一 cgroup 不够。
6. **协调 credential freeze**：由 credential publisher 明确冻结生成、轮换和
   写入窗口；应用发布者不得接触 credential 内容。freeze 未确认时停止。
7. **再应用 tmpfiles**：只有 paused-cron 三项精确读回、legacy front 已停止隔离、
   全量零-holder 与 publisher freeze 证据同时成立后，才安装 tmpfiles 配置并运行
   `systemd-tmpfiles --create`；tmpfiles 只建立/校正目录，不创建 token leaf。
8. **publisher 原子安装新叶**：publisher 在服务器本地生成并注册全新的
   TA-scoped credential，再原子安装
   `/run/secrets/tradingagent/tradingdatas-read.token`；最终叶必须是精确
   `0600 tradingagent:tradingagent`、regular、single-link，整条路径无 symlink，
   也没有硬链接或其它 inode alias。不得复用或改名旧 `marketgraph` credential。
9. **metadata-only readback**：只读回路径组件、owner/group、mode、文件类型、
   link count 与 alias 扫描结果；任何 token 值或内容哈希都不得进入命令行、Git、
   日志、回执或任务消息。
10. **最后 unfreeze**：只有新叶 metadata readback 全部通过，publisher 才解除
    freeze。unfreeze 不安装/启用 timer，也不自动切换 current、front 或 worker；
    legacy front 继续停止隔离，TA 仍须等待独立的 12-profile authenticated parity
    与切换授权。

应用发布流程既不能读取身份不明的
`/etc/tradingagent/tradingdatas-read.token`，也不能复制旧 `marketgraph` runtime
token。步骤 3 暂停成功后任一步失败，TA observation consumer 必须保持
unavailable，TA recurring job 保持为零、pause marker 保持恰好一个，同时保留
non-TA cron 的字节/顺序/环境。步骤 4 停止隔离后失败时，legacy front 必须继续
停止且不得解除隔离；只能保存失败证据并受控前滚。不得恢复旧 TA cron、旧
credential/叶文件或 legacy front，也不得回退到 8082、旧 route、文件或 provider
数据路径。

前端 unit 使用专用 UID/primary group 和 immutable release bytes；迁移期间仅以
`SupplementaryGroups=marketgraph` 只读历史模拟投影，且没有任何
`ReadWritePaths`；`InaccessiblePaths=/run/secrets/tradingagent` 进一步隔离
worker credential，front 即使使用相同主 UID 也不能读取 token。front canary 与
切换属于步骤 10 之后的独立授权阶段；步骤 4 到 10 之间不得启动任何 legacy/new
front 进程。后续切换前必须在备用 loopback 端口以专用身份启动 canary，比较
health、snapshot 安全标志与关键投影计数；无法读取历史投影时保持 front
unavailable 并修复前滚，不得重启 legacy front 或用放宽全树权限掩盖失败。
installed base unit 必须与仓库字节一致，历史
`sharedsignals.conf`/`sim-only.conf` drop-in 必须备份后移出 active unit 目录；
新 base unit 已固定 simulation-only 并把前端 TradingDatas/MarketGraph URL 留空。

现役 `marketgraph` crontab 的 TradingAgent managed block 必须使用本仓
`tools/merge_tradingagent_crontab.py` 与 paused 模板生成，原子安装并读回，结果
必须同时为零条 TA recurring market job、恰好一个 pause marker，并保持 non-TA
字节/顺序/有效环境赋值不变。其它项目的 provider/monitor cron 不属于 TA 写域，
不得在本次切换中逐条删除。这样只证明 **TA 活跃消费者不再使用 8082**；
旧 SharedSignals service、provider/monitor cron 与 8082 的最终退役仍需其 owner
另行完成持续数据替代、no-use 观察和回滚验收。

`--apply` 必须同时传入仓外绝对 `--backup-dir`（本次使用对应 release 的
`/opt/investment/release-evidence/tradingagent/.../cron`）；工具会在任何系统写入
前拒绝 Git checkout 内的备份目录。禁止使用旧的仓内
`runtime/backups/crontab`，也禁止从脏的现役仓运行会写回代码树的安装方式。

本阶段不切换 current 或安装/启用新 front/worker timer，但会在步骤 4 显式停止并
隔离 legacy front，因此不存在用旧 front、旧 TA cron 或旧 credential 恢复可用性的
回滚。步骤 3 前失败可保持原状并退出；步骤 3 后失败固定 fail closed，步骤 4 后还
必须保持 front stopped/isolated：停止任何新 TA process、保持 TA job 为零、唯一
pause marker、non-TA cron 不变和 consumer unavailable，保留 immutable release、
备份与失败证据，再按同一顺序修复前滚。不得删除证据，也不得以旧 front、旧 token、
旧调度或 8082 fallback 缩短故障窗口。

### 2.1 TradingDatas V1 接入验收器

`sharedsignals_v1_gate.py` 与 `sharedsignals_v1_integration_probe.py` 只保留兼容文件名。前者是启动前的轻量 catalog/auth/单次 dataset 可用性 smoke：遇到 non-terminal page 会拒绝，不能声明完整 dataset、research snapshot 或历史 PIT 已通过。后者负责首次接入、TradingDatas 发布或 catalog/profile 变化、消费者切换和故障恢复后的完整只读 consumer 验收，必须完成 bounded pagination 与同一 observation 双跑。二者均不实现或验收 TradingDatas 服务端；reason code 只由 TA 本地状态机产生，上游 `metadata.reasons` 自由文本只保存哈希。

模板见 [sharedsignals_v1_integration_probe.example.json](examples/sharedsignals_v1_integration_probe.example.json)；该文件名是兼容入口。模板中的 `.invalid` 地址、`fixture.*` dataset ID、catalog 与 policy 只用于说明结构，不是生产默认值。TradingDatas owner 提供 fresh handoff 后，应复制到仓外绝对路径并逐项替换；manifest 只允许保存 base URL 与访问策略**身份**，禁止写 API key、token、密码或其它 credential。验收器只从 root/service 配置的 `TRADINGDATAS_API_TOKEN_FILE` 读取 credential，安全文件门未通过时在创建 HTTP opener 前阻断；不会读取明文 token 环境变量或自行发明其它认证/header/fallback。

首批显式功能角色为：

- `trade_calendar`：交易日历，执行必需；
- `security_master`：证券主数据，必须显式请求 `ts_code/name/list_status/list_date` 并固定 `list_status={eq:L}`；runner 在消费侧再排除非主板、风险警示和上市不足 30 日个股；
- `daily_bars`：主板 provider-native 日线当前观察，执行必需；没有历史 first-seen/revision authority 时不得称为历史 PIT；
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

完整验收器只调用一次 `GET /v1/catalog`。每个 dataset 的 manifest 必须分别指定 `fields/filters/schema_major/limit/identity_fields/observation_mode/query_as_of_mode/max_pages/max_rows`，可选配置 provider-native domain event field 的 format/timezone/semantic；`order` 未配置时从请求中省略，使用 registry 默认排序。`query_as_of_mode=decision_as_of` 才发送 manifest 决策时点，`omit` 则不发送 `as_of`。例如日线必须使用精确 `trade_date` filter，禁止无界查询后重试或切换数据路径。

每次 query run 从 `cursor=null` 开始，以 uncached 请求透明跟随 opaque `next_cursor` 到 terminal page；raw cursor 不进入日志、异常或回执。遍历同时受 `max_pages/max_rows` 与代码 hard ceiling 约束，并要求跨页完整 metadata identity 不变、dataset-specific row identity 唯一、服务端页序/行序原样保留。cursor self-loop或A-B-A cycle、metadata drift、缺/重 identity、页/行预算超限、非 terminal 截断均 fail closed；禁止本地排序、去重、增大 limit 或只取第一页。

同一 dataset 完成两次相同 manifest/observation 的完整遍历。semantic trace 排除 transport request ID 与 opaque cursor 值，但包含 envelope metadata、source proof、页结构、ordered rows 与 identity；任一业务语义漂移阻断。exact trace 仍分别绑定每页 request ID、cursor-chain hash 与 cursor-bearing request hash，因此游标值变化不造成业务误报，也不会丢失单次运行的精确审计证据。TradingDatas rows 原样保存；`receipt_id/data_through/observed_at` 与完整 lineage 只从 envelope 形成 source proof，不复制成行级 `available_time/revision_id/receipt_id`。可选 domain event-time 不是可知时间；缺历史 first-seen/revision chain 时 research snapshot 固定 `current_observation`、`historical_pit_eligible=false`。

回执固定标注 `authority=non_authority`、`production_verified=false`、`real_trading_enabled=false`，隐藏 base URL、access policy 值、raw cursor、异常原文与上游自由文本 reason，只保存 authority/config、catalog/query、source proof、分页/identity、same-observation 与 current-observation hashes 及 TA 受控 reason codes。退出码为：`0=通过`、`2=数据或合同阻断`、`64=manifest/transport配置无效`、`74=回执落盘失败`。回执通过只证明该次显式只读输入满足 TA consumer contract，不证明 TradingDatas 服务端整体通过、生产 runtime 已切换、历史 PIT、每日持续健康或交易获授权。schema ID 为 `tradingagent.tradingdatas.integration-readiness.v2`；旧类名/文件名只是代码兼容入口。

每次 catalog/dataset/schema、filters/as-of policy、identity/event mapping、budgets 或 access policy identity 变化都必须生成新 manifest 与新回执，不能复用旧 PASS。

#### 2.1.1 Catalog 全 active-set parity

`shared.runtime_test.tradingdatas_catalog_parity` 是独立于 A股五项 observation
profile 的 transport/contract 验收器。它从一次 `GET /v1/catalog` 发现 active
集合，并要求仓外 secret-free manifest 对 `total/active/paused` 数量、active
dataset ID、`schema_major/default_fields/limits` 精确冻结；集合或合同漂移时在
发出任何 query 前阻断。每个 active dataset 随后执行两次有界、完整 cursor
遍历，并复用同一 identity、metadata 与 Evidence Gate 门禁。

回执把结果拆成三项，禁止用一个 HTTP 成功状态概括：

- `transport_contract_pass`：固定 API、分页预算、跨页 identity 和同一
  observation 双跑均守恒；
- `ready_set_pass`：manifest 声明为 ready 的 dataset 具有完整 source proof，
  且被 Evidence Gate 以权重 `1.0` 接受；
- `impaired_set_accounted`：manifest 声明为 impaired 的 dataset 被逐项拒绝、
  权重固定 `0.0`，没有进入 research snapshot。

`unobserved/paused/failed/stale/empty/degraded` 合法状态可以没有
`receipt_id/data_through/observed_at/provider lineage`。对预先声明的 impaired
dataset，这种缺失必须如实记录为 `source_proof_unavailable`；只要 envelope、
分页和双跑语义稳定且 Evidence Gate 拒绝，它仍可被“诚实计入”，但绝不能
晋级研究证据。ready dataset 缺任何 source proof 仍然阻断。若 impaired
dataset 意外变为 ready，也先阻断并要求更新 manifest，避免无审查自动扩大
输入范围。

```bash
export REAL_TRADING_ENABLED=false

python3 -m shared.runtime_test.tradingdatas_catalog_parity \
  --manifest /etc/tradingagent/tradingdatas-catalog-parity.json \
  --token-file /run/secrets/tradingagent/tradingdatas-read.token \
  --output /var/lib/tradingagent/ashare-observation/catalog-parity-receipt.json
```

退出码固定为 `0=三项均通过`、`2=合同或逐数据集门禁阻断`、
`64=manifest/token/transport配置无效`。回执是 `non_authority`，不含 URL、
token、header、row、cursor 或上游自由文本；它不替代五项 A股 observation
profile，也不授权 timer、订单或真实交易。

### 2.2 A股 one-shot current-observation runner

worker 不再依赖 `/etc/tradingagent` 下的静态日期 manifest。每次 observation
之前先由专用身份运行动态 builder：

```bash
export REAL_TRADING_ENABLED=false

python3 tools/build_ashare_observation_manifest.py \
  --base-url http://127.0.0.1:18082 \
  --access-policy-id ta-ashare-observation-read-v1 \
  --token-file /run/secrets/tradingagent/tradingdatas-read.token \
  --manifest-root /var/lib/tradingagent/ashare-observation/manifests \
  --json
```

builder 只使用固定 `GET /v1/catalog` 与 `POST /v1/query`。它动态冻结完整 active
catalog inventory，但只把经过业务映射审核的
`trade_calendar/security_master/daily_bars` 三角色写入 observation manifest；
其它 active dataset 不自动查询、不自动晋级。交易日历必须完整分页并由当前
metadata/source proof 接受，证券主数据与最近开市日日线再各做一页预检。任一核心
dataset 为 `stale/partial/empty/unobserved/failed/degraded`，或缺 receipt、
lineage、data-through、observed-at，退出码为 `2` 且不更新 `current.json`。

仓外产物固定在 manifest root 的 `archive/`、`catalog/`、`receipts/` 和 regular
file `current.json`；目录 `0700`、文件 `0600`。同一交易会话合同完全相同时复用
原 manifest；同一会话 catalog/active contract 漂移时保留旧 current 并失败关闭。
builder 的 catalog snapshot 是 inventory 证据，不是 92 个（或任意未来数量）
dataset 的研究资格，也不替代下面 runner 的完整分页、same-observation 双跑和五项
committed binding。退出码固定为 `0=发布或精确复用通过`、`2=上游/证据门禁阻断`、
`64=本地配置/token-file/transport无效`。

仓库候选提供一个 observation-only runner，把上一节的完整双跑 probe 作为不可绕过的写入前门禁，再冻结一次同语义的 provider-native research snapshot。runner 同时要求 `security_master` 与 `daily_bars`，以两者 symbol 并集建立 denominator，将 ST/退市风险、新股、停牌/零成交、缺日线和非主板个股作为显式排除记录。首次成功写入必须先持久化 transaction intent，再原子冻结 `ResearchDataSnapshot`、integration probe receipt、aggregate observation receipt 和逐股 membership ledger，四项精确读回后才发布 transaction-complete commit marker；可消费权威因此是五项绑定，不是“文件都出现了”即可。`observation_universe` 只是观察初筛，不是 Account Tradable Universe、小资金可行池或订单池。runner 不生成候选、资本预约、订单、成交、对账或 SampleJournal 样本，也不表示自动模拟 scheduler 已安装。

运行消费端只能通过 `load_verified_ashare_runtime_authority_bundle` 在同一 state root 与 session lock 内重读五项 committed binding。普通 mapping/hash、直接 `AshareRuntimeAuthorityBundle(...)` 或公共诊断 builder 都不能自授资格；缺 complete 的半写事务、权限不是 root `0700`/file `0600`、owner 不匹配或跨根 artifact 一律 fail closed。日线估值 adapter 只接 state root 与显式交易身份，在内部调用该 loader，不接受调用方注入的 receipt、membership 或“已验证”bundle。

运行时必须显式提供仓外 manifest、fresh state root、runtime root 与 log root；token-file 可由参数或服务环境中的 `TRADINGDATAS_API_TOKEN_FILE` 指定，二者都只允许绝对路径。`runtime-root` 与 `log-root` 是 dedicated worker 的安装边界，当前 runner 不在其中创建第二业务 authority。旧 `a7488e9` state root 没有 membership ledger，只能作历史读回证据；新候选不得在其上补写或作精确重放。

```bash
export REAL_TRADING_ENABLED=false
export TRADINGDATAS_API_TOKEN_FILE='/run/secrets/tradingagent/tradingdatas-read.token'

python3 tools/run_ashare_observation.py \
  --manifest /absolute/path/ashare-observation-v2.json \
  --state-root /absolute/path/ashare-observation-state \
  --runtime-root /absolute/path/ashare-observation-run \
  --log-root /absolute/path/ashare-observation-log \
  --json
```

也可显式传入 `--token-file /absolute/path/to/tradingdatas-read.token`；禁止两种方式包含明文 token，且出现 `TRADINGDATAS_API_TOKEN`、`TRADINGDATAS_BEARER_TOKEN` 或 `TRADINGDATAS_TOKEN` 时即使 token-file 合法也会拒绝运行。runner 固定 `mg_off`，不会读取 `MARKETGRAPH_API_URL`。首次运行只有在 bounded pagination、same-observation 双跑、probe 后快照语义守恒、source proof、current-observation、证券主数据与主板 scope 投影全部通过后才写入；同一 profile/decision 的精确重放只读回不可变的五项 committed binding，不创建 transport、不再次请求数据。intent 后任一崩溃点只允许在同 session 锁内恢复精确同内容；没有 complete marker 的四项状态不能被 runtime authority、history 或 planner 消费。创业板、科创板和北交所个股可保留在原始全市场观察中，但只计入排除原因，不能进入 `observation_universe`。当前 `index_classify`/`sw_daily` 只是行业分类与行业指数 `optional_context`；没有成分 denominator/coverage receipt 时不得称为完整行业宽度。旧回执中的 `tradable_*` 只是待退役兼容别名，不是订单 authority。

退出码：`0=观察绑定或精确重放通过`、`2=数据/范围/存储门禁阻断`、`64=参数、manifest、token-file或transport配置无效`。stdout 只输出 secret-free 摘要；systemd/journal 日志不得把 token、Authorization、manifest正文或 provider自由文本 reason 写出。专用 `tradingagent:tradingagent` 身份与 TA-scoped token-file 已完成服务器 handoff；tracked worker unit 已串联 runtime audit → dynamic manifest builder → observation runner，但仍是 **non-enableable code candidate**。在正式 18082 上完成 disabled unit 手工 one-shot、精确重放、失败恢复和回滚验收前，禁止 enable/install timer，也不得声称每日自动观察、自动模拟或生产激活。周末或节假日 one-shot 必须让 `decision_as_of` 反映实际观察时间，并由完整 `trade_calendar` 证明日线 filter 是最新已完成开市日；不得把自然日硬改成行情日，也不得把 session-date `data_through` 伪造成收盘时刻。

### 5分钟 delayed-paper 自动积累

运行读回须分别核对 accepted/feature/candidate、pending、实际模拟成交与
reconciled，不可把 coverage 或 `status=pass` 当成交。每个 sleeve 的
`settled_quantity`、`settled_notional_cny`、`settled_fee_cny` 来自本槽实际
fixture receipt；无 settlement 为零。最高分股票买不起一手时只记录该股票
拒绝，再检查后续合格股票；不增加现金、单票上限或放宽撮合条件。

`Ashare.minute_auto_runner` 只为已初始化的当日私有目录选择一根当前应到达的
5分钟K线，并委托同一个 `minute_paper_runner`。目录固定为：

分钟 canary 在 catalog、query、认证、传输、分页或合同阶段失败时，仍保持原有
`minute_tradingdatas_request_failed`/fail-closed 对外语义，但会原子写入一个
secret-free 的失败回执。`failure_stage` 只允许
`catalog_request`、`catalog_contract`、`query_request`、`query_contract`、
`pagination`、`auth`、`transport`、`configuration` 或 `unknown`；
`failure_class` 也只使用源代码定义的有限枚举，未知异常固定为 `unknown`。
回执不含 token、Authorization、请求/响应正文或异常文本；没有完整 500/500
receipt-lineage 证据时不得启动 Scale500 late-start。

当请求的证券集合只获得部分、但每个已接受标的仍具备一致的时间、schema/key 与
receipt lineage 时，canary 回执会保留这些有效行并标记
`quality_status=usable_degraded`，同时写入 `requested_count`、`accepted_count`、
`missing_count`、`missing_symbols` 以及请求/接受标的集合。该降级证据只支持有明确
范围的已覆盖标的或 shard；Scale500 late-start 仍是其自身的 claim-specific gate，
必须另有同一请求的完整 500/500 canary 与 receipt-lineage，部分回执不得被当作完整
cohort，也不会打开 learning、promotion 或 execution。

```text
/var/lib/tradingagent/ashare-minute-paper/YYYYMMDD/
├── minute-manifest.json
├── reference-facts.json
├── universe.json
└── state-bundle.json
```

目录必须 `0700`，输入和 bundle 必须 `0600` 且归
`tradingagent:tradingagent`。缺目录时安全 no-op；重复并发、数据退化或30只
不完整时失败关闭。分钟缺口不得被历史回填或用后一根冒充：所有跨缺口 pending
模拟订单必须先形成未成交回执，缺口写入 `session_gaps`，滚动特征重置；恢复后的
第一根完整 K 线只建立新基线，至少再取得一根连续完整 K 线后才允许产生候选。
该日永久保持 `full_session_complete=false/learning_eligible=false`，但后续完整
分钟可继续 observation、反事实、盯市和对账积累。服务只读取
`/run/secrets/tradingagent/tradingdatas-read.token`，仍只调用
`GET /v1/catalog` 与 `POST /v1/query`。

服务器停机或上游事故导致当日09:35首槽已错过时，不允许把事后数据回填成实时
模拟。对固定 3193 full-universe 路径，仍仅可人工运行一次
`Ashare.minute_auto_runner --allow-late-start`，并从当时最新、已完成且证据合格的
延迟K线建立当日状态；该路径必须有独立完整 500/500 canary。对
`--rolling-eligible` 路径，如果当天 gate 仍为 pending、尚未创建 state bundle，且
只是错过首槽或发生可重试事故，recurring paper timer 可以自动从当前完整 bar 建立
新的 partial session，不需要把固定 3193 canary 错当成 rolling 3186 的门禁。两条路径
都必须包含 `late_start=true`、实际跳过槽位数、`full_session_complete=false` 与
`learning_eligible=false`，不回填、不补历史、不跨缺口结算 pending；后续完整分钟可
继续 observation、反事实、盯市和对账积累，但该日不能进入完整交易日、模型晋级或离线
学习验收。任何单股或当前 bar 的数据/身份/lineage 失败仍只隔离该股或该窗口，不能让
其它已通过股票退出 rolling 模拟管道。

#### 动态证券池：rolling-eligible 与 full-universe 声明分离

Scale500 的固定 3193 身份集合只适用于明确的 full-universe 覆盖声明和历史 cohort
审计，不再作为模拟交易启动的总门禁。生产/候选运行使用同一份 reviewed source
snapshot 派生当前 `rolling_eligible` partition：

- 已满足当前主板、上市年龄、风险和数据条件的股票先进入模拟；运行结果的
  `universe_sha256` 绑定本窗口实际 active 集合，并另存 `source_universe_sha256`；
- 新股进入 `pending_listings`，记录 `symbol`、`listed_on`、`eligible_after` 和
  `listed_less_than_30_days`，到达资格日后在下一个自然窗口加入；
- 实际退市、停牌、风险警示、缺日线、缺分钟行或 receipt/lineage 不完整的股票只
  在该股票或该 shard 上 fail closed，不能拖住其它股票，也不能由其它股票替换；
- 当前 active 集合内部仍要求 exact row count、same observation、时间窗、分页、
  receipt、lineage 和消费者 readback。`usable_degraded` 只能作为明确范围的观察
  证据，不能冒充 active 模拟集合完整；
- rolling paper runner 对当前窗口实际返回的合法股票逐股消费：缺少分钟行的股票只
  写入本窗口 `missing_symbols`/coverage receipt，并在下一窗口重新查询；不能因为
  一只股票缺行而让整批回滚。单只股票发生 bar gap 时只重置该股票的 rolling baseline，
  其它股票继续产生 feature/candidate；跨窗口状态仍禁止用缺失 bar 补齐；
- 大于 100 只的分钟查询使用最多 4 路有界并行 shard；某个 shard 的 HTTP/transport
  请求失败时只保留其它成功 shard，并在本窗口 coverage receipt 的 `fanout_failures`
  与 `missing_symbols` 中记录。分页、合同、目录、replay、metadata 或全部 shard
  失败仍按整条 snapshot fail closed，不能把 partial coverage 宣称为完整覆盖；
- 覆盖率报告可以同时显示 `source_count`、`active_count`、`pending_count`、
  `excluded_count` 和 `coverage_ratio`，但 coverage 未达 100% 不得反向关闭已经
  逐股通过的模拟链。

每个通过的 delayed-paper 窗口在当日目录写入
`coverage-receipts/<HHMMSS>.json`，schema 为
`tradingagent.ashare.minute_coverage_receipt.v1`，绑定 source/effective universe
hash、source/active/accepted/pending/excluded 数量及 accepted/missing symbol 集合。
该 receipt 是覆盖和消费读回证据，不是资金、订单或 live authority。

Scale500 session timer 在 09:18、09:24、09:30、09:36 设置开盘前重试窗口，09:42
开始消费第一根已完成的 09:35 bar。单次 data/readiness 失败不得通过 `OnFailure`
关闭后续 timer；只有明确的权限、真实交易开关、状态损坏等不可恢复错误才允许人工
执行 rollback service。初始化发现旧分区 gate 与当前 effective hash/count 不一致时，
会先保留为 `.stale*.json` 历史证据，再创建当前分区的 pending gate。

滚动模式的首批合格股票即可激活积累，不以固定 cohort 的开盘两根连续 PASS
作为运行门禁；该两根验收仍保留给固定 cohort 模式。双读不一致仍拒绝本次所有
不可信输入、不给失败槽写成功回执，但不锁死以后独立的当前槽。运行中恢复缺口
必须带 `minute_session_gap_detected`、紧邻当前槽的缺失列表、
`full_session_complete=false` 和 `learning_eligible=false`；不得跨缺口补造历史 PIT。
认证拒绝、状态/账户损坏与真实交易开关不在可恢复分类中。
特别是 `minute_paper_state_invalid`、状态落盘失败和 universe 内容漂移，不能被
宽泛的 `minute_paper_*` 前缀误判为可恢复。可重试失败仍退出非零并输出
`retry_next_slot=true / selected_mode=rolling_eligible`，不得假报已锁存 rollback30。

30 股票基线服务显式启用 `--allow-late-start`：开盘读取失败后可在下一个当前槽
启动，保留缺口且不宣称全天完整或可学习。若现场有覆盖 `ExecStart` 的 event-aux
试验 drop-in，部署时必须保留 `--event-aux` 并同时加入恢复参数，不能只更新被遮蔽
的基础 unit。scale paper 的 180 秒查询预算外保留处理余量，service 总超时为
240 秒，小于 300 秒触发周期；现场旧 480 秒临时覆盖需备份后收口，不能被发布助手
的“保留有效 timeout”逻辑永久继承。任何现场调整都需核对有效配置并保留精确回退副本。

启动该模式的候选命令是在现有隔离服务参数上增加
`Ashare.minute_scale500_runtime initialize/run --rolling-eligible`。该开关保持
`REAL_TRADING_ENABLED=false`，不创建真实订单、资本 authority 或 live authority；
生产切换仍需分别验证当前 release、systemd、receipt/API 和 TA consumer readback。

安装候选：

```text
deploy/systemd/tradingagent-ashare-minute-paper.env.example
deploy/systemd/tradingagent-ashare-minute-paper.service
deploy/systemd/tradingagent-ashare-minute-paper.timer
```

timer 只在工作日48根可处理K线的延迟到达窗口触发：上午
`09:42–11:37`、下午`13:12–15:07`。每次计划触发在目标 bar end 后420秒，
含最多10秒随机延迟；实际 PIT/新鲜度仍由消费者逐批校验，调晚 timer 不会让过期数据合格。
午休后段和收盘后不再重复触发，因此已知
缺口只保留一次失败关闭证据，不会在无新K线时制造重复失败日志。启用前必须依次
通过：不可变 release/manifest 校验、
`systemd-analyze verify`、禁用状态手工 one-shot、状态 SHA 读回、下一轮自动
触发、资金/持仓/费用对账和重复快照不变。回滚只执行
`systemctl disable --now tradingagent-ashare-minute-paper.timer`，保留已有
append-only/原子 fixture 状态；不得删除 bundle 或恢复旧数据入口。

该 timer 只自动积累 `non_production_fixture`。它不生成次日参考文件，不连接
broker，不授予 durable capital，也不改变 `REAL_TRADING_ENABLED=false`。

2026-07-28 运行态：不可变 release
`437fa274f5cfc47bac6ae03f7a26270ec404659c` 已由
`/opt/investment/releases/tradingagent/current` 指向；secret-free env、分钟
service/timer与次日会话service/timer已安装并通过 `systemd-analyze verify`。
两项timer均为`enabled/active`，环境固定`REAL_TRADING_ENABLED=false`。
14:25:44首次分钟自动触发因手工状态停在13:45、13:50存在真实缺口而退出2；
timer继续排定，正式bundle SHA未变。该历史结果保持不改写；后续版本只允许按上述
分段恢复规则取消跨缺口 pending、记录缺口并重建基线。禁止以旧行配新receipt、
改写decision time或把缺口日升级为学习样本。启用timer只证明调度生效和门禁有效，
不证明已有成功自动模拟轮次。

次日输入候选由 `Ashare.minute_session_initializer` 在09:18准备。它只读取正式
catalog/query，使用交易日历的 `pretrade_date` 和审核过Universe的上一交易日
`daily.close`，并原子发布三项0600输入；不建立账本、不生成订单、不运行模拟
成交。对应候选为：

```text
deploy/systemd/tradingagent-ashare-minute-session.service
deploy/systemd/tradingagent-ashare-minute-session.timer
```

初始化器对相同输入精确幂等；目标目录已存在但字节不同、已有
`state-bundle.json`、日线降级、目录漂移或股票缺失时退出2。只有初始化器当日
PASS，分钟累计timer才会在09:35形成可推进的新会话；否则缺目录安全no-op或失败关闭。
二者均不授予真实交易权限。2026-07-29 盘后，正式catalog/query已提供
`20260730 is_open=1/pretrade_date=20260729`及审核30股上一交易日日线，隔离
initializer首次发布与精确幂等复用均通过；正式会话仍须由次日09:18 timer独立
初始化，不能用隔离证据代替生产运行结果。

盘后日线的 `observation_session=T` 只是 current observation。在预测前冻结且独立验证的交易日历没有给出下一 session 之前，daily-only planner 必须写 `paper_trade_session=null` 并固定 `action=abstain/status=completed_with_blocks`。每个 symbol 至少需要 21 个 forward-collected session 才能覆盖 20 日 momentum/volatility 的最小数学窗口；但缺交易日连续性和公司行动/复权 authority 时，即使计数达到 21 也仍是 blocked。当前 membership ledger 不注册任何 label horizon；缺 calendar/minute/market-truth/adjustment authority 不得生成或回填标签。缺分钟/L1 evidence 时不生成 capital/reservation/order/fill/outbox/reconcile/SampleJournal 副作用。

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

### 2.3 影子数值模型 one-shot（离线、零权限）

依赖无关的 M0 canary 可直接在仓库根运行；它只使用确定性 fixture，不读取 TradingDatas、
账户、密钥或网络，也不写现役状态：

```bash
REAL_TRADING_ENABLED=false python3 scripts/run_shadow_model_canary.py --backend ridge
REAL_TRADING_ENABLED=false python3 scripts/run_shadow_model_canary.py --backend logistic
```

LightGBM 必须安装在 learning-plane 专用 venv，不能把
`requirements-model-shadow.txt` 安装进 core/paper runtime venv：

```bash
python3 -m venv <isolated-shadow-model-venv>
<isolated-shadow-model-venv>/bin/python -m pip install \
  -r requirements-model-shadow.txt
REAL_TRADING_ENABLED=false \
  <isolated-shadow-model-venv>/bin/python \
  scripts/run_shadow_model_canary.py --backend lightgbm
```

验收输出必须同时满足：`fixture_only=true`、`shadow_only=true`、
`authority=none`、`execution_authority=false`、`capital_authority=false`、
`risk_expansion_allowed=false`、`automatic_promotion_enabled=false`、
`real_trading_enabled=false`、`model_network_used=false`。依赖版本漂移、无时区/未来特征、
标签在训练截止后才可见、单类二分类样本、特征合同漂移、模型/数据哈希不绑定或真实交易环境
开启时都必须失败关闭。

当前没有对应 systemd service/timer，也不得复用市场 core timer。Kronos、Chronos、TimesFM
此阶段只登记为后续 batch benchmark；不得下载权重、启常驻推理或把输出接入候选/仓位/订单。

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
python3 -m compileall -q shared Ashare CNFutures Crypto tools scripts
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

### 4.3 TradingCopilot 正式只读投影 one-shot

该入口只发布桌面 TradingCopilot 的只读个股投影，不是量化 day loop、候选生成器或交易服务。运行前必须保持 `REAL_TRADING_ENABLED=false`，使用现有 TA 只读 token-file，且输出到专用仓外 `0700/0600` root。证券主数据二选一：五项 committed A股 observation bundle，或显式的 `tradingagent.trading_copilot_company_facts.v1` 回执输入。两者不能混用。

```bash
export REAL_TRADING_ENABLED=false

python3 -m Ashare.trading_copilot_observation_worker \
  --minute-manifest /absolute/session/minute-manifest.json \
  --reference-facts /absolute/session/reference-facts.json \
  --observation-manifest /absolute/observation-manifest.json \
  --observation-state-root /absolute/committed/research-snapshots \
  --load-current-events \
  --event-evidence-artifact-root /absolute/private-staging/event-evidence \
  --event-timeline-output-root /absolute/private-runtime/event-timeline \
  --token-file /absolute/tradingdatas-read.token \
  --decision-time 2026-08-02T16:00:00+08:00 \
  --trading-date 2026-07-31 \
  --evidence-use historical_display \
  --valid-until 2026-08-03T09:00:00+08:00 \
  --batch-output /absolute/private-staging/projection-batch.json \
  --projection-output-root /absolute/private-runtime/stock-intelligence \
  --result-output /absolute/private-staging/worker-result.json
```

事件数据集由 `TradingCopilot/contracts/td_event_consumer_profile.v1.json` 声明：
公告、央视新闻、沪深互动问答和研报只经固定 catalog/query transport 读取，并逐数据集要求
freshness、receipt 与完整 lineage。`cn.dataset.major_news` 固定为 `on_demand` 宏观上下文；
它不会被上述 one-shot 自动读取，只有在同一次受控 TD 读取中显式追加
`--on-demand-event-dataset cn.dataset.major_news` 才可请求。其公开查询不支持 `as_of`，因此仍仅是
当前观察，不是历史 PIT、自动实时能力或交易 authority。`--event-bundle` 不可作为正式事件入口，
因为本地 JSON 不能替代 TradingDatas capability 与 runtime evidence。

`--event-evidence-artifact-root` 与 `--event-timeline-output-root` 必须成对地同
`--load-current-events` 使用，且均为显式绝对路径。前者只在 batch 完成 TD 读取及 runtime
evidence 校验后原子保留 receipt-bound typed artifact；写入失败时该 batch 不会被展平或发布。
后者只读取并再次校验这些 artifact，再调用现有 event timeline publisher；它不会重新查询 TD
或从 timeline JSON 重建事件。该路径仅用于离线/operator preflight；生产 caller、输出 root 与
启用仍需 Nicholas 另行授权，本示例不代表它已激活。

#### 4.3.1 08-07 事件保留 runbook（post-window）

分钟快照门禁（已逐项 runtime 验证，2026-08-07，Tradings ECS，`tradingagent` 身份）：

- 08-05/08-07 manifest 直接重放（`filters={}`）：`pagination_row_budget_exceeded`——manifest
  profile 声明 `max_pages=1/max_rows=30/page_limit=30`，`collect_query_pages` 在装下 30 只
  symbol 的全量 rt_min 结果集前按预算失败关闭。08-07 09:18 会话发布的新 manifest 同样如此，
  因此任何保留运行都必须携带与 paper runner 一致的查询形态。
- 以 root 直跑失败于 `tradingdatas_token_owner_invalid`：token 为 `tradingagent:tradingagent`
  `0600`，worker/canary 必须以 `User=tradingagent` 身份执行，不得以 root 直读。
- 仅加 `time eq + ts_code in` 固定查询、但 decision 仍取 09:40：`minute_evidence_time_order_invalid`
  ——会话首根 bar 在 gap-recovery 下实际晚于 330s 边界提交（同日 paper 09:40/09:45 亦
  `failed_closed`：`minutedatacontracterror`/`minute_auto_initial_bar_missing`；8369ac3 固定
  universe 后 11:25+ 恢复 `status=pass`）。
- 修复（候选）：worker 的分钟快照改为固定 universe（`ts_code.in` + `time eq`）并按
  `minute_auto_runner` 的 `PROVIDER_AVAILABILITY_LAG`（330s）边界取 decision；已用 08-07
  manifest 实测 bar 11:30/decision 11:35:30 通过（30 行、单页、same_observation=true）。
  `--decision-time` 应传运行时刻，worker 自动选取最近可用 bar。

窗口后执行（仅当新 manifest 重放通过；一次一个 vertical slice，输出到专用仓外 0700 root）：

```bash
runuser -u tradingagent -- env \
  REAL_TRADING_ENABLED=false \
  PYTHONPATH=/opt/investment/releases/tradingagent/<effective-release> \
  /opt/investment/tools/venvs/tradingagent-observation-py312-pyyaml603-v1/bin/python3 \
  -m Ashare.trading_copilot_observation_worker \
    --minute-manifest /var/lib/tradingagent/ashare-minute-paper/20260807/minute-manifest.json \
    --reference-facts /var/lib/tradingagent/ashare-minute-paper/20260807/reference-facts.json \
    --observation-manifest /var/lib/tradingagent/ashare-observation/manifests/ashare-observation-20260724-v4.json \
    --observation-state-root /var/lib/tradingagent/ashare-observation/pass-20260727T123136Z \
    --load-current-events \
    --event-evidence-artifact-root /var/lib/tradingagent/trading-copilot/event-evidence-batches-3e6933d \
    --event-timeline-output-root /var/lib/tradingagent/trading-copilot/event-timeline-3e6933d \
    --token-file /run/secrets/tradingagent/tradingdatas-read.token \
    --decision-time <运行时刻，如 2026-08-07T11:40:00+08:00> \
    --trading-date 2026-08-07 \
    --evidence-use historical_display \
    --valid-until <下一交易日 09:00+08:00，按 trade_calendar 核对> \
    --activity-authorities /var/lib/tradingagent/trading-copilot/activity-authorities-20260807.json \
    --batch-output /var/lib/tradingagent/trading-copilot/worker-batch-20260807.json \
    --projection-output-root /var/lib/tradingagent/trading-copilot/stock-intelligence \
    --result-output /var/lib/tradingagent/trading-copilot/worker-result-20260807.json
```

证券主数据 company-facts 取自 committed observation bundle（`ashare-observation`，07-24/07-26
receipt 绑定）：陈旧但可加载，投影按绑定 `dataThrough` 标注，不得改写为 fresh；`trading-copilot-canary`
目录最新为 08-02 分钟 canary 回执，不含 security_master company facts，不混用。若 Controller 指定
其它 company-facts 源，执行前须确认其 receipt 绑定与 data_through。

activity-authorities 构造（不得从展示 JSON 重建；取自 worker 同批 TD catalog/query 回执
envelope）：

- 数据集：`cn.dataset.rt_min`、`cn.equity.security_master`，以及会产出事件的
  `TradingCopilot/contracts/td_event_consumer_profile.v1.json` 五个 session_bounded 数据集
  （`anns_d`/`cctv_news`/`irm_qa_sh`/`irm_qa_sz`/`research_report`；`major_news` 仅当显式追加
  `--on-demand-event-dataset`）。
- 每个数据集做一次有界认证 `POST /v1/query`（token 只经 leaf 注入），从响应 envelope 取
  `receipt_id`、lineage sha、envelope proof sha、`data_through`、`observed_at`。
- 条目结构：`datasetId/market=ashare/timezone=Asia/Shanghai`、`calendar`
  （`sourceDatasetId=cn.market.trade_calendar` + receipt/lineage/calendarSha 三哈希）、
  `session.{state,asOf}`、`dataThrough`、`source.{receiptId,receiptSha256,lineageSha256}`。
  校验见 `_activity_authority`：`dataThrough` 与 source 三哈希必须等于 worker 自身查询产出值，
  任何不匹配失败关闭；构造错误不会被静默接受。示例结构见
  `tests/test_trading_copilot_observation_worker.py::_authorities`。

执行后 readback：

- 直接核对保留产物：`/var/lib/tradingagent/trading-copilot/event-timeline-3e6933d/<symbol>.json`
  与 `<symbol>.receipt.json`，`validUntil` 晚于当前时间；stdout `symbolCount` 为真实覆盖，
  `eventCoverage.blockedDatasetIds` 非空表示该事件数据集失败关闭，不得补位。
- 前端时间线路由当前读 `TRADING_COPILOT_EVENT_TIMELINE_DIR=/var/lib/tradingagent/trading-copilot/event-timeline`
  （既有 root，见 `front/docs/integration.md`），one-shot 写入 3e6933d root：按 integration.md
  的路由读取前端时间线前，须先由 Controller 把该 env 切到 3e6933d root（可回退服务 env 变更 +
  healthz 回读）；未切换前 404 是预期状态，不代表保留失败。000001.SZ 与 000333.SZ 各读一次，
  200 且 validUntil 有效才记为已消费。
- 本 runbook 不授权 provider 重放、事件合成、模型晋级或任何订单 authority；结果按
  `AUTODEV_HANDOFF_V1` 交 Controller 验收。

首次启用分钟 session root 时，不能靠“已启用 timer”假定存在历史模板。必须由发布侧准备一个
仓外、已审核的 minute manifest 和 universe artifact，并仅在空 root 的第一次 session 初始化时
显式传入 `--bootstrap-manifest` 与 `--universe-source`。bootstrap 不会跳过 catalog、日历、
日线、分页、同观察重放或 receipt 门禁；一旦 root 已有历史 session，后续 bootstrap 被拒绝，
避免不透明地切换 universe 或数据合同。

服务器首次启动使用独立、不可 enable 的 `tradingagent-ashare-minute-bootstrap.service`；它只可由
发布侧在首次交易日前手工执行一次。两个 `/etc/tradingagent/ashare-minute-bootstrap-*.json`
输入均须由发布侧审核、`root:root` 拥有且不可写，token 继续只由专用 leaf 注入。bootstrap 成功
后读回当日目录的三份输入和 service journal，再由既有 session/paper timer 在后续交易日运行；
不得把 bootstrap service 安装成周期性 timer。

minute session 与 bootstrap 都会把已验证 session 实际使用的 symbol/name 集合原子投影到
`/var/lib/tradingagent/trading-copilot/tracking-universe.json`。该目录由受控 tmpfiles 阶段以
`tradingagent:tradingagent`、`0700` 创建；前端 API 只读此路径，文件缺失或合同无效时返回
unavailable，绝不以静态名单、报价或预测替代。

退出 `0` 只证明本批 projection/receipt 已原子发布。`delayed_paper` 仍要求一个 bar cadence 加 jitter；休市日显式使用 `historical_display` 时只允许展示带当前查询回执的旧行情并固定标记 `stale`，它不证明历史 first-seen/revision 链，不取得 historical-PIT 训练、延迟模拟或执行资格。stdout 的 `symbolCount` 是本批真实覆盖；`eventCoverage.blockedDatasetIds` 非空表示相应事件数据集失败关闭，不能用摘要、缓存或演示事件补位。输出中的 `forecast=null` 是正常停止线，只有另行通过冻结OOS、校准、覆盖率、费用与基线门禁后才可发布正式预测。

Kronos/基线评估只消费外部冻结的同样本预测，不下载权重、不训练、不晋级：

```bash
python3 -m Ashare.trading_copilot_forecast_evaluation \
  --input /absolute/frozen/forecast-evaluation-input.json \
  --output /absolute/private-research/forecast-evaluation.json
```

`eligible_for_shadow_comparison` 仍然只能进入研究影子比较。不得据此修改 Quant Core Champion、Copilot概率展示、资本、订单、broker、timer或生产路由。

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
2. TradingDatas V1 请求包含必填 `schema_major`，并按 dataset 使用显式 filters、`query_as_of_mode`、identity/event mapping 与 page/row budgets；`order` 省略时由 registry 默认排序。完整 probe 必须遍历到 terminal page并完成同一 observation 双跑，跨页 metadata/identity/顺序或预算异常一律阻断。provider-native rows 不补造行级 receipt/available/revision；不可用数据、null source proof 或 `historical_pit_eligible=false` 不能被其它健康 dataset 洗白。
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

## 8. 有效运行版本只读核验

仓库 `HEAD`、远端 `main`、immutable release、`current` symlink、systemd 的
effective configuration 和正在运行的进程是六项独立事实。发布或验收时不得只读
其中一项就声称 runtime 已同步。只读解析器：

```bash
python3 tools/effective_runtime_release.py \
  --unit tradingagent-ashare-minute-paper.service
```

解析器只向 `systemctl show` 请求 unit/load/active/PID/ExecStart/WorkingDirectory
等非 secret 字段，再从 `/proc/<pid>` 读取 cwd/exe/cmdline 的 release path；不读取
Environment、token file、状态账本或服务日志。输出分别保留：

- `current_release`：当前 symlink 的 immutable target；
- `unit_release_refs`：systemd effective ExecStart/Pre/WorkingDirectory 引用；
- `process_release_refs`：运行进程实际引用；
- `effective_release/effective_source` 与 `current_matches_effective`；
- `runtime_verified` 和精确 blockers。

active unit 缺 PID、进程无法绑定 release、unit/process 引用不同 release、一个层级
引用多个 release、`current` 无法安全解析时统一退出 2。inactive unit 即使能解析
声明 release 也固定 `runtime_verified=false`。该工具只消除版本误报，不执行切换、
重启、daemon-reload、timer 或回滚。

### 8.1 独立模拟运行的只读展示

`tools/read_runtime_observations.py` 只读现有 A股分钟 state bundle 和 G5 epoch，
不启动行情请求、模拟执行、初始化、补写或修复。A股复用 `MinuteFixtureClosedLoop.restore`
验证完整状态/hash，从已验证 bar 集合计算覆盖；不信任外层未封存的 receipt 计数，
也不把四份反事实资金相加。G5 复用现有 round-trip health 的无写读取，保留资本 head、
数据时槽和模拟账务，不能作为 exchange/live 或统一账户 authority。

既有 snapshot API 通过两个 server-only 配置启动固定 Python/script 子进程：
`TRADING_AGENT_RUNTIME_PYTHON`、`TRADING_AGENT_RUNTIME_READER`。生产值见已跟踪
`deploy/systemd/tradingagent-front-api.service`；无配置则不启动，HTTP 参数不能改命令。
首次/过期请求只触发后台读取，页面不等待；单进程单飞、30 秒超时、64 KiB 输出上限、
5 分钟缓存，缓存保留原时间，失败清除旧成功。无新 route、worker、timer、权限组或
可写路径；front 继续不能访问 secrets。All Markets 不汇总或并列显示金额。

A股最多查找近 8 个自然日；长假或长期停机后的 unavailable 不代表历史记录不存在。
普通来源错误按市场隔离；共享子进程整体超时会撤下两个展示项，但不影响各市场运行。

候选检查：`REAL_TRADING_ENABLED=false PYTHONDONTWRITEBYTECODE=1 python3 -B -m pytest -q
tests/test_read_runtime_observations.py tests/test_tradingagent_service_identity.py`，以及
frontend README 的 lint/test/build 检查。独立模拟展示失败只影响该展示块，不影响
采集、资金、其它市场或既有 snapshot 领域。

发布时先保留现役 front unit 的原始字节/hash，再以验收后的 immutable release 中
同名 unit 安装两个读取路径配置，保持用户、组、只读 sandbox、localhost 和秘密隔离
不变。既有 release helper 不会自动安装 front unit，必须独立核对安装字节并读回。
回退恢复旧 unit 与代码，绝不恢复旧账本。前端观察值必须标明原数据日期；恢复目录里
存在资金政策或账本，不等于现役服务已连接。迁移账本/调整唯一写者权限另行确认。
启用带金额的展示前还必须验证实际入口：后端监听 localhost、CORS 或代理注入 Bearer
均不能证明公网用户已认证。无单用户认证时使用
`deploy/nginx/tradingagent-snapshot-local-only.conf` 限制现有精确 snapshot location，
不增加宽泛 `/api/` 代理。检查 effective nginx 的 real-IP 重写及其它代理入口；
保留原文件/hash，候选和安装态均须 `nginx -t`，再验证真实非loopback来源被 nginx
拒绝、localhost仍可读。外层 ICP/Cloudflare 错误不是 origin 权限证据。其它站点、
DNS、静态 root 和健康路由保持不动。展示失败时回退读取器，不能为恢复页面而撤销
已建立的访问限制。

四十币既有 daily rolling-eval service 的包装器也需与 CLI 同批更新：备份
`/usr/local/sbin/tradingagent-crypto-rolling-eval.sh` 的原始字节/hash，在服务不运行时
以 accepted immutable release 内 `deploy/run-crypto-forty-symbol-rolling-eval.sh`
安装至该原位置，保持 root:root 0755。不新增/启用 timer，不修改观察器，不调用资本。
受控运行只向已有独立研究 output root 新建唯一 attempt；失败不会覆盖报告或追加
成功日志。回退恢复包装器和代码，保留研究输出。测试入口为
`tests/test_crypto_forty_symbol_rolling_entrypoint.py`。

### 8.2 原 A股账户的原样搬迁与只读展示

这不是 fresh-start、旧共享资金导入或交易启动。只有明确授权搬迁既有
`ashare-capital-v1` 与匹配 execution lineage 后才执行；禁止 `init/bootstrap`、
刷新旧投影时间戳、重置现金、改变 generation 或重建成交。先确认源没有打开文件和
现役写入任务，持有既有账本/执行锁，再复制两个精确目录及全部 reconcile sources。
用现有 canonical replay、lineage/outbox verifier 检查原件及副本，记录所有文件 hash、
权限与账本 head；不复制整个 recovery 工作树或另一个市场。

服务器的独立目标根是 `/var/lib/tradingagent/ashare-canonical`。资本子路径仍为
`shared/logs/capital/ashare`，执行子路径仍从已验证 snapshot 的
`execution_lineage_id` 派生到 `shared/logs/execution_lineages/<id>`。未来受验收的
runtime 可显式使用既有 `TRADINGAGENT_ASHARE_CAPITAL_ROOT` 和
`TRADINGAGENT_ASHARE_EXECUTION_ROOT`；本次搬迁不安装这些变量、不启用 legacy executor
或新 scheduler，也不把此根接到反事实分钟资金簿。

新根父目录由 root 所有，两个数据子树为 `tradingagent:tradingagent`、目录0700/
文件0600。原件保留原字节，改为 root 所有、目录0500/文件0400，并对精确原件集合
设置 immutable；元数据清单和本次回退路径记录在私有 migration receipt。应用只读
验证必须以实际服务身份重放；权限验证可以打开现有文件的 append descriptor 后立即
关闭（无create/truncate、无写入），并复核 hash：新writer可写，旧writer不可写，
front mount namespace仍因 `ProtectSystem=strict` 不可写。root管理权不是第二个业务writer。

失败时停止目标使用并保留所有新事实；不得用旧副本覆盖目标。只有确认目标已无writer、
无新增事件且仍与原head一致，才可根据保存的精确metadata清单撤销原件immutable与权限。
不要对 recovery 根或其它市场递归解冻，也不要自动恢复旧任务。

搬迁验收只证明完整性和写入身份就绪。行情/交易日历/Champion适配器及资本组合入口
未接通时，必须继续报告 `canonicalAccountConnected=false`；旧账本最后交易时间不
因搬迁而变新，空仓旧账本也不能冒充当前收益。旁路模拟样本照常独立积累。

## 9. 按能力划分的外部依赖

以下各项只约束依赖它的能力，不是所有代码、只读展示、普通 dataset 接入或模拟
积累的统一发布清单。局部可回退变更完成对应测试、回退和结果读回后独立交付；
20 日、60–120 日及历史 PIT 条件不得阻断已合格 symbol/slot 的当前观察或受控模拟。
已经成立的上游证据保留，下游失败单独报告：

1. TradingDatas owner handoff 冻结的 base URL、catalog version、dataset IDs、schema、filters/as-of policy、auth/receipt authority，以及 TA 使用自身 token/client 完成的 fresh readback；上游声明不能替代 TA 独立复现；
2. 本次切换的 A 股消费者完成同 `as_of` parity、V1 cutover、旧引用清零和 runtime no-fallback 负例；不等待无关消费者；
3. 每个predictive dataset的首次可见时间、release/revision链、first-seen receipt和训练时vintage；无法还原历史回填版本的数据不得进入历史训练；
4. PIT证券主数据覆盖上市/退市、板块迁移、ST/风险警示、停复牌和历史指数/行业成员，证明没有用当前存续集合回填过去Universe；
5. 统一资本/执行接线需其 market-evidence、Champion/数值特征 registry 与可信时钟，费用后科学结论需独立 metrics 重算；真实会话、crash/restart、对账及 20 个交易日证明运行成熟度，不是首次启动模拟的前置；
6. 60–120 个交易日、费用后统计置信度、回撤和状态分层仅约束长期策略结论及相应风险能力，不阻断只读发布、数据积累或独立模拟；
7. DeepSeek若启用，会话中曾暴露的credential必须先由供应商侧revoke/rotate，新值不得入仓；还需真实模型/请求字段readback、quota/限流/幂等/数据留存核验、敏感数据门、提示注入语义/编码变体、引用绑定、typed receipt持久化、成本/延迟和冻结增量评测，且仍保持evidence-only。首版固定单次调用、无自动重试；未来是否保留或变更该策略必须另立评审，不能在运行时静默开启；
8. standing release authorization下仍须完成当次preflight、回退方案，以及本地、Git、远端、生产文件、生产runtime和外部路由的分别验收；授权不能替代证据。

`server_validated_non_authority_simulation_only`只描述目标服务器安装与旁路验证，
不自行升级为当前运行、完整账户接线或实盘能力。已经运行的组件以同轮
`STATUS.md` 与直接 runtime/receipt/readback 分层确认，不使用本节静态文字覆盖新证据。


### Existing minute-session failure diagnosis

The initializer preserves a small allowlist of data-contract reason codes, including
`minute_dataset_contract_drift` and catalog limit mismatches. Unknown contract errors
remain `minute_session_data_contract_invalid`, without provider payloads or credentials.
A scale-session contract rejection can leave the paper consumer with
`minute_scale500_session_inputs_missing`; diagnose and rebind the reviewed upstream
contract before the next session rather than relaxing fingerprint checks, inventing
session inputs, or clearing systemd failure state. A dated failed oneshot does not
prove that the same failure has run again on the current release.

A reviewed catalog contract update can be rebound for **future** sessions with
`ASHARE_MINUTE_ACCEPTED_DATASET_CONTRACT_FINGERPRINT` in the existing minute
session environment file. The value must be one exact lowercase SHA-256. The
initializer substitutes that reviewed binding into its copied profile and still
requires equality with the authenticated current catalog on every run; it
recomputes the consumer profile digest. Missing configuration preserves the old
template binding. This does not accept arbitrary future drift, change historical
manifests, or authorize strategy/execution changes. Review the complete per-dataset
contract difference before setting it; save the previous environment value for
rollback. A closed-day preflight is not proof of a successful open-day session.
The bootstrap manifest option is only used when no prior session exists, so it
cannot rebind an established session history.


### Release-helper rollback coverage

A rollback after a successful cut must preserve the installed coordinator's full
managed-unit coverage. An older helper missing the ten-symbol units is rejected
before self-refresh or cutover; names mentioned only in comments do not count.
Retain the full pre-cut drop-in/timeout/helper/service-state snapshot separately
from temporary deployment files and follow [the complete rollback procedure](DEPLOYMENT.md#automatic-rollback).
In-flight failure rollback and a later operator-requested rollback are distinct.
Neither may rewrite append-only simulation facts or restore old data over new data.

### 纯文档 CI 快路径

CI 只对 `.github/scripts/docs_fast_path.py` 中逐项审核的文档路径启用快路径，且必须全部为已有普通文件的内容修改。PR 比较基线到候选，push 比较整个推送范围；自动合并后的 exact-main dispatch 显式启用 `docs_fast_path` 并比较该提交与第一父提交。缺少基线、空差异、未知路径、代码/配置/工作流、文件新增/删除/重命名、符号链接或权限变化均走原测试。`docs/AGENT_INTEGRATIONS.md` 等产品编译输入不在白名单，不能按 Markdown 后缀推断安全。

快路径保留现有 required check 名称，只验证变更文档的差异、非空 UTF-8 内容；不安装运行依赖，不声称代码测试或生产验收已完成。普通手动 dispatch 默认完整测试。文档正文、规则与链接正确性仍由变更审核负责，不新增等待天数或审批流程。分类边界回归可运行 `python3 tests/test_docs_fast_path.py`。回退时 revert 本批 CI 变更即可恢复原测试，无生产状态或数据迁移。

文档快路径不构建、不上传 release artifact，不能提供部署证据；现有部署工作流仍要求精确主线 push 的成功测试与对应 artifact。后续代码变更继续完整测试、构建和打包，文档更新无需重启生产。
