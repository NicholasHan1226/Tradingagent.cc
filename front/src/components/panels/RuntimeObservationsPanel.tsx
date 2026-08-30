import type { Market } from '../../types/dashboard'
import { parseRuntimeObservations, type RuntimeObservationEntry, type RuntimeObservations } from '../../types/runtimeObservations'
import { PanelTitle } from '../PanelTitle'

const STATUS_LABELS: Record<RuntimeObservationEntry['status'], string> = {
  ready: '记录可读', dated: '数据滞后', pending: '运行中 / 等待结果', unavailable: '读取不可用', invalid: '数据损坏 / 校验未通过',
}
const REASON_LABELS: Record<string, string> = {
  runtime_refresh_pending: '后台正在重验，下一次页面刷新会自动读取结果。',
  runtime_reader_configuration_invalid: '读取配置不可用。',
  runtime_reader_timeout: '后台重验超时，本次未提供运行数据。',
  runtime_reader_failed: '后台读取失败，本次未提供运行数据。',
  runtime_reader_output_invalid: '运行数据损坏或未通过合同校验。',
  source_missing: '本次查找范围内未找到运行记录，不代表历史记录不存在。',
  source_validation_failed: '来源记录未通过完整校验。',
  writer_in_progress: '模拟运行正在写入，等待完整记录。',
  independent_research_account_not_connected: '独立研究覆盖；未接入正式模拟账户。',
  dated_simulated_ledger_not_live_account: '独立模拟账本；未连接真实账户。',
}
const timestamp = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})

export function RuntimeObservationsPanel({ observations, activeMarket }: { observations?: RuntimeObservations; activeMarket: Market }) {
  const validated = parseRuntimeObservations(observations)
  const entries = validated?.entries.filter((item) => activeMarket === 'All Markets' || item.market === activeMarket) ?? []
  const localRefresh = validated?.entries.some((item) => item.reason === 'runtime_refresh_pending')
  const localFailure = validated?.entries.some((item) => item.reason.startsWith('runtime_reader_'))
  return (
    <section className="panel rail-panel runtime-observations-panel" aria-label="独立模拟运行">
      <PanelTitle kicker="只读观测" title="独立模拟运行" />
      <p className="runtime-observations-boundary">延迟研究 · 账户未接入 · 不计入账户、收益或成熟度</p>
      {!entries.length && <p className="maturity-empty">{
        observations === undefined ? '运行观测未配置或未提供。'
          : !validated ? '运行数据损坏或未通过合同校验。'
            : validated.entries.length === 0 ? '暂无运行记录。' : '所选市场暂无独立模拟运行记录。'
      }</p>}
      {entries.map((item) => <Observation key={item.id} item={item} showMoney={activeMarket !== 'All Markets'} />)}
      {activeMarket === 'All Markets' && <p className="runtime-observations-note">仅并列非货币计数；选择单一市场查看独立账本金额。</p>}
      {validated && <footer>{localRefresh ? '刷新发起时间' : localFailure ? '读取状态时间' : '核验时间'} {timestamp.format(new Date(validated.generatedAt))} (UTC+8) · 缓存保留原时间</footer>}
    </section>
  )
}

function Observation({ item, showMoney }: { item: RuntimeObservationEntry; showMoney: boolean }) {
  const showData = item.status === 'ready' || item.status === 'dated'
  const reason = Object.hasOwn(REASON_LABELS, item.reason) ? REASON_LABELS[item.reason] : '来源未提供可识别的原因说明。'
  return (
    <article className="runtime-observation" aria-label={item.market === 'A-share' ? 'A股分钟覆盖' : 'Crypto 独立模拟账本'}>
      <header><strong>{item.market === 'A-share' ? 'A股 · 分钟覆盖' : 'Crypto · 独立模拟账本'}</strong><span data-status={item.status}>{STATUS_LABELS[item.status]}</span></header>
      <p>数据时间 {item.observedAt ? <time dateTime={item.observedAt}>{timestamp.format(new Date(item.observedAt))} (UTC+8)</time> : '未提供'}</p>
      {showData && <dl className="runtime-observation-metrics">
        {item.coverage && <>
          <Metric label="股票池" value={item.coverage.universe} />
          <Metric label="覆盖" value={item.coverage.accepted} />
          <Metric label="缺失" value={item.coverage.missing} />
        </>}
        {item.simulation && <>
          {showMoney && <>
            <Metric label="账本现金" value={`${item.simulation.cash} ${item.simulation.currency}`} />
            <Metric label="账本权益" value={`${item.simulation.equity} ${item.simulation.currency}`} />
            <Metric label="累计费用" value={`${item.simulation.fees} ${item.simulation.currency}`} />
          </>}
          <Metric label="模拟仓位" value={item.simulation.positions} />
          <Metric label="模拟订单回执" value={item.simulation.orders} />
        </>}
        {item.counts && <><Metric label="完成观测轮次" value={item.counts.completed} /><Metric label="数据拒收" value={item.counts.rejected} /></>}
      </dl>}
      <p className="runtime-observations-note">{item.market === 'A-share' ? '数据时间为覆盖来源的观测时间。' : '数据时间为市场时段，不代表账本权益截止时刻。'}</p>
      {reason && <p className="runtime-observations-note">{reason}</p>}
      {item.status === 'dated' && <p className="runtime-observations-note">历史记录；不代表当前收益或持续稳定运行。</p>}
    </article>
  )
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>
}
