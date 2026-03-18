import { useState, useEffect } from 'react'
import { Routes, Route } from 'react-router-dom'
import Sidebar from './components/Sidebar.jsx'
import Queue from './pages/queue/Queue.jsx'
import Services from './pages/services/Services.jsx'
import Agents from './pages/agents/Agents.jsx'
import Tenants from './pages/Tenants.jsx'
import TenantForm from './pages/TenantForm.jsx'
import { api } from './api/client.js'

export default function App() {
  const [tenants, setTenants] = useState([])
  const [loading, setLoading] = useState(true)

  async function loadTenants() {
    try { setTenants(await api.getTenants()) }
    catch (e) { console.error('Failed to load tenants', e) }
    finally { setLoading(false) }
  }

  useEffect(() => { loadTenants() }, [])

  return (
    <div style={{ display: 'flex', minHeight: '100vh' }}>
      <Sidebar />
      <main style={{ marginLeft: 220, flex: 1, padding: '32px 36px', minHeight: '100vh' }}>
        {loading ? (
          <div className="pulse" style={{ color: 'var(--muted)', fontFamily: 'var(--mono)', fontSize: 12, letterSpacing: '0.08em', marginTop: 60, textAlign: 'center' }}>
            Connecting...
          </div>
        ) : (
          <Routes>
            <Route path="/"                  element={<Queue    tenants={tenants} />} />
            <Route path="/services"          element={<Services tenants={tenants} />} />
            <Route path="/agents"            element={<Agents   tenants={tenants} />} />
            <Route path="/businesses"        element={<Tenants  tenants={tenants} reload={loadTenants} />} />
            <Route path="/businesses/new"    element={<TenantForm tenants={tenants} reload={loadTenants} />} />
            <Route path="/businesses/:id/edit" element={<TenantForm tenants={tenants} reload={loadTenants} />} />
          </Routes>
        )}
      </main>
    </div>
  )
}
