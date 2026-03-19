import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client.js'
import { Badge, Button, Card, Empty, useToast, Toast } from '../components/UI.jsx'

export default function Tenants({ tenants, reload }) {
  const navigate = useNavigate()
  const [toggling, setToggling] = useState(null)
  const { toast, show } = useToast()

  async function toggleActive(tenant) {
    setToggling(tenant.id)
    try {
      await api.updateTenant(tenant.id, { is_active: !tenant.is_active })
      show(`${tenant.business_name} ${tenant.is_active ? 'deactivated' : 'activated'}.`, 'success')
      reload()
    } catch {
      show('Failed to update tenant.', 'error')
    } finally {
      setToggling(null)
    }
  }

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.01em' }}>Businesses</h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
            {tenants.length} registered {tenants.length === 1 ? 'business' : 'businesses'}
          </p>
        </div>
        <Button onClick={() => navigate('/businesses/new')}>+ Add Business</Button>
      </div>

      {tenants.length === 0 ? (
        <Card>
          <Empty message="No businesses registered yet. Add your first one." />
        </Card>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {tenants.map((t, i) => (
            <Card key={t.id} className="animate-fade-up" style={{ padding: '20px 24px', animationDelay: `${i * 50}ms` }}>
              <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16 }}>
                {/* Left */}
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
                    <span style={{ fontSize: 16, fontWeight: 800 }}>{t.business_name}</span>
                    <Badge color={t.is_active ? 'green' : 'red'}>{t.is_active ? 'Active' : 'Inactive'}</Badge>
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: '6px 24px', marginTop: 10 }}>
                    {[
                      ['WhatsApp',      t.whatsapp_number],
                      ['Instance',      t.evolution_instance],
                      ['Agent Label',   t.agent_label],
                      ['Service Label', t.service_label],
                      ['Type',          t.business_type],
                      ['Hours',         `${t.queue_opens}:00 – ${t.queue_closes}:00`],
                      ['Advance Days',  `${t.advance_days} day(s)`],
                    ].map(([label, val]) => (
                      <div key={label}>
                        <span style={{ fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)' }}>{label}</span>
                        <div style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--muted2)', marginTop: 2 }}>{val}</div>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Actions */}
                <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
                  <Button
                    variant="ghost" size="sm"
                    onClick={() => navigate(`/businesses/${t.id}/edit`)}
                  >
                    Edit
                  </Button>
                  <Button
                    variant={t.is_active ? 'danger' : 'outline'} size="sm"
                    onClick={() => toggleActive(t)}
                    loading={toggling === t.id}
                  >
                    {t.is_active ? 'Deactivate' : 'Activate'}
                  </Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      <Toast toast={toast} />
    </div>
  )
}
