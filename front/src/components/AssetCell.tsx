export function AssetCell({ symbol, name }: { symbol: string; name: string }) {
  return (
    <span className="asset-cell">
      <strong>{symbol}</strong>
      <em>{name}</em>
    </span>
  )
}
