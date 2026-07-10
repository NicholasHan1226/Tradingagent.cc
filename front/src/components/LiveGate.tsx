export function LiveGate({
  detail,
  onUseSimulation,
  title,
}: {
  detail: string
  onUseSimulation: () => void
  title: string
}) {
  return (
    <section className="live-gate" role="region" aria-label="实盘接入状态">
      <span>模拟盘参考</span>
      <strong>{title}</strong>
      <p>{detail}</p>
      <div className="live-gate-requirements" aria-label="实盘接入要求">
        <span><b>01</b>账户授权</span>
        <span><b>02</b>风险校验</span>
        <span><b>03</b>成交回执</span>
      </div>
      <small>接入完成前不展示真实资金，也不提供下单或确认交易入口。</small>
      <button onClick={onUseSimulation} type="button">返回模拟盘</button>
    </section>
  )
}
