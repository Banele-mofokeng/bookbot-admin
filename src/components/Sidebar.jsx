import { NavLink } from 'react-router-dom'

const NAV = [
  {
    to: '/', label: 'Queue',
    icon: <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 4h12M2 8h8M2 12h5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/></svg>
  },
  {
    to: '/services', label: 'Services',
    icon: <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><rect x="2" y="2" width="5" height="5" rx="1.5" stroke="currentColor" strokeWidth="1.3"/><rect x="9" y="2" width="5" height="5" rx="1.5" stroke="currentColor" strokeWidth="1.3"/><rect x="2" y="9" width="5" height="5" rx="1.5" stroke="currentColor" strokeWidth="1.3"/><rect x="9" y="9" width="5" height="5" rx="1.5" stroke="currentColor" strokeWidth="1.3"/></svg>
  },
  {
    to: '/agents', label: 'Agents',
    icon: <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="6" cy="5" r="2.5" stroke="currentColor" strokeWidth="1.3"/><path d="M2 13c0-2.2 1.8-4 4-4s4 1.8 4 4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/><circle cx="11.5" cy="5.5" r="1.8" stroke="currentColor" strokeWidth="1.3"/><path d="M13.5 13c0-1.6-1-2.9-2.5-3.3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/></svg>
  },
]

export default function Sidebar({ isOpen, onClose }) {
  return (
    <>
      {/* Dark overlay — only visible on mobile when open */}
      <div
        className={`sidebar-overlay${isOpen ? ' sidebar-open' : ''}`}
        onClick={onClose}
        aria-hidden="true"
      />

      <aside className={`sidebar${isOpen ? ' sidebar-open' : ''}`}>
        {/* Brand */}
        <div style={{ padding: '24px 24px 20px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <div style={{ width: 32, height: 32, background: 'var(--accent)', borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center', marginBottom: 10 }}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path d="M2 4h12M2 8h8M2 12h5" stroke="#0b0b0e" strokeWidth="1.8" strokeLinecap="round"/>
              </svg>
            </div>
            <div style={{ fontSize: 14, fontWeight: 800, letterSpacing: '0.04em' }}>QUEUEBOT</div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--muted)', marginTop: 2, letterSpacing: '0.08em' }}>ADMIN PANEL</div>
          </div>
          {/* Close button — only useful on mobile, hidden on desktop via the overlay click */}
          <button
            onClick={onClose}
            aria-label="Close menu"
            style={{
              background: 'none', border: 'none', color: 'var(--muted)',
              padding: 6, cursor: 'pointer', borderRadius: 6,
              display: 'flex', alignItems: 'center',
            }}
          >
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <path d="M4 4l10 10M14 4L4 14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"/>
            </svg>
          </button>
        </div>

        {/* Nav links */}
        <nav style={{ padding: '12px 0', flex: 1 }}>
          {NAV.map(({ to, label, icon }) => (
            <NavLink key={to} to={to} end={to === '/'} onClick={onClose}
              style={({ isActive }) => ({
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '11px 24px', fontSize: 13, fontWeight: 600, letterSpacing: '0.03em',
                color: isActive ? 'var(--accent)' : 'var(--muted2)',
                textDecoration: 'none',
                borderLeft: `2px solid ${isActive ? 'var(--accent)' : 'transparent'}`,
                background: isActive ? 'var(--accent-dim)' : 'transparent',
                transition: 'all 0.15s',
              })}>
              {icon}{label}
            </NavLink>
          ))}
        </nav>

        <div style={{ padding: '16px 24px', borderTop: '1px solid var(--border)' }}>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--muted)', letterSpacing: '0.06em' }}>MVP v0.2</div>
        </div>
      </aside>
    </>
  )
}
