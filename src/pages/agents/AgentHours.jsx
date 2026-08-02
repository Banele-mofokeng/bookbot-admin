import { useState, useEffect } from 'react'
import { api } from '../../api/client.js'
import { Badge, Button, Card, Loading, useToast, Toast } from '../../components/UI.jsx'

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

const toHHMM = (m) => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`
const toMinutes = (hhmm) => {
  const [h, m] = String(hhmm).split(':').map(Number)
  return (Number.isFinite(h) ? h : 0) * 60 + (Number.isFinite(m) ? m : 0)
}
const todayISO = () => new Date().toISOString().slice(0, 10)

const label = { fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted2)' }
const timeInput = {
  background: '#fff', border: '1px solid #d1d5db', borderRadius: 8,
  padding: '6px 10px', fontSize: 13, fontFamily: 'var(--sans)', outline: 'none',
}

/**
 * Working hours for one agent: a recurring weekly pattern, plus one-off blocks
 * that cut holes in it (lunch, leave, an appointment).
 *
 * A day with no rows means the agent does not work that day. That only applies
 * once at least one day is set — an agent with a completely empty schedule
 * falls back to the shop's opening hours, which is how every agent starts.
 */
export default function AgentHours({ agent, tenant, onClose }) {
  const [days, setDays]       = useState(() => DAYS.map(() => []))
  const [usesTenant, setUses] = useState(true)
  const [blocks, setBlocks]   = useState([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving]   = useState(false)
  const [newBlock, setNewBlock] = useState({ date: todayISO(), start: '13:00', end: '14:00', reason: '' })
  const { toast, show }       = useToast()

  async function load() {
    setLoading(true)
    try {
      const [sched, blks] = await Promise.all([
        api.getSchedule(agent.id),
        api.getBlocks(agent.id, todayISO()),
      ])
      const next = DAYS.map(() => [])
      sched.windows.forEach(w => next[w.weekday].push({ start: w.start, end: w.end }))
      setDays(next)
      setUses(sched.uses_tenant_hours)
      setBlocks(blks)
    } catch (e) {
      show(e.message || 'Failed to load hours.', 'error')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [agent.id])

  function addWindow(dayIdx) {
    setDays(d => d.map((w, i) => i === dayIdx ? [...w, { start: '08:00', end: '17:00' }] : w))
  }
  function removeWindow(dayIdx, wIdx) {
    setDays(d => d.map((w, i) => i === dayIdx ? w.filter((_, j) => j !== wIdx) : w))
  }
  function editWindow(dayIdx, wIdx, field, value) {
    setDays(d => d.map((w, i) => i !== dayIdx ? w
      : w.map((win, j) => j === wIdx ? { ...win, [field]: value } : win)))
  }

  function applyShopHours() {
    const start = toHHMM((tenant?.queue_opens ?? 8) * 60)
    const end   = toHHMM((tenant?.queue_closes ?? 17) * 60)
    setDays(DAYS.map((_, i) => i <= 5 ? [{ start, end }] : []))   // Mon–Sat
  }

  async function saveSchedule() {
    const windows = []
    for (let d = 0; d < DAYS.length; d++) {
      for (const w of days[d]) {
        const start = toMinutes(w.start), end = toMinutes(w.end)
        if (end <= start) { show(`${DAYS[d]}: end time must be after start.`, 'error'); return }
        windows.push({ weekday: d, start_minute: start, end_minute: end })
      }
    }
    setSaving(true)
    try {
      await api.setSchedule(agent.id, windows)
      show(windows.length ? 'Hours saved. Today\'s ETAs updated.' : 'Cleared — back to shop hours.', 'success')
      await load()
    } catch (e) {
      show(e.message || 'Failed to save.', 'error')
    } finally {
      setSaving(false)
    }
  }

  async function addBlock() {
    const { date, start, end, reason } = newBlock
    if (toMinutes(end) <= toMinutes(start)) { show('End time must be after start.', 'error'); return }
    try {
      await api.createBlock(agent.id, {
        starts_at: `${date}T${start}:00`,
        ends_at:   `${date}T${end}:00`,
        reason,
      })
      show('Time off added.', 'success')
      setNewBlock(b => ({ ...b, reason: '' }))
      await load()
    } catch (e) { show(e.message || 'Failed to add.', 'error') }
  }

  async function removeBlock(id) {
    try {
      await api.deleteBlock(id)
      show('Removed.', 'success')
      await load()
    } catch (e) { show(e.message || 'Failed to remove.', 'error') }
  }

  const agentLabel = tenant?.agent_label || 'Agent'

  return (
    <Card style={{ padding: 20, marginBottom: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <div>
          <div style={{ fontWeight: 800, fontSize: 15 }}>{agent.name} — working hours</div>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 3 }}>
            Bookings are only ever placed inside these hours.
          </div>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose}>Close</Button>
      </div>

      {loading ? <Loading message="Loading hours..." /> : (
        <>
          {usesTenant && (
            <Card style={{ padding: '12px 16px', marginBottom: 14, borderColor: 'var(--blue)', background: 'var(--blue-dim)' }}>
              <span style={{ fontSize: 12.5, color: 'var(--blue)' }}>
                No hours set — this {agentLabel.toLowerCase()} is treated as available
                whenever the shop is open ({String(tenant?.queue_opens ?? 8).padStart(2, '0')}:00–
                {String(tenant?.queue_closes ?? 17).padStart(2, '0')}:00, every day).
              </span>
            </Card>
          )}

          {/* ── weekly pattern ── */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {DAYS.map((day, d) => (
              <div key={day} style={{
                display: 'flex', alignItems: 'flex-start', gap: 12, padding: '10px 12px',
                borderRadius: 8, background: days[d].length ? 'transparent' : 'var(--surface3)',
              }}>
                <div style={{ width: 92, paddingTop: 6, fontSize: 13, fontWeight: 700, flexShrink: 0 }}>{day}</div>
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {days[d].length === 0 && (
                    <span style={{ fontSize: 12, color: 'var(--muted2)', paddingTop: 7 }}>
                      {usesTenant ? 'Shop hours' : 'Not working'}
                    </span>
                  )}
                  {days[d].map((w, wi) => (
                    <div key={wi} style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <input type="time" value={w.start} style={timeInput}
                        onChange={e => editWindow(d, wi, 'start', e.target.value)} />
                      <span style={{ color: 'var(--muted2)', fontSize: 12 }}>to</span>
                      <input type="time" value={w.end} style={timeInput}
                        onChange={e => editWindow(d, wi, 'end', e.target.value)} />
                      <Button variant="ghost" size="sm" onClick={() => removeWindow(d, wi)}>Remove</Button>
                    </div>
                  ))}
                </div>
                <Button variant="outline" size="sm" onClick={() => addWindow(d)}>
                  + {days[d].length ? 'Split shift' : 'Hours'}
                </Button>
              </div>
            ))}
          </div>

          <div style={{ display: 'flex', gap: 10, marginTop: 16, flexWrap: 'wrap' }}>
            <Button onClick={saveSchedule} loading={saving}>Save hours</Button>
            <Button variant="outline" onClick={applyShopHours}>Use shop hours, Mon–Sat</Button>
            <Button variant="ghost" onClick={() => setDays(DAYS.map(() => []))}>Clear all</Button>
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--muted2)', marginTop: 8 }}>
            Saving with every day empty puts this {agentLabel.toLowerCase()} back on shop hours.
          </div>

          {/* ── one-off time off ── */}
          <div style={{ marginTop: 26, paddingTop: 18, borderTop: '1px solid #e5e7eb' }}>
            <div style={{ ...label, marginBottom: 4 }}>Time off</div>
            <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 12 }}>
              One-off gaps — lunch, leave, an appointment. These override the weekly hours above.
            </div>

            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 14 }}>
              <input type="date" value={newBlock.date} style={timeInput}
                onChange={e => setNewBlock(b => ({ ...b, date: e.target.value }))} />
              <input type="time" value={newBlock.start} style={timeInput}
                onChange={e => setNewBlock(b => ({ ...b, start: e.target.value }))} />
              <span style={{ color: 'var(--muted2)', fontSize: 12 }}>to</span>
              <input type="time" value={newBlock.end} style={timeInput}
                onChange={e => setNewBlock(b => ({ ...b, end: e.target.value }))} />
              <input type="text" value={newBlock.reason} placeholder="Reason (optional)"
                style={{ ...timeInput, minWidth: 160, flex: 1 }}
                onChange={e => setNewBlock(b => ({ ...b, reason: e.target.value }))} />
              <Button size="sm" onClick={addBlock}>Add</Button>
            </div>

            {blocks.length === 0 ? (
              <div style={{ fontSize: 12.5, color: 'var(--muted2)' }}>No upcoming time off.</div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {blocks.map(b => {
                  const from = new Date(b.starts_at), to = new Date(b.ends_at)
                  const sameDay = b.starts_at.slice(0, 10) === b.ends_at.slice(0, 10)
                  return (
                    <div key={b.id} style={{
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                      gap: 12, padding: '9px 12px', borderRadius: 8, background: 'var(--surface3)',
                    }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                        <Badge color="amber">
                          {from.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })}
                        </Badge>
                        <span style={{ fontSize: 13, fontWeight: 600 }}>
                          {b.starts_at.slice(11, 16)} – {b.ends_at.slice(11, 16)}
                          {!sameDay && ` (${to.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })})`}
                        </span>
                        {b.reason && <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>{b.reason}</span>}
                      </div>
                      <Button variant="danger" size="sm" onClick={() => removeBlock(b.id)}>Remove</Button>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </>
      )}
      <Toast toast={toast} />
    </Card>
  )
}
