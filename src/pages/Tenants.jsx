import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client.js'
import { Badge, Button, Card, Empty, useToast, Toast } from '../components/UI.jsx'

export default function Tenants({ tenants, reload }) {
  const navigate = useNavigate()
  const [toggling, setToggling] = useState(null)
  const [loginFor, setLoginFor] = useState(null)   // tenant we're creating a login for
  const [email, setEmail]       = useState('')
  const [pw, setPw]             = useState('')
  const [saving, setSaving]     = useState(false)
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

  function openLogin(tenant) {
    setLoginFor(tenant); setEmail(''); setPw('')
  }

  async function createLogin(e) {
    e.preventDefault()
    if (!email.trim() || pw.length < 8) {
      show('Email required and password must be 8+ characters.', 'error'); return
    }
    setSaving(true)
    try {
      await api.createUser({ email: email.trim(), password: pw, tenant_id: loginFor.id })
      show(`Login created for ${loginFor.business_name}. Share the credentials with them.`, 'success')
      setLoginFor(null)
    } catch (err) {
      show(String(err.message || err).includes('409') ? 'Email already registered.' : 'Failed to create login.', 'error')
    } finally {
      setSaving(false)
    }
  }

  const field = { padding: '10px 12px', borderRadius: 8, border: '1px solid #d1d5db', background: '#fff', color: '#111827', fontSize: 14, width: '100%' }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.01em' }}>Businesses</h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
            {tenants.length} registered {tenants.length === 1 ? 'business' : 'businesses'}
          </p>
        </div>
        <div className="page-header-actions">
          <Button onClick={() => navigate('/admin/businesses/new')}>+ Add Business</Button>
        </div>
      </div>

      {tenants.length === 0 ? (
        <Card>
          <Empty
            message="No businesses registered yet."
            hint="Use '+ Add Business' to register your first one."
          />
        </Card>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {tenants.map((t, i) => (
            <Card key={t.id} className="animate-fade-up" style={{ padding: '20px 24px', animationDelay: `${i * 50}ms` }}>
              <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16 }}>
                {/* Left */}
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
                    <span style={{ fontSize: 16, fontWeight: 800 }}>{t.business_name}</span>
                    <Badge color={t.is_active ? 'green' : 'red'}>{t.is_active ? 'Active' : 'Inactive'}</Badge>
                  </div>
                  <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap' }}>
                    {[
                      ['WhatsApp', t.whatsapp_number],
                      ['Type',    t.business_type],
                      ['Hours',   `${t.queue_opens}:00 – ${t.queue_closes}:00`],
                    ].map(([label, val]) => (
                      <div key={label}>
                        <div style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)' }}>{label}</div>
                        <div style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--muted2)', marginTop: 2 }}>{val}</div>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Actions */}
                <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
                  <Button
                    variant="outline" size="sm"
                    onClick={() => openLogin(t)}
                    title="Create a dashboard login for this business"
                  >
                    Add login
                  </Button>
                  <Button
                    variant="ghost" size="sm"
                    onClick={() => navigate(`/admin/businesses/${t.id}/edit`)}
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

      {loginFor && (
        <div
          onClick={() => !saving && setLoginFor(null)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24, zIndex: 50 }}
        >
          <form
            onClick={e => e.stopPropagation()}
            onSubmit={createLogin}
            style={{ width: '100%', maxWidth: 380, background: '#fff', borderRadius: 12, padding: 24, display: 'flex', flexDirection: 'column', gap: 12, boxShadow: '0 20px 60px rgba(0,0,0,0.25)' }}
          >
            <div>
              <h2 style={{ fontSize: 16, fontWeight: 800 }}>Create login</h2>
              <p style={{ fontSize: 12, color: 'var(--muted)', marginTop: 4 }}>
                For <strong>{loginFor.business_name}</strong>. They’ll sign in with these and only see their own queue.
              </p>
            </div>
            <input type="email" value={email} onChange={e => setEmail(e.target.value)}
              placeholder="Email" autoFocus autoComplete="off" style={field} />
            <input type="text" value={pw} onChange={e => setPw(e.target.value)}
              placeholder="Password (8+ chars)" autoComplete="off" style={field} />
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
              <Button type="button" variant="ghost" size="sm" onClick={() => setLoginFor(null)}>Cancel</Button>
              <Button type="submit" size="sm" loading={saving}>Create</Button>
            </div>
          </form>
        </div>
      )}

      <Toast toast={toast} />
    </div>
  )
}
