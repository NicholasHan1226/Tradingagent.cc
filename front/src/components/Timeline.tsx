import { CircleDot } from 'lucide-react'

export function Timeline({ steps, labels }: { steps: number; labels: string[] }) {
  return (
    <div className="timeline">
      <div className="timeline-labels">
        {labels.map((label) => (
          <span key={label}>{label}</span>
        ))}
      </div>
      <div className="timeline-track">
        <i style={{ width: `${Math.min(steps, 6) * 16.7}%` }} />
        {labels.map((label, index) => (
          <b key={label} className={index < steps ? 'on' : ''} style={{ left: `${index * 16.7}%` }}>
            <CircleDot size={11} />
          </b>
        ))}
      </div>
    </div>
  )
}
