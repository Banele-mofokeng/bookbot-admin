import { useState, useEffect, useCallback } from 'react'
import { api } from '../api/client.js'
import { Badge, Button, Card, StatCard, Empty, Loading, useToast, Toast } from '../components/UI.jsx'

function formatDate(iso) {
  const d = new Date(iso)
  return d.toLocaleDateString('en-ZA', { day: 'numeric', month: 'short', year: 'numeric' })
}

function formatTime(iso) {
  return new Date(iso).toLocaleTimeString('en-ZA', { hour: '2-digit', minute: '2-digit' })
}

function apptStatus(iso) {
  const d = new Date(iso)
  const now = new Date()
  const todayStr = now.toDateString()
  if (d.toDateString() === todayStr) return { label: 'Today', color: 'blue' }
  if (d < now) return { label: 'Past', color: 'amber' }
  return { label: 'Upcoming', color: 'green' }
}

export default function Appointments({ tenants }) {
  const [selectedTenant, setSelectedTenant] = useState(null)
  const [appointments, setAppointments] = useState([])
  const [loading, setLoading] = useState(false)
  const [cancelling, setCancelling] = useState(null)
  const { toast, show } = useToast()

  // Auto-select first tenant
  useEffect(() => {
    if (tenants.length && !selectedTenant) setSelectedTenant(tenants[0])
  }, [tenants])

  const load = useCallback(async (tenant) => {
    if (!tenant) return
    setLoading(true)
    try {
      const data = await api.getAppointments(tenant.id)
      setAppointments(data)
    } catch {
      show('Failed to load appointments.', 'error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(selectedTenant) }, [selectedTenant])

  async function cancel(id) {
    if (!confirm('Cancel this appointment?')) return
    setCancelling(id)
    try {
      await api.cancelAppointment(id)
      show('Appointment cancelled.', 'success')
      load(selectedTenant)
    } catch {
      show('Failed to cancel.', 'error')
    } finally {
      setCancelling(null)
    }
  }

  const now = new Date()
  const today = appointments.filter(a => new Date(a.appointment_date).toDateString() === now.toDateString())
  const upcoming = appointments.filter(a => {
    const d = new Date(a.appointment_date)
    return d >= now && d <= new Date(now.getTime() + 7 * 86400000)
  })

  return (
    <div>
      {/* Header row */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.01em' }}>Appointments</h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
            {selectedTenant ? selectedTenant.business_name : 'Select a business'}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          {/* Tenant switcher */}
          <select
            value={selectedTenant?.id || ''}
            onChange={e => setSelectedTenant(tenants.find(t => t.id === parseInt(e.target.value)))}
            style={{
              background: 'var(--surface2)', border: '1px solid var(--border2)',
              borderRadius: 8, padding: '8px 14px', fontSize: 13, color: 'var(--text)',
              fontFamily: 'var(--sans)', fontWeight: 600, outline: 'none', cursor: 'pointer',
            }}
          >
            {tenants.map(t => <option key={t.id} value={t.id}>{t.business_name}</option>)}
          </select>
          <Button variant="ghost" size="sm" onClick={() => load(selectedTenant)} loading={loading}>
            ↻ Refresh
          </Button>
        </div>
      </div>

      {/* Stats */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16, marginBottom: 28 }}>
        <StatCard label="Total Confirmed" value={appointments.length} color="var(--accent)" sub="all time" delay={0} />
        <StatCard label="Today" value={today.length} color="var(--amber)"
          sub={now.toLocaleDateString('en-ZA', { weekday: 'short', day: 'numeric', month: 'short' })} delay={60} />
        <StatCard label="Next 7 Days" value={upcoming.length} color="var(--blue)" sub="upcoming" delay={120} />
      </div>

      {/* Table */}
      <Card className="animate-fade-up" style={{ animationDelay: '180ms', overflow: 'hidden' }}>
        {loading ? (
          <Loading message="Loading appointments..." />
        ) : appointments.length === 0 ? (
          <Empty message="No confirmed appointments for this business." />
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: 'var(--surface2)', borderBottom: '1px solid var(--border)' }}>
                {['#', 'Customer', 'Number', 'Service', 'Date', 'Time', 'Status', ''].map(h => (
                  <th key={h} style={{
                    padding: '11px 16px', textAlign: 'left',
                    fontFamily: 'var(--mono)', fontSize: 10,
                    letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)',
                    fontWeight: 500,
                  }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {appointments.map((a, i) => {
                const status = apptStatus(a.appointment_date)
                return (
                  <tr key={a.id} style={{
                    borderBottom: i < appointments.length - 1 ? '1px solid var(--border)' : 'none',
                    transition: 'background 0.12s',
                  }}
                    onMouseEnter={e => e.currentTarget.style.background = 'var(--surface2)'}
                    onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
                  >
                    <td style={{ padding: '14px 16px', fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)' }}>{a.id}</td>
                    <td style={{ padding: '14px 16px', fontWeight: 700, fontSize: 13 }}>{a.customer_name}</td>
                    <td style={{ padding: '14px 16px', fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted2)' }}>
                      {a.customer_number.replace('@s.whatsapp.net', '')}
                    </td>
                    <td style={{ padding: '14px 16px', fontSize: 13 }}>{a.service_type}</td>
                    <td style={{ padding: '14px 16px', fontSize: 13, fontWeight: 600 }}>{formatDate(a.appointment_date)}</td>
                    <td style={{ padding: '14px 16px', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--muted2)' }}>{formatTime(a.appointment_date)}</td>
                    <td style={{ padding: '14px 16px' }}><Badge color={status.color}>{status.label}</Badge></td>
                    <td style={{ padding: '14px 16px' }}>
                      <Button
                        variant="danger" size="sm"
                        onClick={() => cancel(a.id)}
                        loading={cancelling === a.id}
                      >
                        Cancel
                      </Button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Card>

      <Toast toast={toast} />
    </div>
  )
}
