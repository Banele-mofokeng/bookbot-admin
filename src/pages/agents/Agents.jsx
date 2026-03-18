import { useState, useEffect } from 'react'
import { api } from '../../api/client.js'
import { Badge, Button, Card, Empty, Loading, Input, useToast, Toast } from '../../components/UI.jsx'

function AgentForm({ tenantId, agent, services, onSave, onCancel }) {
  const [name, setName]               = useState(agent?.name || '')
  const [selectedSvcs, setSelectedSvcs] = useState(agent?.service_ids || [])
  const [saving, setSaving]           = useState(false)
  const { toast, show }               = useToast()

  function toggleService(id) {
    setSelectedSvcs(s => s.includes(id) ? s.filter(x => x !== id) : [...s, id])
  }

  async function submit() {
    if (!name.trim())           { show('Name is required.', 'error'); return }
    if (!selectedSvcs.length)   { show('Select at least one service.', 'error'); return }
    setSaving(true)
    try {
      const payload = { tenant_id: tenantId, name: name.trim(), service_ids: selectedSvcs, is_active: true }
      const result = agent
        ? await api.updateAgent(agent.id, { name: name.trim(), service_ids: selectedSvcs })
        : await api.createAgent(payload)
      onSave(result)
    } catch (e) {
      show(e.message || 'Failed to save.', 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card style={{ padding: 20, marginBottom: 12 }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <Input label="Name" value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Nomsa" />
        <div>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted2)', marginBottom: 10 }}>
            Services this agent can do
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {services.filter(s => s.is_active).map(svc => (
              <button key={svc.id} onClick={() => toggleService(svc.id)}
                style={{
                  padding: '6px 14px', borderRadius: 20, fontSize: 12, fontWeight: 700, cursor: 'pointer',
                  fontFamily: 'var(--sans)', transition: 'all 0.15s',
                  background: selectedSvcs.includes(svc.id) ? 'var(--accent)' : 'var(--surface2)',
                  color: selectedSvcs.includes(svc.id) ? '#0b0b0e' : 'var(--muted2)',
                  border: `1px solid ${selectedSvcs.includes(svc.id) ? 'var(--accent)' : 'var(--border2)'}`,
                }}>
                {svc.name}
              </button>
            ))}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <Button onClick={submit} loading={saving}>Save</Button>
          <Button variant="ghost" onClick={onCancel}>Cancel</Button>
        </div>
      </div>
      <Toast toast={toast} />
    </Card>
  )
}

export default function Agents({ tenants }) {
  const [selectedTenantId, setSelectedTenantId] = useState(tenants[0]?.id || null)
  const [agents, setAgents]     = useState([])
  const [services, setServices] = useState([])
  const [loading, setLoading]   = useState(false)
  const [adding, setAdding]     = useState(false)
  const [editing, setEditing]   = useState(null)
  const { toast, show }         = useToast()

  async function load(id) {
    setLoading(true)
    try {
      const [a, s] = await Promise.all([api.getAgents(id), api.getServices(id)])
      setAgents(a)
      setServices(s)
    } catch { show('Failed to load.', 'error') }
    finally { setLoading(false) }
  }

  useEffect(() => { if (selectedTenantId) load(selectedTenantId) }, [selectedTenantId])
  useEffect(() => { if (tenants.length && !selectedTenantId) setSelectedTenantId(tenants[0].id) }, [tenants])

  async function toggleActive(agent) {
    try {
      const updated = await api.updateAgent(agent.id, { is_active: !agent.is_active })
      setAgents(a => a.map(x => x.id === agent.id ? { ...x, ...updated } : x))
      show(`${agent.name} ${agent.is_active ? 'deactivated' : 'activated'}.`, 'success')
    } catch { show('Failed to update.', 'error') }
  }

  const tenant = tenants.find(t => t.id === selectedTenantId)

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800 }}>{tenant?.agent_label || 'Agent'}s</h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
            {tenant ? `${tenant.agent_label}s at ${tenant.business_name}` : ''}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <select value={selectedTenantId || ''} onChange={e => setSelectedTenantId(parseInt(e.target.value))}
            style={{ background: 'var(--surface2)', border: '1px solid var(--border2)', borderRadius: 8, padding: '8px 14px', fontSize: 13, color: 'var(--text)', fontFamily: 'var(--sans)', fontWeight: 600, outline: 'none', cursor: 'pointer' }}>
            {tenants.map(t => <option key={t.id} value={t.id}>{t.business_name}</option>)}
          </select>
          <Button onClick={() => { setAdding(true); setEditing(null) }}
            disabled={services.filter(s => s.is_active).length === 0}>
            + Add {tenant?.agent_label || 'Agent'}
          </Button>
        </div>
      </div>

      {services.filter(s => s.is_active).length === 0 && !loading && (
        <Card style={{ padding: '16px 20px', marginBottom: 16, borderColor: 'var(--amber)', background: 'var(--amber-dim)' }}>
          <span style={{ fontSize: 13, color: 'var(--amber)' }}>
            ⚠️ Add services first before creating {tenant?.agent_label?.toLowerCase() || 'agent'}s.
          </span>
        </Card>
      )}

      {adding && (
        <AgentForm tenantId={selectedTenantId} services={services}
          onSave={agent => { setAgents(a => [...a, agent]); setAdding(false); show('Agent added.', 'success') }}
          onCancel={() => setAdding(false)} />
      )}

      {loading ? <Loading message="Loading agents..." /> : agents.length === 0 && !adding ? (
        <Card><Empty message={`No ${tenant?.agent_label?.toLowerCase() || 'agent'}s yet.`} /></Card>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {agents.map((agent, i) => (
            editing === agent.id ? (
              <AgentForm key={agent.id} tenantId={selectedTenantId} agent={agent} services={services}
                onSave={updated => { setAgents(a => a.map(x => x.id === agent.id ? { ...x, ...updated } : x)); setEditing(null); show('Updated.', 'success') }}
                onCancel={() => setEditing(null)} />
            ) : (
              <Card key={agent.id} className="animate-fade-up" style={{ padding: '16px 20px', animationDelay: `${i * 40}ms` }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
                    <div style={{ width: 40, height: 40, background: 'var(--surface3)', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 16, fontWeight: 800, color: 'var(--text)' }}>
                      {agent.name.charAt(0).toUpperCase()}
                    </div>
                    <div>
                      <div style={{ fontWeight: 700, fontSize: 14 }}>{agent.name}</div>
                      <div style={{ display: 'flex', gap: 6, marginTop: 5, flexWrap: 'wrap' }}>
                        {(agent.service_ids || []).map(sid => {
                          const svc = services.find(s => s.id === sid)
                          return svc ? (
                            <span key={sid} style={{ padding: '2px 8px', borderRadius: 20, background: 'var(--accent-dim)', color: 'var(--accent)', fontSize: 11, fontWeight: 700 }}>
                              {svc.name}
                            </span>
                          ) : null
                        })}
                      </div>
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <Badge color={agent.is_active ? 'green' : 'gray'}>{agent.is_active ? 'Active' : 'Inactive'}</Badge>
                    <Button variant="ghost" size="sm" onClick={() => setEditing(agent.id)}>Edit</Button>
                    <Button variant={agent.is_active ? 'danger' : 'outline'} size="sm" onClick={() => toggleActive(agent)}>
                      {agent.is_active ? 'Deactivate' : 'Activate'}
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
