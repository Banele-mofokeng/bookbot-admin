import { useState, useEffect } from 'react'
import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import Sidebar from './components/Sidebar.jsx'
import Queue from './pages/queue/Queue.jsx'
import Services from './pages/services/Services.jsx'
import Agents from './pages/agents/Agents.jsx'
import Tenants from './pages/Tenants.jsx'
import TenantForm from './pages/TenantForm.jsx'
import { api, login as apiLogin, getMe, getToken, clearToken, setUnauthorizedHandler } from './api/client.js'

const PAGE_TITLES = {
  '/':                     'Queue',
  '/services':             'Services',
  '/agents':               'Agents',
  '/admin/businesses':     'Businesses',
}

function HamburgerIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
      <path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"/>
    </svg>
  )
}

function Login({ onAuthed }) {
  const [email, setEmail]   = useState('')
  const [pw, setPw]         = useState('')
  const [error, setError]   = useState('')
  const [busy, setBusy]     = useState(false)

  async function submit(e) {
    e.preventDefault()
    if (!email.trim() || !pw) return
    setBusy(true); setError('')
    try {
      const user = await apiLogin(email.trim(), pw)
      onAuthed(user)
    } catch (err) {
      setError('Invalid email or password.')
    } finally {
      setBusy(false)
    }
  }

  const field = { padding: '10px 12px', borderRadius: 8, border: '1px solid #d1d5db', background: '#fff', color: '#111827', fontSize: 14 }

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
      <form onSubmit={submit} style={{ width: '100%', maxWidth: 340, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <h1 style={{ fontSize: 18, fontWeight: 800, letterSpacing: '0.02em' }}>QueueBot Admin</h1>
        <p style={{ color: 'var(--muted)', fontSize: 13 }}>Sign in to your dashboard.</p>
        <input type="email" value={email} onChange={e => setEmail(e.target.value)}
          placeholder="Email" autoFocus autoComplete="username" style={field} />
        <input type="password" value={pw} onChange={e => setPw(e.target.value)}
          placeholder="Password" autoComplete="current-password" style={field} />
        {error && <span style={{ color: '#ef4444', fontSize: 12 }}>{error}</span>}
        <button type="submit" disabled={busy} style={{ padding: '10px 12px', borderRadius: 8, border: 'none', background: 'linear-gradient(135deg, #6366f1 0%, #818cf8 100%)', color: '#fff', fontWeight: 700, fontSize: 14, cursor: busy ? 'default' : 'pointer', opacity: busy ? 0.6 : 1 }}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}

export default function App() {
  const [user, setUser]               = useState(null)
  const [booting, setBooting]         = useState(() => !!getToken())  // restoring a session?
  const [tenants, setTenants]         = useState([])
  const [loading, setLoading]         = useState(true)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const location = useLocation()

  const isSuper = !!user?.is_super

  function logout() { clearToken(); setUser(null) }

  // Drop back to the login screen whenever the API reports 401.
  useEffect(() => { setUnauthorizedHandler(() => setUser(null)) }, [])

  // Restore an existing session on first load.
  useEffect(() => {
    if (!getToken()) return
    getMe().then(setUser).catch(() => clearToken()).finally(() => setBooting(false))
  }, [])

  // Close sidebar on route change (mobile nav tap)
  useEffect(() => { setSidebarOpen(false) }, [location.pathname])

  // Close sidebar on Escape key
  useEffect(() => {
    function onKey(e) { if (e.key === 'Escape') setSidebarOpen(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  async function loadTenants() {
    try { setTenants(await api.getTenants()) }
    catch (e) { console.error('Failed to load tenants', e) }
    finally { setLoading(false) }
  }

  useEffect(() => { if (user) loadTenants() }, [user])

  const pageTitle = PAGE_TITLES[location.pathname] ?? 'QueueBot'

  if (booting) {
    return (
      <div className="pulse" style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--muted)', fontFamily: 'var(--mono)', fontSize: 12, letterSpacing: '0.08em' }}>
        Loading…
      </div>
    )
  }

  if (!user) return <Login onAuthed={setUser} />

  return (
    <div className="app-layout">
      {/* ── Mobile top bar ──────────────────────────────────────── */}
      <header className="mobile-topbar">
        <div className="mobile-topbar-brand">
          <div style={{ width: 28, height: 28, background: 'linear-gradient(135deg, #6366f1 0%, #818cf8 100%)', borderRadius: 7, display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 0 12px rgba(99,102,241,0.30)' }}>
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
              <path d="M2 4h12M2 8h8M2 12h5" stroke="#fff" strokeWidth="1.8" strokeLinecap="round"/>
            </svg>
          </div>
          <span style={{ fontSize: 13, fontWeight: 800, letterSpacing: '0.04em' }}>{pageTitle}</span>
        </div>
        <button
          className="hamburger"
          onClick={() => setSidebarOpen(o => !o)}
          aria-label="Open menu"
          aria-expanded={sidebarOpen}
        >
          <HamburgerIcon />
        </button>
      </header>

      {/* ── Sidebar (desktop: always visible, mobile: drawer) ──── */}
      <Sidebar isOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} onLogout={logout} isSuper={isSuper} />

      {/* ── Main content ─────────────────────────────────────────── */}
      <main className="main-content">
        {loading ? (
          <div className="pulse" style={{ color: 'var(--muted)', fontFamily: 'var(--mono)', fontSize: 12, letterSpacing: '0.08em', marginTop: 60, textAlign: 'center' }}>
            Connecting...
          </div>
        ) : (
          <Routes>
            <Route path="/"                    element={<Queue      tenants={tenants} />} />
            <Route path="/services"            element={<Services   tenants={tenants} />} />
            <Route path="/agents"              element={<Agents     tenants={tenants} />} />
            {/* Business + user management is super-admin only */}
            <Route path="/admin/businesses"          element={isSuper ? <Tenants    tenants={tenants} reload={loadTenants} /> : <Navigate to="/" replace />} />
            <Route path="/admin/businesses/new"      element={isSuper ? <TenantForm tenants={tenants} reload={loadTenants} /> : <Navigate to="/" replace />} />
            <Route path="/admin/businesses/:id/edit" element={isSuper ? <TenantForm tenants={tenants} reload={loadTenants} /> : <Navigate to="/" replace />} />
          </Routes>
        )}
      </main>
    </div>
  )
}
