import { useState, useEffect } from 'react'

// ── Badge ──────────────────────────────────────────────────────────────────
export function Badge({ children, color = 'green' }) {
  const colors = {
    green:  { bg: 'var(--green-dim)',  text: 'var(--green)' },
    red:    { bg: 'var(--red-dim)',    text: 'var(--red)' },
    amber:  { bg: 'var(--amber-dim)',  text: 'var(--amber)' },
    blue:   { bg: 'var(--blue-dim)',   text: 'var(--blue)' },
    indigo: { bg: 'var(--accent-dim)', text: 'var(--accent)' },
    gray:   { bg: 'var(--surface3)',   text: 'var(--muted2)' },
  }
  const c = colors[color] || colors.gray
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      padding: '3px 10px', borderRadius: 20,
      background: c.bg, color: c.text,
      fontSize: 11, fontWeight: 700, letterSpacing: '0.03em',
      whiteSpace: 'nowrap',
    }}>
      <span style={{ width: 5, height: 5, borderRadius: '50%', background: 'currentColor', flexShrink: 0 }} />
      {children}
    </span>
  )
}

// ── Button ─────────────────────────────────────────────────────────────────
export function Button({ children, onClick, variant = 'primary', size = 'md', disabled, loading, style, title }) {
  const [hovered, setHovered] = useState(false)
  const isDisabled = disabled || loading

  const base = {
    display: 'inline-flex', alignItems: 'center', gap: 6,
    border: 'none', borderRadius: 8, fontFamily: 'var(--sans)',
    fontWeight: 700, letterSpacing: '0.03em',
    cursor: isDisabled ? 'not-allowed' : 'pointer',
    opacity: isDisabled ? 0.5 : 1,
    transition: 'filter 0.12s, transform 0.12s, box-shadow 0.12s',
    filter: hovered && !isDisabled ? (variant === 'primary' ? 'brightness(1.10)' : 'brightness(0.95)') : 'none',
    transform: hovered && !isDisabled ? 'translateY(-1px)' : 'none',
    ...style,
  }
  const sizes = {
    sm: { padding: '6px 12px', fontSize: 11 },
    md: { padding: '9px 18px', fontSize: 13 },
    lg: { padding: '12px 24px', fontSize: 14 },
  }
  const variants = {
    primary: { background: 'var(--accent)', color: '#fff', boxShadow: hovered ? '0 4px 20px rgba(99,102,241,0.40)' : '0 2px 14px rgba(99,102,241,0.28)' },
    danger:  { background: 'var(--red-dim)', color: 'var(--red)', border: '1px solid rgba(220,38,38,0.30)' },
    ghost:   { background: hovered ? '#e9eaec' : '#f3f4f6', color: '#6b7280', border: '1px solid #e5e7eb' },
    outline: { background: hovered ? 'rgba(99,102,241,0.16)' : 'var(--accent-dim)', color: 'var(--accent)', border: '1px solid rgba(99,102,241,0.35)' },
  }
  return (
    <button
      title={title}
      style={{ ...base, ...sizes[size], ...variants[variant] }}
      onClick={onClick}
      disabled={isDisabled}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {loading ? '...' : children}
    </button>
  )
}

// ── Input ──────────────────────────────────────────────────────────────────
export function Input({ label, id, error, ...props }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {label && <label htmlFor={id} style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted2)' }}>{label}</label>}
      <input
        id={id}
        style={{
          background: '#ffffff', border: `1px solid ${error ? 'var(--red)' : '#d1d5db'}`,
          borderRadius: 8, padding: '10px 14px', fontSize: 13,
          color: 'var(--text)', outline: 'none', width: '100%',
          transition: 'border-color 0.15s', boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
        }}
        onFocus={e => { if (!error) e.target.style.borderColor = 'var(--accent)' }}
        onBlur={e => { if (!error) e.target.style.borderColor = '#d1d5db' }}
        {...props}
      />
      {error && <span style={{ fontSize: 11, color: 'var(--red)' }}>{error}</span>}
    </div>
  )
}

// ── Select ─────────────────────────────────────────────────────────────────
export function Select({ label, id, children, ...props }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {label && <label htmlFor={id} style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted2)' }}>{label}</label>}
      <select
        id={id}
        style={{
          background: '#ffffff', border: '1px solid #d1d5db',
          borderRadius: 8, padding: '10px 14px', fontSize: 13,
          color: 'var(--text)', outline: 'none', width: '100%', cursor: 'pointer',
          boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
        }}
        {...props}
      >
        {children}
      </select>
    </div>
  )
}

// ── Card ───────────────────────────────────────────────────────────────────
export function Card({ children, style, className }) {
  return (
    <div className={className} style={{
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius-lg)', boxShadow: '0 1px 4px rgba(0,0,0,0.06)',
      ...style,
    }}>
      {children}
    </div>
  )
}

// ── Stat Card ──────────────────────────────────────────────────────────────
export function StatCard({ label, value, color = 'var(--text)', sub, delay = 0 }) {
  return (
    <Card className="animate-fade-up" style={{ padding: '20px 24px', animationDelay: `${delay}ms`, borderTop: `2px solid ${color}`, borderTopLeftRadius: 'var(--radius-lg)', borderTopRightRadius: 'var(--radius-lg)' }}>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)', marginBottom: 10 }}>{label}</div>
      <div style={{ fontSize: 46, fontWeight: 800, letterSpacing: '-0.03em', lineHeight: 1, color }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 6 }}>{sub}</div>}
    </Card>
  )
}

// ── Toast ──────────────────────────────────────────────────────────────────
export function useToast() {
  const [toast, setToast] = useState(null)
  const show = (msg, type = 'success') => {
    setToast({ msg, type })
    setTimeout(() => setToast(null), 3500)
  }
  return { toast, show }
}

export function Toast({ toast }) {
  if (!toast) return null
  const colors = {
    success: { border: 'var(--accent)', color: 'var(--accent)' },
    error:   { border: 'var(--red)',    color: 'var(--red)' },
  }
  const c = colors[toast.type] || colors.success
  return (
    <div className="animate-fade-up" style={{
      position: 'fixed', bottom: 28, right: 28, zIndex: 999,
      background: '#ffffff', border: `1px solid ${c.border}`,
      borderRadius: 10, padding: '12px 18px', color: c.color,
      fontSize: 13, fontWeight: 600, maxWidth: 320,
      boxShadow: '0 8px 24px rgba(0,0,0,0.12)',
    }}>
      {toast.msg}
    </div>
  )
}

// ── Empty State ────────────────────────────────────────────────────────────
export function Empty({ message = 'Nothing here yet.', hint }) {
  return (
    <div style={{ padding: '52px 24px', textAlign: 'center' }}>
      <div style={{ width: 40, height: 40, borderRadius: 12, background: 'var(--surface3)', display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 14px' }}>
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
          <rect x="2" y="4" width="14" height="11" rx="2" stroke="var(--muted)" strokeWidth="1.4"/>
          <path d="M6 4V3a1 1 0 011-1h4a1 1 0 011 1v1" stroke="var(--muted)" strokeWidth="1.4"/>
          <path d="M6 9h6M6 12h4" stroke="var(--muted)" strokeWidth="1.3" strokeLinecap="round"/>
        </svg>
      </div>
      <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--muted2)', marginBottom: hint ? 6 : 0 }}>{message}</div>
      {hint && <div style={{ fontSize: 12, color: 'var(--muted)' }}>{hint}</div>}
    </div>
  )
}

// ── Loading ────────────────────────────────────────────────────────────────
export function Loading({ message = 'Loading...' }) {
  return (
    <div className="pulse" style={{ padding: '56px 24px', textAlign: 'center', color: 'var(--muted)', fontFamily: 'var(--mono)', fontSize: 12, letterSpacing: '0.08em' }}>
      {message}
    </div>
  )
}
