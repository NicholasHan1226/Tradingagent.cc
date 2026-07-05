export function LiveGate({ onDismiss }: { onDismiss: () => void }) {
  return (
    <div className="live-gate-overlay" role="dialog" aria-label="实盘接入状态">
      <strong>真实账户结果暂不展示</strong>
      <p>这里先展示模拟盘结果。账户、风控和成交回执确认完成后，再切换到真实账户。</p>
      <button onClick={onDismiss} type="button">知道了</button>
    </div>
  )
}
