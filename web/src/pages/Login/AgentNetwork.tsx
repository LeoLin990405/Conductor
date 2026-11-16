import './AgentNetwork.css';

const NODES = [
  { x: 110, y: 170, r: 8, delay: '0s' },
  { x: 420, y: 120, r: 6, delay: '.4s' },
  { x: 510, y: 330, r: 9, delay: '.9s' },
  { x: 180, y: 390, r: 6, delay: '1.4s' },
  { x: 300, y: 520, r: 10, delay: '.2s' },
  { x: 460, y: 610, r: 7, delay: '1.1s' },
  { x: 120, y: 650, r: 5, delay: '1.8s' },
  { x: 360, y: 250, r: 5, delay: '.7s' },
];

/**
 * The split login's visual pane: a live agent graph.
 * A hub fans work out to workers, workers report back, and the edges carry the
 * pulse of dispatch. Pure SVG/CSS, so it costs no canvas loop and no network.
 */
export default function AgentNetwork() {
  const hub = { x: 300, y: 370 };
  return (
    <div className="agent-network" aria-hidden="true">
      <div className="agent-network__grid" />
      <svg className="agent-network__svg" viewBox="0 0 620 800" preserveAspectRatio="xMidYMid slice">
        <defs>
          <radialGradient id="agent-hub" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--login-ember)" stopOpacity="0.95" />
            <stop offset="100%" stopColor="var(--login-ember)" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="agent-edge" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="var(--login-ember)" stopOpacity="0.55" />
            <stop offset="100%" stopColor="var(--login-ember)" stopOpacity="0.06" />
          </linearGradient>
        </defs>
        <circle cx={hub.x} cy={hub.y} r="120" fill="url(#agent-hub)" opacity="0.18" />
        <g className="agent-network__edges">
          {NODES.map((n, i) => (
            <path
              key={`e${i}`}
              d={`M ${hub.x} ${hub.y} Q ${(hub.x + n.x) / 2 + (i % 2 ? 22 : -22)} ${(hub.y + n.y) / 2} ${n.x} ${n.y}`}
              stroke="url(#agent-edge)"
              strokeWidth={i % 3 === 0 ? 2 : 1.2}
              fill="none"
              style={{ animationDelay: `${i * 0.18}s` }}
            />
          ))}
        </g>
        <circle cx={hub.x} cy={hub.y} r="34" className="agent-network__hub-ring" />
        <circle cx={hub.x} cy={hub.y} r="12" className="agent-network__hub" />
        {NODES.map((n, i) => (
          <g key={`n${i}`}>
            <circle cx={n.x} cy={n.y} r={n.r * 2.6} className="agent-network__halo" style={{ animationDelay: n.delay }} />
            <circle cx={n.x} cy={n.y} r={n.r} className="agent-network__node" style={{ animationDelay: n.delay }} />
          </g>
        ))}
      </svg>
      <div className="agent-network__labels">
        <span>PLAN</span>
        <span>DISPATCH</span>
        <span>SYNTHESIZE</span>
      </div>
    </div>
  );
}
