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

const SUPER_NAV = [
  {
    to: '/admin/businesses', label: 'Businesses',
    icon: <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 14V6l6-4 6 4v8" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/><path d="M6 14V9h4v5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/></svg>
  },
]

export default function Sidebar({ isOpen, onClose, onLogout, isSuper }) {
  const navItems = isSuper ? [...NAV, ...SUPER_NAV] : NAV
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
            <div style={{ width: 36, height: 36, background: 'linear-gradient(135deg, #6366f1 0%, #818cf8 100%)', borderRadius: 10, display: 'flex', alignItems: 'center', justifyContent: 'center', marginBottom: 12, boxShadow: '0 0 18px rgba(99,102,241,0.35)' }}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path d="M2 4h12M2 8h8M2 12h5" stroke="#fff" strokeWidth="1.8" strokeLinecap="round"/>
              </svg>
            </div>
            <div style={{ fontSize: 14, fontWeight: 800, letterSpacing: '0.06em', color: '#111827' }}>QUEUEBOT</div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--accent)', marginTop: 3, letterSpacing: '0.10em', fontWeight: 600 }}>ADMIN PANEL</div>
          </div>
          {/* Close button — only useful on mobile, hidden on desktop via the overlay click */}
          <button
            onClick={onClose}
            aria-label="Close menu"
            style={{
              background: 'none', border: 'none', color: '#9ca3af',
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
          {navItems.map(({ to, label, icon }) => (
            <NavLink key={to} to={to} end={to === '/'} onClick={onClose}
              style={({ isActive }) => ({
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '11px 16px 11px 22px', fontSize: 13, fontWeight: 600, letterSpacing: '0.03em',
                color: isActive ? 'var(--accent)' : '#6b7280',
                textDecoration: 'none',
                borderLeft: `2px solid ${isActive ? 'var(--accent)' : 'transparent'}`,
                background: isActive ? 'rgba(99,102,241,0.07)' : 'transparent',
                transition: 'all 0.15s',
                borderRadius: '0 8px 8px 0',
                marginRight: 12,
              })}>
              {icon}{label}
            </NavLink>
          ))}
        </nav>

        <div style={{ padding: '16px 24px', borderTop: '1px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: '#9ca3af', letterSpacing: '0.06em' }}>MVP v0.2</div>
          {onLogout && (
            <button
              onClick={onLogout}
              style={{ background: 'none', border: '1px solid var(--border)', color: '#6b7280', fontSize: 11, fontWeight: 600, padding: '4px 10px', borderRadius: 6, cursor: 'pointer', letterSpacing: '0.04em' }}
            >
              Sign out
            </button>
          )}
        </div>
      </aside>
    </>
  )
}
