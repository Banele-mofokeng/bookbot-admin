import { useState, useEffect } from 'react'

// ── Badge ──────────────────────────────────────────────────────────────────
export function Badge({ children, color = 'green' }) {
  const colors = {
    green: { bg: 'var(--accent-dim)', text: 'var(--accent)' },
    red:   { bg: 'var(--red-dim)',    text: 'var(--red)' },
    amber: { bg: 'var(--amber-dim)',  text: 'var(--amber)' },
    blue:  { bg: 'var(--blue-dim)',   text: 'var(--blue)' },
    gray:  { bg: 'var(--surface3)',   text: 'var(--muted2)' },
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
export function Button({ children, onClick, variant = 'primary', size = 'md', disabled, loading, style }) {
  const base = {
    display: 'inline-flex', alignItems: 'center', gap: 6,
    border: 'none', borderRadius: 8, fontFamily: 'var(--sans)',
    fontWeight: 700, letterSpacing: '0.03em', cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled || loading ? 0.5 : 1,
    transition: 'opacity 0.15s, background 0.15s',
    ...style,
  }
  const sizes = { sm: { padding: '6px 12px', fontSize: 11 }, md: { padding: '9px 18px', fontSize: 13 }, lg: { padding: '12px 24px', fontSize: 14 } }
  const variants = {
    primary: { background: 'var(--accent)', color: '#0b0b0e' },
    danger:  { background: 'var(--red-dim)', color: 'var(--red)', border: '1px solid var(--red)' },
    ghost:   { background: 'var(--surface2)', color: 'var(--muted2)', border: '1px solid var(--border2)' },
    outline: { background: 'transparent', color: 'var(--accent)', border: '1px solid var(--accent)' },
  }
  return (
    <button style={{ ...base, ...sizes[size], ...variants[variant] }} onClick={onClick} disabled={disabled || loading}>
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
          background: 'var(--surface2)', border: `1px solid ${error ? 'var(--red)' : 'var(--border2)'}`,
          borderRadius: 8, padding: '10px 14px', fontSize: 13,
          color: 'var(--text)', outline: 'none', width: '100%',
          transition: 'border-color 0.15s',
        }}
        onFocus={e => { if (!error) e.target.style.borderColor = 'var(--accent)' }}
        onBlur={e => { if (!error) e.target.style.borderColor = 'var(--border2)' }}
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
          background: 'var(--surface2)', border: '1px solid var(--border2)',
          borderRadius: 8, padding: '10px 14px', fontSize: 13,
          color: 'var(--text)', outline: 'none', width: '100%', cursor: 'pointer',
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
      borderRadius: 'var(--radius-lg)', ...style,
    }}>
      {children}
    </div>
  )
}

// ── Stat Card ──────────────────────────────────────────────────────────────
export function StatCard({ label, value, color = 'var(--text)', sub, delay = 0 }) {
  return (
    <Card className="animate-fade-up" style={{ padding: '20px 24px', animationDelay: `${delay}ms` }}>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)', marginBottom: 10 }}>{label}</div>
      <div style={{ fontSize: 34, fontWeight: 800, letterSpacing: '-0.02em', lineHeight: 1, color }}>{value}</div>
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
      background: 'var(--surface2)', border: `1px solid ${c.border}`,
      borderRadius: 10, padding: '12px 18px', color: c.color,
      fontSize: 13, fontWeight: 600, maxWidth: 320,
    }}>
      {toast.msg}
    </div>
  )
}

// ── Empty State ────────────────────────────────────────────────────────────
export function Empty({ message = 'No data found.' }) {
  return (
    <div style={{ padding: '56px 24px', textAlign: 'center', color: 'var(--muted)', fontSize: 13 }}>
      {message}
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
