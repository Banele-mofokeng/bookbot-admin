import { useState, useEffect } from 'react'
import { Routes, Route } from 'react-router-dom'
import Sidebar from './components/Sidebar.jsx'
import Appointments from './pages/Appointments.jsx'
import Tenants from './pages/Tenants.jsx'
import TenantForm from './pages/TenantForm.jsx'
import { api } from './api/client.js'

export default function App() {
  const [tenants, setTenants] = useState([])
  const [loadingTenants, setLoadingTenants] = useState(true)

  async function loadTenants() {
    try {
      const data = await api.getTenants()
      setTenants(data)
    } catch (e) {
      console.error('Failed to load tenants', e)
    } finally {
      setLoadingTenants(false)
    }
  }

  useEffect(() => { loadTenants() }, [])

  return (
    <div style={{ display: 'flex', minHeight: '100vh' }}>
      <Sidebar />
      <main style={{ marginLeft: 220, flex: 1, padding: '32px 36px', minHeight: '100vh' }}>
        {loadingTenants ? (
          <div className="pulse" style={{ color: 'var(--muted)', fontFamily: 'var(--mono)', fontSize: 12, letterSpacing: '0.08em', marginTop: 60, textAlign: 'center' }}>
            Connecting to API...
          </div>
        ) : (
          <Routes>
            <Route path="/"                    element={<Appointments tenants={tenants} />} />
            <Route path="/tenants"             element={<Tenants tenants={tenants} reload={loadTenants} />} />
            <Route path="/tenants/new"         element={<TenantForm tenants={tenants} reload={loadTenants} />} />
            <Route path="/tenants/:id/edit"    element={<TenantForm tenants={tenants} reload={loadTenants} />} />
          </Routes>
        )}
      </main>
    </div>
  )
}
