import { useState, useEffect, useCallback } from 'react'
import { api } from '../../api/client.js'
import { Badge, Button, Card, StatCard, Empty, Loading, Select, Input, useToast, Toast } from '../../components/UI.jsx'

const STATUS_COLOR = {
  Placed:    'blue',
  Preparing: 'amber',
  Ready:     'green',
  Collected: 'gray',
  Cancelled: 'red',
}

// What the one big button does next, in the order the kitchen works.
const STATUS_NEXT  = { Placed: 'Preparing', Preparing: 'Ready', Ready: 'Collected' }
const STATUS_LABEL = { Placed: 'Start making', Preparing: 'Mark ready', Ready: 'Collected' }

function money(cents, symbol = 'R') {
  return `${symbol}${(cents / 100).toFixed(2).replace(/\.00$/, '')}`
}

function clockTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleTimeString('en-ZA', { hour: '2-digit', minute: '2-digit', hour12: false })
}

function formatDateOption(dateStr) {
  const d = new Date(dateStr + 'T00:00:00')
  const today = new Date(); today.setHours(0, 0, 0, 0)
  const yesterday = new Date(today); yesterday.setDate(yesterday.getDate() - 1)
  const label = d.toLocaleDateString('en-ZA', { weekday: 'short', day: 'numeric', month: 'short' })
  if (d.getTime() === today.getTime())     return `${label} (today)`
  if (d.getTime() === yesterday.getTime()) return `${label} (yesterday)`
  return label
}

function getLast7Days() {
  const days = []
  const today = new Date(); today.setHours(0, 0, 0, 0)
  for (let i = 0; i < 7; i++) {
    const d = new Date(today); d.setDate(d.getDate() - i)
    days.push(d.toISOString().split('T')[0])
  }
  return days
}

// ── Counter order modal ────────────────────────────────────────────────────
function CounterOrderModal({ tenant, menu, onClose, onAdd }) {
  const [name, setName]   = useState('')
  const [phone, setPhone] = useState('')
  const [note, setNote]   = useState('')
  const [qtys, setQtys]   = useState({})
  const [saving, setSaving] = useState(false)
  const { toast, show } = useToast()

  const symbol = tenant?.currency_symbol || 'R'
  const available = menu.filter(m => m.is_active)
  const lines = available
    .map(m => ({ item: m, qty: qtys[m.id] || 0 }))
    .filter(l => l.qty > 0)
  const total = lines.reduce((sum, l) => sum + l.item.price_cents * l.qty, 0)

  function bump(id, delta) {
    setQtys(q => {
      const next = Math.max(0, Math.min(20, (q[id] || 0) + delta))
      return { ...q, [id]: next }
    })
  }

  async function submit() {
    if (!lines.length) { show('Add at least one item.', 'error'); return }
    setSaving(true)
    try {
      const order = await api.addCounterOrder({
        tenant_id: tenant.id,
        customer_name: name.trim() || 'Walk-in',
        customer_phone: phone.trim(),
        note: note.trim(),
        items: lines.map(l => ({ menu_item_id: l.item.id, qty: l.qty })),
      })
      onAdd(order)
    } catch (e) {
      show(e.message || 'Failed to place the order.', 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(17,24,39,0.45)', backdropFilter: 'blur(4px)', WebkitBackdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 300, padding: 16 }}>
      <Card className="walkin-modal-card animate-fade-up">
        <div>
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 4 }}>
            <div style={{ fontSize: 16, fontWeight: 800 }}>Counter Order</div>
            <button onClick={onClose} aria-label="Close"
              style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', padding: 4, borderRadius: 6, display: 'flex' }}>
              <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                <path d="M4 4l10 10M14 4L4 14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"/>
              </svg>
            </button>
          </div>
          <p style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 18 }}>
            Goes on the same board as WhatsApp orders, so the ready time counts it too.
          </p>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginBottom: 18 }}>
            <Input label="Customer name" value={name} onChange={e => setName(e.target.value)} placeholder="Walk-in" />
            <Input label="Phone (optional)" value={phone} onChange={e => setPhone(e.target.value)}
              placeholder="0764519653" inputMode="tel" />
            <Input label="Note (optional)" value={note} onChange={e => setNote(e.target.value)} placeholder="no chilli" />
          </div>

          {available.length === 0 ? (
            <Empty message="Nothing is on the menu right now." />
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 18 }}>
              {available.map(item => (
                <div key={item.id} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 8 }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.name}</div>
                    <div style={{ fontSize: 11, color: 'var(--muted)' }}>{money(item.price_cents, symbol)}</div>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
                    <Button variant="ghost" size="sm" onClick={() => bump(item.id, -1)}>−</Button>
                    <span style={{ fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 700, minWidth: 18, textAlign: 'center' }}>
                      {qtys[item.id] || 0}
                    </span>
                    <Button variant="outline" size="sm" onClick={() => bump(item.id, 1)}>+</Button>
                  </div>
                </div>
              ))}
            </div>
          )}

          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted2)' }}>Total</span>
            <span style={{ fontSize: 20, fontWeight: 800 }}>{money(total, symbol)}</span>
          </div>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={onClose}>Cancel</Button>
            <Button onClick={submit} loading={saving}>Place order</Button>
          </div>
        </div>
        <Toast toast={toast} />
      </Card>
    </div>
  )
}

// ── One order on the board ─────────────────────────────────────────────────
function OrderCard({ order, symbol, onAdvance, onCancel, delay }) {
  const next = STATUS_NEXT[order.status]
  const late = order.ready_at
    && ['Placed', 'Preparing'].includes(order.status)
    && new Date(order.ready_at) < new Date()

  return (
    <Card className="animate-fade-up" style={{ padding: '16px 20px', animationDelay: `${delay}ms` }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 14, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14, minWidth: 0 }}>
          <div style={{ width: 46, height: 46, background: 'var(--accent-dim)', borderRadius: 10, display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: 'var(--mono)', fontSize: 14, color: 'var(--accent)', fontWeight: 800, flexShrink: 0 }}>
            {order.code}
          </div>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 700, fontSize: 14 }}>
              {order.customer_name || 'Walk-in'}
              {order.placed_via === 'counter' && (
                <span style={{ fontSize: 11, color: 'var(--muted)', fontWeight: 600, marginLeft: 8 }}>counter</span>
              )}
            </div>
            <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 3 }}>
              {order.items.map(i => `${i.qty} × ${i.name}`).join(', ')}
            </div>
            {order.note && (
              <div style={{ fontSize: 12, color: 'var(--amber)', marginTop: 4 }}>“{order.note}”</div>
            )}
            <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: late ? 'var(--red)' : 'var(--muted2)', marginTop: 6 }}>
              {money(order.total_cents, symbol)} · ready {clockTime(order.ready_at)}
              {late && ' · running late'}
            </div>
          </div>
        </div>

        <div className="card-row-actions">
          <Badge color={STATUS_COLOR[order.status]}>{order.status}</Badge>
          {next && (
            <Button size="sm" onClick={() => onAdvance(order, next)}>{STATUS_LABEL[order.status]}</Button>
          )}
          {['Placed', 'Preparing'].includes(order.status) && (
            <Button variant="danger" size="sm" onClick={() => onCancel(order)}>Cancel</Button>
          )}
        </div>
      </div>
    </Card>
  )
}

export default function Orders({ tenants }) {
  const orderingTenants = tenants.filter(t => t.mode === 'orders')
  const [selectedTenantId, setSelectedTenantId] = useState(orderingTenants[0]?.id || null)
  const [date, setDate]         = useState(() => new Date().toISOString().split('T')[0])
  const [board, setBoard]       = useState(null)
  const [menu, setMenu]         = useState([])
  const [loading, setLoading]   = useState(true)
  const [showCounter, setShowCounter] = useState(false)
  const { toast, show }         = useToast()

  const tenant = tenants.find(t => t.id === selectedTenantId)
  const symbol = tenant?.currency_symbol || 'R'

  const loadBoard = useCallback(async () => {
    if (!selectedTenantId) return
    try { setBoard(await api.getOrders(selectedTenantId, date)) }
    catch { show('Failed to load orders.', 'error') }
    finally { setLoading(false) }
  }, [selectedTenantId, date])

  useEffect(() => { setLoading(true); loadBoard() }, [loadBoard])

  // A kitchen board goes stale fast — two people work the same list.
  useEffect(() => {
    const interval = setInterval(loadBoard, 15000)
    return () => clearInterval(interval)
  }, [loadBoard])

  useEffect(() => {
    if (!selectedTenantId) return
    api.getMenu(selectedTenantId).then(setMenu).catch(() => setMenu([]))
  }, [selectedTenantId])

  useEffect(() => {
    if (orderingTenants.length && !selectedTenantId) setSelectedTenantId(orderingTenants[0].id)
  }, [tenants])

  async function advance(order, next) {
    try {
      const updated = await api.updateOrderStatus(order.id, next)
      setBoard(b => ({ ...b, orders: b.orders.map(o => o.id === order.id ? updated : o) }))
      show(next === 'Ready'
        ? `#${order.code} is up — the customer has been messaged.`
        : `#${order.code} → ${next}.`, 'success')
      loadBoard()
    } catch { show('Failed to update the order.', 'error') }
  }

  async function cancel(order) {
    if (!window.confirm(`Cancel order #${order.code}?\n\nThe customer will be messaged.`)) return
    try {
      await api.updateOrderStatus(order.id, 'Cancelled')
      show(`#${order.code} cancelled.`, 'success')
      loadBoard()
    } catch { show('Failed to cancel.', 'error') }
  }

  if (!orderingTenants.length) {
    return (
      <Card>
        <Empty message="No takeaway businesses yet."
          hint="Set a business to 'Takeaway orders' mode to open a kitchen board." />
      </Card>
    )
  }

  const all       = board?.orders || []
  const kitchen   = all.filter(o => ['Placed', 'Preparing'].includes(o.status))
  const ready     = all.filter(o => o.status === 'Ready')
  const closed    = all.filter(o => ['Collected', 'Cancelled'].includes(o.status))
  const summary   = board?.summary || { open: 0, ready: 0, collected: 0, takings_cents: 0 }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800 }}>Orders</h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
            {tenant ? `Kitchen board for ${tenant.business_name}` : ''}
          </p>
        </div>
        <div className="page-header-actions">
          {orderingTenants.length > 1 && (
            <select value={selectedTenantId || ''} onChange={e => setSelectedTenantId(parseInt(e.target.value))}
              style={{ background: '#ffffff', border: '1px solid #d1d5db', borderRadius: 8, padding: '8px 14px', fontSize: 13, color: 'var(--text)', fontFamily: 'var(--sans)', fontWeight: 600, outline: 'none', cursor: 'pointer', boxShadow: '0 1px 2px rgba(0,0,0,0.04)' }}>
              {orderingTenants.map(t => <option key={t.id} value={t.id}>{t.business_name}</option>)}
            </select>
          )}
          <select value={date} onChange={e => setDate(e.target.value)}
            style={{ background: '#ffffff', border: '1px solid #d1d5db', borderRadius: 8, padding: '8px 14px', fontSize: 13, color: 'var(--text)', fontFamily: 'var(--sans)', fontWeight: 600, outline: 'none', cursor: 'pointer', boxShadow: '0 1px 2px rgba(0,0,0,0.04)' }}>
            {getLast7Days().map(d => <option key={d} value={d}>{formatDateOption(d)}</option>)}
          </select>
          <Button onClick={() => setShowCounter(true)}>+ Counter Order</Button>
        </div>
      </div>

      <div className="stats-grid-4">
        <StatCard label="In the kitchen" value={summary.open}       color="var(--amber)" sub="being made"    delay={0} />
        <StatCard label="Ready"          value={summary.ready}      color="var(--green)" sub="on the pass"   delay={60} />
        <StatCard label="Collected"      value={summary.collected}  color="var(--blue)"  sub="handed over"   delay={120} />
        <StatCard label="Takings"        value={money(summary.takings_cents, symbol)} color="var(--accent)" sub="collected orders only" delay={180} />
      </div>

      {loading ? <Loading message="Loading orders..." /> : all.length === 0 ? (
        <Card><Empty message="No orders yet today." hint="WhatsApp orders land here the moment they're placed." /></Card>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 24, marginTop: 20 }}>
          {[['In the kitchen', kitchen], ['Ready for collection', ready], ['Closed', closed]].map(([label, group]) => (
            group.length > 0 && (
              <div key={label}>
                <div style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase', color: 'var(--muted2)', marginBottom: 10 }}>
                  {label} · {group.length}
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {group.map((order, i) => (
                    <OrderCard key={order.id} order={order} symbol={symbol}
                      onAdvance={advance} onCancel={cancel} delay={i * 40} />
                  ))}
                </div>
              </div>
            )
          ))}
        </div>
      )}

      {showCounter && tenant && (
        <CounterOrderModal tenant={tenant} menu={menu}
          onClose={() => setShowCounter(false)}
          onAdd={order => { setShowCounter(false); show(`Order #${order.code} placed.`, 'success'); loadBoard() }} />
      )}
      <Toast toast={toast} />
    </div>
  )
}
