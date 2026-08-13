import { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../../api/client.js'
import { Badge, Button, Card, StatCard, Empty, Loading, useToast, Toast } from '../../components/UI.jsx'

// Vertical scale. An eight-hour day lands a little over 500px, which fits a
// laptop without scrolling and still leaves a 15-minute service readable.
const PX_PER_MIN = 1.15
const MIN_BLOCK_HEIGHT = 20
const COLUMN_MIN_WIDTH = 168

const STATUS_NEXT  = { Waiting: 'InService', InService: 'Done' }
const STATUS_LABEL = { Waiting: 'Start service', InService: 'Mark done' }

const ms      = (iso) => new Date(iso).getTime()
const hhmm    = (iso) => iso.slice(11, 16)
const todayISO = () => new Date().toISOString().split('T')[0]

function formatDateOption(dateStr) {
  const d = new Date(dateStr + 'T00:00:00')
  const today = new Date(); today.setHours(0, 0, 0, 0)
  const tomorrow = new Date(today); tomorrow.setDate(tomorrow.getDate() + 1)
  const label = d.toLocaleDateString('en-ZA', { weekday: 'short', day: 'numeric', month: 'short' })
  if (d.getTime() === today.getTime())    return `${label} (today)`
  if (d.getTime() === tomorrow.getTime()) return `${label} (tomorrow)`
  return label
}

function getNext7Days() {
  const days = []
  const today = new Date(); today.setHours(0, 0, 0, 0)
  for (let i = 0; i < 7; i++) {
    const d = new Date(today); d.setDate(d.getDate() + i)
    days.push(d.toISOString().split('T')[0])
  }
  return days
}

function hours(dayStart, dayEnd) {
  const out = []
  for (let t = ms(dayStart); t <= ms(dayEnd); t += 3600000) out.push(new Date(t))
  return out
}

/**
 * Side-by-side placement for entries that overlap in time.
 *
 * Nothing should ever overlap — the engine schedules around fixed slots — so
 * this exists to make a scheduling bug visible rather than to look tidy. Two
 * bookings drawn on top of each other would hide one of them, and a calendar
 * that hides a booking is worse than no calendar.
 *
 * Lanes are counted per cluster of connected overlaps, so one bad pair narrows
 * itself and not the whole day.
 */
function layout(entries) {
  const items = entries
    .map(e => ({ entry: e, start: ms(e.start), end: Math.max(ms(e.end), ms(e.start) + 60000) }))
    .sort((a, b) => a.start - b.start)

  const out = []
  let cluster = [], lanes = [], clusterEnd = -Infinity

  const flush = () => {
    cluster.forEach(it => { it.lanes = lanes.length })
    out.push(...cluster)
    cluster = []; lanes = []; clusterEnd = -Infinity
  }

  for (const it of items) {
    if (cluster.length && it.start >= clusterEnd) flush()
    let lane = lanes.findIndex(end => end <= it.start)
    if (lane === -1) { lanes.push(it.end); lane = lanes.length - 1 }
    else lanes[lane] = it.end
    it.lane = lane
    clusterEnd = Math.max(clusterEnd, it.end)
    cluster.push(it)
  }
  if (cluster.length) flush()
  return out
}

function blockStyle(status, isFixed) {
  if (status === 'Done')      return { bg: '#f1f5f9', border: '#cbd5e1', text: '#64748b' }
  if (status === 'InService') return { bg: 'var(--amber-dim)', border: 'var(--amber)', text: 'var(--amber)' }
  if (isFixed)                return { bg: 'var(--accent-dim)', border: 'var(--accent)', text: 'var(--accent)' }
  return { bg: 'var(--blue-dim)', border: 'var(--blue)', text: 'var(--blue)' }
}

// ── One agent's column ──────────────────────────────────────────────────────
function AgentColumn({ agent, dayStart, dayEnd, height, onPick, selectedId }) {
  const top = (iso) => ((ms(iso) - ms(dayStart)) / 60000) * PX_PER_MIN
  const placed = layout(agent.entries)

  return (
    <div style={{ flex: 1, minWidth: COLUMN_MIN_WIDTH, borderLeft: '1px solid var(--border)' }}>
      {/* column header */}
      <div style={{
        padding: '10px 12px', borderBottom: '1px solid var(--border)',
        background: '#f8f9fb', height: 62, boxSizing: 'border-box',
      }}>
        <div style={{ fontWeight: 700, fontSize: 13, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {agent.name}{!agent.is_active && ' (off)'}
        </div>
        <div style={{ fontFamily: 'var(--mono)', fontSize: 10.5, color: 'var(--muted)', marginTop: 4 }}>
          {agent.working
            ? `${Math.round(agent.booked_minutes / 6) / 10}h booked · ${Math.round(agent.free_minutes / 6) / 10}h free`
            : 'not working'}
        </div>
      </div>

      {/* body */}
      <div style={{ position: 'relative', height, background: 'repeating-linear-gradient(45deg, #fafafa, #fafafa 6px, #f3f4f6 6px, #f3f4f6 12px)' }}>
        {/* working hours sit on top of the hatched off-hours ground */}
        {agent.windows.map((w, i) => (
          <div key={i} style={{
            position: 'absolute', left: 0, right: 0,
            top: top(w.start), height: ((ms(w.end) - ms(w.start)) / 60000) * PX_PER_MIN,
            background: '#ffffff',
          }} />
        ))}

        {/* hour lines */}
        {hours(dayStart, dayEnd).map((h, i) => (
          <div key={i} style={{
            position: 'absolute', left: 0, right: 0,
            top: ((h.getTime() - ms(dayStart)) / 60000) * PX_PER_MIN,
            borderTop: '1px solid #eef0f3',
          }} />
        ))}

        {placed.map(({ entry, lane, lanes }) => {
          const c = blockStyle(entry.status, entry.is_fixed)
          const h = Math.max((entry.minutes || 0) * PX_PER_MIN, MIN_BLOCK_HEIGHT)
          const selected = selectedId === entry.id
          return (
            <button
              key={entry.id}
              onClick={() => onPick(selected ? null : { ...entry, agentName: agent.name })}
              title={`${hhmm(entry.start)}–${hhmm(entry.end)} · ${entry.customer_name} · ${entry.service}`}
              style={{
                position: 'absolute', top: top(entry.start), height: h,
                left: `calc(${(lane / lanes) * 100}% + 3px)`,
                width: `calc(${100 / lanes}% - 6px)`,
                background: c.bg, color: c.text,
                border: `1px solid ${c.border}`,
                borderLeft: `3px solid ${c.border}`,
                borderRadius: 6, padding: '3px 6px', textAlign: 'left',
                overflow: 'hidden', cursor: 'pointer', font: 'inherit',
                boxShadow: selected ? '0 0 0 2px var(--accent)' : 'none',
                opacity: entry.status === 'Done' ? 0.7 : 1,
              }}
            >
              <div style={{ fontFamily: 'var(--mono)', fontSize: 10, opacity: 0.85 }}>
                {hhmm(entry.start)}{entry.is_fixed ? ' ●' : ''}
              </div>
              {h > 32 && (
                <div style={{ fontSize: 11.5, fontWeight: 700, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {entry.customer_name}
                </div>
              )}
              {h > 52 && (
                <div style={{ fontSize: 11, opacity: 0.8, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {entry.service}
                </div>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ── Calendar page ───────────────────────────────────────────────────────────
export default function Calendar({ tenants }) {
  const [selectedTenant, setSelectedTenant] = useState(null)
  const [selectedDate, setSelectedDate]     = useState(todayISO())
  const [day, setDay]                       = useState(null)
  const [loading, setLoading]               = useState(false)
  const [picked, setPicked]                 = useState(null)
  const [updating, setUpdating]             = useState(false)
  const [now, setNow]                       = useState(() => Date.now())
  const { toast, show }                     = useToast()

  useEffect(() => {
    if (tenants.length && !selectedTenant) setSelectedTenant(tenants[0])
  }, [tenants])

  const load = useCallback(async () => {
    if (!selectedTenant) return
    setLoading(true)
    try { setDay(await api.getTimeline(selectedTenant.id, selectedDate)) }
    catch { show('Failed to load the day.', 'error') }
    finally { setLoading(false) }
  }, [selectedTenant, selectedDate])

  useEffect(() => { setPicked(null); load() }, [load])

  useEffect(() => {
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  // Moves the now-line without refetching the day.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 60000)
    return () => clearInterval(t)
  }, [])

  async function setStatus(entryId, status) {
    setUpdating(true)
    try {
      await api.updateStatus(entryId, status)
      show(`Marked as ${status}.`, 'success')
      setPicked(null)
      await load()
    } catch {
      show('Failed to update.', 'error')
    } finally {
      setUpdating(false)
    }
  }

  const agentLabel = selectedTenant?.agent_label || 'Agent'
  const isToday    = selectedDate === todayISO()

  const totals = useMemo(() => {
    const agents = day?.agents || []
    return {
      booked: agents.reduce((n, a) => n + a.booked_minutes, 0),
      free:   agents.reduce((n, a) => n + a.free_minutes, 0),
      fixed:  agents.reduce((n, a) => n + a.entries.filter(e => e.is_fixed).length, 0),
    }
  }, [day])

  const totalMinutes = day ? (ms(day.day_end) - ms(day.day_start)) / 60000 : 0
  const height       = totalMinutes * PX_PER_MIN
  const nowOffset    = day && isToday && now >= ms(day.day_start) && now <= ms(day.day_end)
    ? ((now - ms(day.day_start)) / 60000) * PX_PER_MIN
    : null

  const stray = day ? [...day.agents.flatMap(a => a.unplaced), ...day.orphaned] : []

  const selectStyle = {
    background: '#ffffff', border: '1px solid #d1d5db',
    borderRadius: 8, padding: '8px 14px', fontSize: 13,
    color: 'var(--text)', fontFamily: 'var(--sans)', fontWeight: 600,
    outline: 'none', cursor: 'pointer', maxWidth: '100%',
    boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
  }

  return (
    <div>
      {/* ── Header ──────────────────────────────────────────────── */}
      <div className="page-header">
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.01em' }}>Calendar</h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
            {selectedTenant?.business_name} — {formatDateOption(selectedDate)}
          </p>
        </div>
        <div className="page-header-actions">
          {tenants.length > 1 && (
            <select
              value={selectedTenant?.id || ''}
              onChange={e => setSelectedTenant(tenants.find(t => t.id === parseInt(e.target.value)))}
              style={selectStyle}
            >
              {tenants.map(t => <option key={t.id} value={t.id}>{t.business_name}</option>)}
            </select>
          )}
          <select
            value={selectedDate}
            onChange={e => setSelectedDate(e.target.value)}
            style={{ ...selectStyle, border: '1px solid var(--accent)', color: 'var(--accent)' }}
          >
            {getNext7Days().map(d => <option key={d} value={d}>{formatDateOption(d)}</option>)}
          </select>
          <Button variant="ghost" size="sm" onClick={load} loading={loading} aria-label="Refresh day">↻</Button>
        </div>
      </div>

      {/* ── Stats ───────────────────────────────────────────────── */}
      <div className="stats-grid">
        <StatCard label="Booked"       value={`${Math.round(totals.booked / 6) / 10}h`} color="var(--accent)" sub="of the day sold"        delay={0} />
        <StatCard label="Free"         value={`${Math.round(totals.free / 6) / 10}h`}   color="var(--green)"  sub="still bookable"         delay={60} />
        <StatCard label="Appointments" value={totals.fixed}                              color="var(--blue)"   sub="fixed times promised"   delay={120} />
      </div>

      {/* ── Selected entry ──────────────────────────────────────── */}
      {picked && (
        <Card className="animate-fade-up" style={{ padding: '14px 18px', marginBottom: 12, display: 'flex', gap: 14, alignItems: 'center', flexWrap: 'wrap' }}>
          <Badge color={picked.is_fixed ? 'indigo' : 'blue'}>
            {picked.is_fixed ? 'Appointment' : 'Queue'}
          </Badge>
          <div style={{ fontWeight: 700, fontSize: 14 }}>{picked.customer_name}</div>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 12.5 }}>
            {hhmm(picked.start)}–{hhmm(picked.end)}
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--muted)' }}>
            {picked.service} · {picked.agentName}
            {picked.customer_number !== 'walkin' && ` · ${picked.customer_number}`}
          </div>
          <div style={{ display: 'flex', gap: 8, marginLeft: 'auto', flexWrap: 'wrap' }}>
            {STATUS_NEXT[picked.status] && (
              <Button size="sm" variant={picked.status === 'Waiting' ? 'outline' : 'primary'}
                loading={updating}
                onClick={() => setStatus(picked.id, STATUS_NEXT[picked.status])}>
                {STATUS_LABEL[picked.status]}
              </Button>
            )}
            {picked.status === 'Waiting' && (
              <Button size="sm" variant="danger" loading={updating}
                onClick={() => setStatus(picked.id, 'NoShow')}>No Show</Button>
            )}
            <Button size="sm" variant="ghost" onClick={() => setPicked(null)}>Close</Button>
          </div>
        </Card>
      )}

      {/* ── Grid ────────────────────────────────────────────────── */}
      <Card className="animate-fade-up" style={{ overflow: 'hidden', animationDelay: '180ms' }}>
        {loading && !day ? (
          <Loading message="Loading the day..." />
        ) : !day || day.agents.length === 0 ? (
          <Empty
            message={`No ${agentLabel.toLowerCase()}s to show.`}
            hint={`Add a ${agentLabel.toLowerCase()} and set their working hours to see the day.`}
          />
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <div style={{ display: 'flex', minWidth: 'min-content', position: 'relative' }}>
              {/* time gutter */}
              <div style={{ width: 54, flexShrink: 0 }}>
                <div style={{ height: 62, borderBottom: '1px solid var(--border)', background: '#f8f9fb' }} />
                <div style={{ position: 'relative', height }}>
                  {hours(day.day_start, day.day_end).map((h, i) => (
                    <div key={i} style={{
                      position: 'absolute', right: 8, top: ((h.getTime() - ms(day.day_start)) / 60000) * PX_PER_MIN - 6,
                      fontFamily: 'var(--mono)', fontSize: 10.5, color: 'var(--muted2)',
                    }}>
                      {String(h.getHours()).padStart(2, '0')}:00
                    </div>
                  ))}
                </div>
              </div>

              {day.agents.map(a => (
                <AgentColumn key={a.agent_id} agent={a}
                  dayStart={day.day_start} dayEnd={day.day_end}
                  height={height} onPick={setPicked} selectedId={picked?.id} />
              ))}

              {/* now-line, drawn over every column */}
              {nowOffset !== null && (
                <div style={{
                  position: 'absolute', left: 46, right: 0, top: 62 + nowOffset,
                  borderTop: '2px solid var(--red)', pointerEvents: 'none', zIndex: 5,
                }}>
                  <span style={{
                    position: 'absolute', left: 0, top: -7, width: 8, height: 8,
                    borderRadius: '50%', background: 'var(--red)',
                  }} />
                </div>
              )}
            </div>
          </div>
        )}
      </Card>

      {/* Anything the grid could not draw is stated rather than dropped. */}
      {stray.length > 0 && (
        <Card style={{ padding: '12px 16px', marginTop: 12, borderColor: 'var(--amber)', background: 'var(--amber-dim)' }}>
          <div style={{ fontSize: 12.5, color: 'var(--amber)', fontWeight: 600, marginBottom: 6 }}>
            {stray.length} entr{stray.length === 1 ? 'y is' : 'ies are'} not on the grid — no time, or no {agentLabel.toLowerCase()}.
          </div>
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            {stray.map(e => `${e.customer_name} (${e.service})`).join(', ')}
          </div>
        </Card>
      )}

      <div style={{ display: 'flex', gap: 16, marginTop: 12, flexWrap: 'wrap', fontSize: 11.5, color: 'var(--muted)' }}>
        <span>● fixed appointment</span>
        <span style={{ color: 'var(--blue)' }}>▮ queue — time may move</span>
        <span style={{ color: 'var(--amber)' }}>▮ in service</span>
        <span>▨ outside working hours</span>
      </div>

      <Toast toast={toast} />
    </div>
  )
}
