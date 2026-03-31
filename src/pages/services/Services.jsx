import { useState, useEffect } from 'react'
import { api } from '../../api/client.js'
import { Badge, Button, Card, Empty, Loading, Input, useToast, Toast } from '../../components/UI.jsx'

function ServiceForm({ tenantId, service, onSave, onCancel }) {
  const [form, setForm] = useState(
    service || { tenant_id: tenantId, name: '', duration_minutes: 60, is_active: true }
  )
  const [saving, setSaving] = useState(false)
  const { toast, show } = useToast()

  async function submit() {
    if (!form.name.trim()) { show('Name is required.', 'error'); return }
    setSaving(true)
    try {
      const result = service
        ? await api.updateService(service.id, { name: form.name, duration_minutes: form.duration_minutes, is_active: form.is_active })
        : await api.createService(form)
      onSave(result)
    } catch (e) {
      show(e.message || 'Failed to save.', 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card style={{ padding: 20, marginBottom: 12 }}>
      <div className="service-form-grid">
        <Input label="Service Name" value={form.name}
          onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
          placeholder="e.g. Box Braids" />
        <Input label="Duration (min)" type="number" value={form.duration_minutes}
          onChange={e => setForm(f => ({ ...f, duration_minutes: parseInt(e.target.value) || 30 }))}
          min={5} />
        <Button onClick={submit} loading={saving} style={{ marginBottom: 0 }}>Save</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
      <Toast toast={toast} />
    </Card>
  )
}

export default function Services({ tenants }) {
  const [selectedTenantId, setSelectedTenantId] = useState(tenants[0]?.id || null)
  const [services, setServices]                 = useState([])
  const [loading, setLoading]                   = useState(false)
  const [adding, setAdding]                     = useState(false)
  const [editing, setEditing]                   = useState(null)
  const { toast, show }                         = useToast()

  async function load(id) {
    setLoading(true)
    try { setServices(await api.getServices(id)) }
    catch { show('Failed to load services.', 'error') }
    finally { setLoading(false) }
  }

  useEffect(() => { if (selectedTenantId) load(selectedTenantId) }, [selectedTenantId])
  useEffect(() => { if (tenants.length && !selectedTenantId) setSelectedTenantId(tenants[0].id) }, [tenants])

  async function toggleActive(svc) {
    try {
      const updated = await api.updateService(svc.id, { is_active: !svc.is_active })
      setServices(s => s.map(x => x.id === svc.id ? updated : x))
      show(`${svc.name} ${svc.is_active ? 'deactivated' : 'activated'}.`, 'success')
    } catch { show('Failed to update.', 'error') }
  }

  function formatDuration(min) {
    if (min < 60) return `${min} min`
    const h = Math.floor(min / 60), m = min % 60
    return m === 0 ? `${h}hr` : `${h}hr ${m}min`
  }

  const tenant = tenants.find(t => t.id === selectedTenantId)

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800 }}>Services</h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
            {tenant ? `${tenant.service_label}s offered by ${tenant.business_name}` : ''}
          </p>
        </div>
        <div className="page-header-actions">
          {tenants.length > 1 && (
            <select value={selectedTenantId || ''} onChange={e => setSelectedTenantId(parseInt(e.target.value))}
              style={{ background: 'var(--surface2)', border: '1px solid var(--border2)', borderRadius: 8, padding: '8px 14px', fontSize: 13, color: 'var(--text)', fontFamily: 'var(--sans)', fontWeight: 600, outline: 'none', cursor: 'pointer' }}>
              {tenants.map(t => <option key={t.id} value={t.id}>{t.business_name}</option>)}
            </select>
          )}
          <Button onClick={() => { setAdding(true); setEditing(null) }}>+ Add Service</Button>
        </div>
      </div>

      {adding && (
        <ServiceForm tenantId={selectedTenantId}
          onSave={svc => { setServices(s => [...s, svc]); setAdding(false); show('Service added.', 'success') }}
          onCancel={() => setAdding(false)} />
      )}

      {loading ? <Loading message="Loading services..." /> : services.length === 0 && !adding ? (
        <Card><Empty message="No services yet. Add your first one." /></Card>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {services.map((svc, i) => (
            editing === svc.id ? (
              <ServiceForm key={svc.id} tenantId={selectedTenantId} service={svc}
                onSave={updated => { setServices(s => s.map(x => x.id === svc.id ? updated : x)); setEditing(null); show('Updated.', 'success') }}
                onCancel={() => setEditing(null)} />
            ) : (
              <Card key={svc.id} className="animate-fade-up" style={{ padding: '16px 20px', animationDelay: `${i * 40}ms` }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
                    <div style={{ width: 40, height: 40, background: 'var(--accent-dim)', borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--accent)', fontWeight: 700 }}>
                      {svc.duration_minutes}m
                    </div>
                    <div>
                      <div style={{ fontWeight: 700, fontSize: 14 }}>{svc.name}</div>
                      <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>{formatDuration(svc.duration_minutes)} per session</div>
                    </div>
                  </div>
                  <div className="card-row-actions">
                    <Badge color={svc.is_active ? 'green' : 'gray'}>{svc.is_active ? 'Active' : 'Inactive'}</Badge>
                    <Button variant="ghost" size="sm" onClick={() => setEditing(svc.id)}>Edit</Button>
                    <Button variant={svc.is_active ? 'danger' : 'outline'} size="sm" onClick={() => toggleActive(svc)}>
                      {svc.is_active ? 'Deactivate' : 'Activate'}
                    </Button>
                  </div>
                </div>
              </Card>
            )
          ))}
        </div>
      )}
      <Toast toast={toast} />
    </div>
  )
}
