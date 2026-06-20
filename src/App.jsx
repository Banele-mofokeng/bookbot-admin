import { useState, useEffect } from 'react'
import { Routes, Route, useLocation } from 'react-router-dom'
import Sidebar from './components/Sidebar.jsx'
import Queue from './pages/queue/Queue.jsx'
import Services from './pages/services/Services.jsx'
import Agents from './pages/agents/Agents.jsx'
import Tenants from './pages/Tenants.jsx'
import TenantForm from './pages/TenantForm.jsx'
import { api, getToken, setToken, clearToken, setUnauthorizedHandler } from './api/client.js'

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
  const [value, setValue] = useState('')
  const [error, setError] = useState('')

  async function submit(e) {
    e.preventDefault()
    const token = value.trim()
    if (!token) return
    setToken(token)
    try {
      // Validate the token against a guarded endpoint before entering.
      await api.getTenants()
      onAuthed()
    } catch (err) {
      clearToken()
      setError('Invalid token. Check ADMIN_TOKEN on the server.')
    }
  }

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
      <form onSubmit={submit} style={{ width: '100%', maxWidth: 340, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <h1 style={{ fontSize: 18, fontWeight: 800, letterSpacing: '0.02em' }}>QueueBot Admin</h1>
        <p style={{ color: 'var(--muted)', fontSize: 13 }}>Enter your admin token to continue.</p>
        <input
          type="password"
          value={value}
          onChange={e => setValue(e.target.value)}
          placeholder="Admin token"
          autoFocus
          style={{ padding: '10px 12px', borderRadius: 8, border: '1px solid var(--border, #333)', background: 'transparent', color: 'inherit', fontSize: 14 }}
        />
        {error && <span style={{ color: '#ef4444', fontSize: 12 }}>{error}</span>}
        <button type="submit" style={{ padding: '10px 12px', borderRadius: 8, border: 'none', background: 'linear-gradient(135deg, #6366f1 0%, #818cf8 100%)', color: '#fff', fontWeight: 700, fontSize: 14, cursor: 'pointer' }}>
          Sign in
        </button>
      </form>
    </div>
  )
}

export default function App() {
  const [authed, setAuthed]           = useState(() => !!getToken())
  const [tenants, setTenants]         = useState([])
  const [loading, setLoading]         = useState(true)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const location = useLocation()

  // Drop back to the login screen whenever the API reports 401.
  useEffect(() => { setUnauthorizedHandler(() => setAuthed(false)) }, [])

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

  useEffect(() => { if (authed) loadTenants() }, [authed])

  const pageTitle = PAGE_TITLES[location.pathname] ?? 'QueueBot'

  function logout() { clearToken(); setAuthed(false) }

  if (!authed) return <Login onAuthed={() => setAuthed(true)} />

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
      <Sidebar isOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} onLogout={logout} />

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
            <Route path="/admin/businesses"          element={<Tenants    tenants={tenants} reload={loadTenants} />} />
            <Route path="/admin/businesses/new"      element={<TenantForm tenants={tenants} reload={loadTenants} />} />
            <Route path="/admin/businesses/:id/edit" element={<TenantForm tenants={tenants} reload={loadTenants} />} />
          </Routes>
        )}
      </main>
    </div>
  )
}
