export function LiveGate({ onDismiss }: { onDismiss: () => void }) {
  return (
    <div className="live-gate-overlay" role="dialog" aria-label="实盘接入状态">
      <strong>实盘待接入</strong>
      <p>这里先展示模拟盘结果。实盘开启前不会显示真实资金；账户、风控和成交回执确认完成后再切换。</p>
      <button onClick={onDismiss} type="button">知道了</button>
    </div>
  )
}
