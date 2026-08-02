import { useState, useEffect } from 'react'
import { api } from '../../api/client.js'
import { Badge, Button, Card, Empty, Loading, useToast, Toast } from '../../components/UI.jsx'

const RANGES = [
  { label: '7 days',  days: 7 },
  { label: '30 days', days: 30 },
  { label: '90 days', days: 90 },
]

const isoDaysAgo = (n) => {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return d.toISOString().slice(0, 10)
}

const pct = (v) => v === null || v === undefined ? '—' : `${Math.round(v * 1000) / 10}%`
const mins = (v) => v === null || v === undefined ? '—' : `${v} min`

const labelStyle = {
  fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em',
  textTransform: 'uppercase', color: 'var(--muted2)',
}

/** A single headline number. `hint` explains what it is not, when that matters. */
function Stat({ label, value, hint, tone }) {
  return (
    <Card style={{ padding: '16px 18px', flex: '1 1 150px', minWidth: 150 }}>
      <div style={labelStyle}>{label}</div>
      <div style={{
        fontSize: 26, fontWeight: 800, marginTop: 6,
        color: tone === 'muted' ? 'var(--muted2)' : tone === 'amber' ? 'var(--amber)' : 'var(--text)',
      }}>
        {value}
      </div>
      {hint && <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 5, lineHeight: 1.45 }}>{hint}</div>}
    </Card>
  )
}

/** Horizontal bars. No chart library — the CSP on this app blocks external JS. */
function BarChart({ rows, valueKey = 'bookings', labelKey = 'name', formatValue }) {
  const max = Math.max(1, ...rows.map(r => r[valueKey]))
  if (!rows.some(r => r[valueKey] > 0)) {
    return <div style={{ fontSize: 12.5, color: 'var(--muted2)', padding: '8px 0' }}>Nothing in this period.</div>
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {rows.map((r, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ width: 78, fontSize: 12, color: 'var(--muted)', flexShrink: 0, textAlign: 'right' }}>
            {r[labelKey]}
          </div>
          <div style={{ flex: 1, height: 18, background: 'var(--surface3)', borderRadius: 4, overflow: 'hidden' }}>
            <div style={{
              width: `${(r[valueKey] / max) * 100}%`, height: '100%',
              background: 'var(--accent)', borderRadius: 4, minWidth: r[valueKey] ? 2 : 0,
              transition: 'width 0.3s',
            }} />
          </div>
          <div style={{ width: 46, fontSize: 12, fontWeight: 700, flexShrink: 0 }}>
            {formatValue ? formatValue(r[valueKey]) : r[valueKey]}
          </div>
        </div>
      ))}
    </div>
  )
}

function Section({ title, subtitle, children }) {
  return (
    <Card style={{ padding: 20, marginBottom: 14 }}>
      <div style={{ marginBottom: 14 }}>
        <div style={{ fontWeight: 800, fontSize: 14 }}>{title}</div>
        {subtitle && <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 3 }}>{subtitle}</div>}
      </div>
      {children}
    </Card>
  )
}

export default function Reports({ tenants }) {
  const [selectedTenantId, setSelectedTenantId] = useState(tenants[0]?.id || null)
  const [days, setDays]       = useState(30)
  const [data, setData]       = useState(null)
  const [loading, setLoading] = useState(false)
  const { toast, show }       = useToast()

  async function load() {
    if (!selectedTenantId) return
    setLoading(true)
    try {
      setData(await api.getAnalytics(selectedTenantId, isoDaysAgo(days - 1)))
    } catch (e) {
      show(e.message || 'Failed to load report.', 'error')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [selectedTenantId, days])
  useEffect(() => { if (tenants.length && !selectedTenantId) setSelectedTenantId(tenants[0].id) }, [tenants])

  const tenant = tenants.find(t => t.id === selectedTenantId)
  const t = data?.totals
  const r = data?.rates

  // Only show trading hours — a 24-bar chart is mostly empty for a salon.
  const openHour  = tenant?.queue_opens ?? 8
  const closeHour = tenant?.queue_closes ?? 17
  const hours = (data?.by_hour || [])
    .filter(h => h.hour >= Math.max(0, openHour - 1) && h.hour <= Math.min(23, closeHour))
    .map(h => ({ ...h, name: `${String(h.hour).padStart(2, '0')}:00` }))

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800 }}>Reports</h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
            {tenant ? `${tenant.business_name} — last ${days} days` : ''}
          </p>
        </div>
        <div className="page-header-actions">
          {tenants.length > 1 && (
            <select value={selectedTenantId || ''} onChange={e => setSelectedTenantId(parseInt(e.target.value))}
              style={{ background: '#ffffff', border: '1px solid #d1d5db', borderRadius: 8, padding: '8px 14px', fontSize: 13, color: 'var(--text)', fontFamily: 'var(--sans)', fontWeight: 600, outline: 'none', cursor: 'pointer' }}>
              {tenants.map(x => <option key={x.id} value={x.id}>{x.business_name}</option>)}
            </select>
          )}
          {RANGES.map(({ label, days: d }) => (
            <Button key={d} size="sm" variant={days === d ? 'primary' : 'outline'} onClick={() => setDays(d)}>
              {label}
            </Button>
          ))}
        </div>
      </div>

      {loading ? <Loading message="Crunching numbers..." /> : !data ? (
        <Card><Empty message="No report yet." hint="Pick a business to see its numbers." /></Card>
      ) : t.bookings === 0 ? (
        <Card><Empty message={`No bookings in the last ${days} days.`} hint="Reports fill in as customers join the queue." /></Card>
      ) : (
        <>
          {/* ── headline ───────────────────────────────────────────── */}
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 14 }}>
            <Stat label="Bookings" value={t.bookings}
              hint={`${data.channel.whatsapp} WhatsApp · ${data.channel.walkin} walk-in`} />
            <Stat label="Served" value={t.done} hint={`${pct(r.completion)} of closed bookings`} />
            <Stat label="No-shows" value={r.no_show === null ? '—' : t.no_shows}
              tone={r.no_show === null ? 'muted' : undefined}
              hint={r.no_show === null ? 'Not available for this period' : pct(r.no_show)} />
            <Stat label="Cancelled" value={t.cancelled}
              hint={`${t.cancelled_by_customer} by the customer`} />
            {t.still_open > 0 && (
              <Stat label="Still open" value={t.still_open}
                tone="muted" hint="Not counted in any rate yet" />
            )}
          </div>

          {/* ── the honesty banners ────────────────────────────────── */}
          {r.no_show_note && (
            <Card style={{ padding: '14px 18px', marginBottom: 14, borderColor: 'var(--blue)', background: 'var(--blue-dim)' }}>
              <div style={{ fontSize: 12.5, color: 'var(--blue)', lineHeight: 1.5 }}>
                <strong>No-show rate withheld.</strong> {r.no_show_note}
              </div>
            </Card>
          )}

          {t.unclosed > 0 && (
            <Card style={{ padding: '14px 18px', marginBottom: 14, borderColor: 'var(--amber)', background: 'var(--amber-dim)' }}>
              <div style={{ fontSize: 12.5, color: 'var(--amber)', lineHeight: 1.55 }}>
                <strong>{t.unclosed} booking{t.unclosed === 1 ? '' : 's'} closed automatically at midnight
                ({pct(r.unclosed)}).</strong> Nobody marked them Done or No-show, so the overnight
                reset closed them. These are <em>not</em> counted as no-shows — there's no way to
                know whether those customers arrived. Marking people Done as you go makes every
                number on this page sharper.
              </div>
            </Card>
          )}

          {/* ── measured times ─────────────────────────────────────── */}
          <Section title="Waiting and service times"
            subtitle="Measured from real timestamps, not estimates. Collected from the day this feature went live.">
            {!data.wait_time.available && !data.service_time.available ? (
              <div style={{ fontSize: 12.5, color: 'var(--muted2)', lineHeight: 1.5 }}>
                Nothing measured yet. These fill in as staff mark customers InService and Done —
                bookings closed before that will never have times.
              </div>
            ) : (
              <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                <Stat label="Typical wait" value={mins(data.wait_time.median_minutes)}
                  hint={data.wait_time.available
                    ? `9 in 10 seen within ${mins(data.wait_time.p90_minutes)} · ${data.wait_time.samples} measured`
                    : 'No measurements yet'} />
                <Stat label="Typical service" value={mins(data.service_time.median_minutes)}
                  hint={data.service_time.available
                    ? `9 in 10 done within ${mins(data.service_time.p90_minutes)} · ${data.service_time.samples} measured`
                    : 'No measurements yet'} />
              </div>
            )}
          </Section>

          {/* ── when the work arrives ──────────────────────────────── */}
          <Section title="Busiest times of day" subtitle="When bookings come in — use it to plan cover.">
            <BarChart rows={hours} />
          </Section>

          <Section title="Busiest days of the week">
            <BarChart rows={(data.by_weekday || []).map(w => ({ ...w, name: w.name.slice(0, 3) }))} />
          </Section>

          {/* ── who and what ───────────────────────────────────────── */}
          <Section title={`${tenant?.agent_label || 'Agent'} workload`}>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13, minWidth: 420 }}>
                <thead>
                  <tr style={{ textAlign: 'left' }}>
                    {['Name', 'Bookings', 'Served', 'No-shows', 'Left open'].map(h => (
                      <th key={h} style={{ ...labelStyle, padding: '0 10px 8px 0', whiteSpace: 'nowrap' }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.by_agent.map(row => (
                    <tr key={row.agent_id} style={{ borderTop: '1px solid #e5e7eb' }}>
                      <td style={{ padding: '9px 10px 9px 0', fontWeight: 700 }}>{row.name}</td>
                      <td style={{ padding: '9px 10px 9px 0' }}>{row.bookings}</td>
                      <td style={{ padding: '9px 10px 9px 0' }}>{row.done}</td>
                      <td style={{ padding: '9px 10px 9px 0' }}>{row.no_shows}</td>
                      <td style={{ padding: '9px 10px 9px 0' }}>
                        {row.unclosed > 0
                          ? <Badge color="amber">{row.unclosed}</Badge>
                          : <span style={{ color: 'var(--muted2)' }}>0</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          <Section title={`${tenant?.service_label || 'Service'} mix`}
            subtitle="Hours booked is scheduled time, from each service's set duration — not measured.">
            <BarChart rows={data.by_service} />
            <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px solid #e5e7eb' }}>
              <BarChart rows={data.by_service} valueKey="minutes_booked"
                formatValue={v => v >= 60 ? `${Math.round(v / 60)}h` : `${v}m`} />
            </div>
          </Section>

          <div style={{ fontSize: 11.5, color: 'var(--muted2)', textAlign: 'center', padding: '8px 0 24px' }}>
            {data.from_date} to {data.to_date} · {t.closed} closed of {t.bookings} bookings
          </div>
        </>
      )}
      <Toast toast={toast} />
    </div>
  )
}
