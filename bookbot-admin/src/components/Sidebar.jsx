import { NavLink } from 'react-router-dom'

const NAV = [
  {
    to: '/',
    label: 'Appointments',
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <rect x="2" y="3" width="12" height="11" rx="2" stroke="currentColor" strokeWidth="1.3"/>
        <path d="M5 2v2M11 2v2M2 7h12" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      </svg>
    ),
  },
  {
    to: '/tenants',
    label: 'Businesses',
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <circle cx="6" cy="5" r="2.5" stroke="currentColor" strokeWidth="1.3"/>
        <path d="M2 13c0-2.2 1.8-4 4-4s4 1.8 4 4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
        <circle cx="11.5" cy="5.5" r="1.8" stroke="currentColor" strokeWidth="1.3"/>
        <path d="M13.5 13c0-1.6-1-2.9-2.5-3.3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      </svg>
    ),
  },
  {
    to: '/tenants/new',
    label: 'Add Business',
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.3"/>
        <path d="M8 5v6M5 8h6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      </svg>
    ),
  },
]

export default function Sidebar() {
  return (
    <aside style={{
      width: 220, minHeight: '100vh',
      background: 'var(--surface)',
      borderRight: '1px solid var(--border)',
      display: 'flex', flexDirection: 'column',
      position: 'fixed', top: 0, left: 0, bottom: 0,
      zIndex: 100,
    }}>
      {/* Logo */}
      <div style={{ padding: '24px 24px 20px', borderBottom: '1px solid var(--border)' }}>
        <div style={{
          width: 32, height: 32, background: 'var(--accent)',
          borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center',
          marginBottom: 10,
        }}>
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path d="M2 4C2 2.9 2.9 2 4 2h8c1.1 0 2 .9 2 2v6c0 1.1-.9 2-2 2H9l-3 2v-2H4c-1.1 0-2-.9-2-2V4z" fill="#0b0b0e"/>
          </svg>
        </div>
        <div style={{ fontSize: 14, fontWeight: 800, letterSpacing: '0.04em' }}>BOOKBOT</div>
        <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--muted)', marginTop: 2, letterSpacing: '0.08em' }}>ADMIN PANEL</div>
      </div>

      {/* Nav */}
      <nav style={{ padding: '12px 0', flex: 1 }}>
        {NAV.map(({ to, label, icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            style={({ isActive }) => ({
              display: 'flex', alignItems: 'center', gap: 10,
              padding: '10px 24px',
              fontSize: 13, fontWeight: 600, letterSpacing: '0.03em',
              color: isActive ? 'var(--accent)' : 'var(--muted)',
              textDecoration: 'none',
              borderLeft: `2px solid ${isActive ? 'var(--accent)' : 'transparent'}`,
              background: isActive ? 'var(--accent-dim)' : 'transparent',
              transition: 'all 0.15s',
            })}
          >
            {icon}
            {label}
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div style={{ padding: '16px 24px', borderTop: '1px solid var(--border)' }}>
        <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--muted)', letterSpacing: '0.06em' }}>
          MVP v0.1
        </div>
      </div>
    </aside>
  )
}
