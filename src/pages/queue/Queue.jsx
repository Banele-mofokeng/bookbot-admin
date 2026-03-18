import { useState, useEffect, useCallback } from 'react'
import { api } from '../../api/client.js'
import { Badge, Button, Card, StatCard, Empty, Loading, useToast, Toast, Select, Input } from '../../components/UI.jsx'

const STATUS_COLOR = {
  Waiting:   'blue',
  InService: 'amber',
  Done:      'green',
  NoShow:    'gray',
  Cancelled: 'red',
}

const STATUS_NEXT = {
  Waiting:   'InService',
  InService: 'Done',
}

const STATUS_LABEL = {
  Waiting:   'Mark In Service',
  InService: 'Mark Done',
}

function WalkinModal({ tenant, services, agents, onClose, onAdd, show }) {
  const [name, setName]           = useState('')
  const [serviceId, setServiceId] = useState('')
  const [agentId, setAgentId]     = useState('')
  const [saving, setSaving]       = useState(false)
  const { toast, show: showToast } = useToast()

  if (!show) return null

  async function submit() {
    if (!name.trim() || !serviceId) { showToast('Name and service are required.', 'error'); return }
    setSaving(true)
    try {
      const entry = await api.addWalkin({
        tenant_id:     tenant.id,
        service_id:    parseInt(serviceId),
        agent_id:      agentId ? parseInt(agentId) : null,
        customer_name: name.trim(),
        queue_date:    new Date().toISOString().split('T')[0],
      })
      onAdd(entry)
      onClose()
    } catch (e) {
      showToast(e.message || 'Failed to add walk-in.', 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200,
    }}>
      <Card style={{ width: 420, padding: 28 }}>
        <div style={{ fontSize: 16, fontWeight: 800, marginBottom: 20 }}>Add Walk-in</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <Input label="Customer Name" value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Thabo or Walk-in" />
          <Select label="Service" value={serviceId} onChange={e => setServiceId(e.target.value)}>
            <option value="">Select service...</option>
            {services.filter(s => s.is_active).map(s => (
              <option key={s.id} value={s.id}>{s.name} ({s.duration_minutes} min)</option>
            ))}
          </Select>
          <Select label={`Preferred ${tenant?.agent_label || 'Agent'} (optional)`} value={agentId} onChange={e => setAgentId(e.target.value)}>
            <option value="">No preference</option>
            {agents.filter(a => a.is_active).map(a => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </Select>
        </div>
        <div style={{ display: 'flex', gap: 10, marginTop: 20 }}>
          <Button onClick={submit} loading={saving}>Add to Queue</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </div>
        <Toast toast={toast} />
      </Card>
    </div>
  )
}

export default function Queue({ tenants }) {
  const [selectedTenant, setSelectedTenant] = useState(null)
  const [queue, setQueue]                   = useState([])
  const [services, setServices]             = useState([])
  const [agents, setAgents]                 = useState([])
  const [loading, setLoading]               = useState(false)
  const [updating, setUpdating]             = useState(null)
  const [showWalkin, setShowWalkin]         = useState(false)
  const { toast, show }                     = useToast()

  useEffect(() => { if (tenants.length && !selectedTenant) setSelectedTenant(tenants[0]) }, [tenants])

  useEffect(() => {
    if (!selectedTenant) return
    api.getServices(selectedTenant.id).then(setServices).catch(() => {})
    api.getAgents(selectedTenant.id).then(setAgents).catch(() => {})
  }, [selectedTenant])

  const loadQueue = useCallback(async () => {
    if (!selectedTenant) return
    setLoading(true)
    try {
      setQueue(await api.getQueue(selectedTenant.id))
    } catch {
      show('Failed to load queue.', 'error')
    } finally {
      setLoading(false)
    }
  }, [selectedTenant])

  useEffect(() => { loadQueue() }, [loadQueue])

  // Auto-refresh every 30s
  useEffect(() => {
    const interval = setInterval(loadQueue, 30000)
    return () => clearInterval(interval)
  }, [loadQueue])

  async function updateStatus(entryId, status) {
    setUpdating(entryId)
    try {
      await api.updateStatus(entryId, status)
      show(`Marked as ${status}.`, 'success')
      loadQueue()
    } catch {
      show('Failed to update status.', 'error')
    } finally {
      setUpdating(null)
    }
  }

  const waiting   = queue.filter(e => e.status === 'Waiting')
  const inService = queue.filter(e => e.status === 'InService')
  const done      = queue.filter(e => ['Done','NoShow','Cancelled'].includes(e.status))

  return (
    <div>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.01em' }}>Live Queue</h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
            {new Date().toLocaleDateString('en-ZA', { weekday: 'long', day: 'numeric', month: 'long' })}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <select
            value={selectedTenant?.id || ''}
            onChange={e => setSelectedTenant(tenants.find(t => t.id === parseInt(e.target.value)))}
            style={{ background: 'var(--surface2)', border: '1px solid var(--border2)', borderRadius: 8, padding: '8px 14px', fontSize: 13, color: 'var(--text)', fontFamily: 'var(--sans)', fontWeight: 600, outline: 'none', cursor: 'pointer' }}
          >
            {tenants.map(t => <option key={t.id} value={t.id}>{t.business_name}</option>)}
          </select>
          <Button variant="outline" onClick={() => setShowWalkin(true)}>+ Walk-in</Button>
          <Button variant="ghost" size="sm" onClick={loadQueue} loading={loading}>↻ Refresh</Button>
        </div>
      </div>

      {/* Stats */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 16, marginBottom: 28 }}>
        <StatCard label="Waiting"    value={waiting.length}   color="var(--blue)"  sub="in queue now"    delay={0} />
        <StatCard label="In Service" value={inService.length} color="var(--amber)" sub="being served"    delay={60} />
        <StatCard label="Served Today" value={done.filter(e=>e.status==='Done').length} color="var(--accent)" sub="completed" delay={120} />
      </div>

      {/* Queue Table */}
      <Card className="animate-fade-up" style={{ overflow: 'hidden', animationDelay: '180ms' }}>
        {loading && queue.length === 0 ? (
          <Loading message="Loading queue..." />
        ) : queue.length === 0 ? (
          <Empty message="Queue is empty. Add a walk-in or wait for WhatsApp bookings." />
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: 'var(--surface2)', borderBottom: '1px solid var(--border)' }}>
                {['#', 'Customer', 'Service', selectedTenant?.agent_label || 'Agent', 'ETA', 'Via', 'Status', ''].map(h => (
                  <th key={h} style={{ padding: '11px 16px', textAlign: 'left', fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)', fontWeight: 500 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {queue.map((entry, i) => (
                <tr key={entry.id}
                  style={{ borderBottom: i < queue.length - 1 ? '1px solid var(--border)' : 'none', transition: 'background 0.12s', opacity: ['Done','NoShow','Cancelled'].includes(entry.status) ? 0.45 : 1 }}
                  onMouseEnter={e => e.currentTarget.style.background = 'var(--surface2)'}
                  onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
                >
                  <td style={{ padding: '14px 16px', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--muted)' }}>#{entry.position}</td>
                  <td style={{ padding: '14px 16px' }}>
                    <div style={{ fontWeight: 700, fontSize: 13 }}>{entry.customer_name}</div>
                    {entry.customer_number !== 'walkin' && (
                      <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>{entry.customer_number}</div>
                    )}
                  </td>
                  <td style={{ padding: '14px 16px', fontSize: 13 }}>{entry.service}</td>
                  <td style={{ padding: '14px 16px', fontSize: 13 }}>{entry.agent}</td>
                  <td style={{ padding: '14px 16px', fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 600 }}>{entry.estimated_start}</td>
                  <td style={{ padding: '14px 16px' }}>
                    <Badge color={entry.booked_via === 'walkin' ? 'amber' : 'blue'}>
                      {entry.booked_via === 'walkin' ? 'Walk-in' : 'WhatsApp'}
                    </Badge>
                  </td>
                  <td style={{ padding: '14px 16px' }}>
                    <Badge color={STATUS_COLOR[entry.status] || 'gray'}>{entry.status}</Badge>
                  </td>
                  <td style={{ padding: '14px 16px' }}>
                    <div style={{ display: 'flex', gap: 6 }}>
                      {STATUS_NEXT[entry.status] && (
                        <Button
                          variant={entry.status === 'Waiting' ? 'outline' : 'primary'}
                          size="sm"
                          onClick={() => updateStatus(entry.id, STATUS_NEXT[entry.status])}
                          loading={updating === entry.id}
                        >
                          {STATUS_LABEL[entry.status]}
                        </Button>
                      )}
                      {entry.status === 'Waiting' && (
                        <Button variant="danger" size="sm"
                          onClick={() => updateStatus(entry.id, 'NoShow')}
                          loading={updating === entry.id}
                        >No Show</Button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <WalkinModal
        show={showWalkin}
        tenant={selectedTenant}
        services={services}
        agents={agents}
        onClose={() => setShowWalkin(false)}
        onAdd={() => { setShowWalkin(false); loadQueue() }}
      />
      <Toast toast={toast} />
    </div>
  )
}
