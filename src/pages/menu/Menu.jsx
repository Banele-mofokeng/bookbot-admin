import { useState, useEffect } from 'react'
import { api } from '../../api/client.js'
import { Badge, Button, Card, Empty, Loading, Input, useToast, Toast } from '../../components/UI.jsx'

// The API speaks cents; the kitchen speaks rands. Convert at the edge only, so
// nothing in between can lose half a cent to a float.
function toRands(cents) {
  return (cents / 100).toFixed(2).replace(/\.00$/, '')
}

function toCents(rands) {
  const n = parseFloat(String(rands).replace(',', '.'))
  return Number.isFinite(n) ? Math.round(n * 100) : 0
}

function money(cents, symbol = 'R') {
  return `${symbol}${toRands(cents)}`
}

function MenuItemForm({ tenantId, item, onSave, onCancel }) {
  const [form, setForm] = useState(item
    ? { ...item, price: toRands(item.price_cents) }
    : { tenant_id: tenantId, name: '', category: '', price: '', prep_minutes: 10, sort_order: 0, is_active: true }
  )
  const [saving, setSaving] = useState(false)
  const { toast, show } = useToast()

  async function submit() {
    if (!form.name.trim()) { show('Name is required.', 'error'); return }
    const price_cents = toCents(form.price)
    if (price_cents <= 0) { show('Give the item a price.', 'error'); return }
    setSaving(true)
    try {
      const payload = {
        name: form.name.trim(),
        category: form.category.trim(),
        price_cents,
        prep_minutes: parseInt(form.prep_minutes) || 0,
        sort_order: parseInt(form.sort_order) || 0,
      }
      const result = item
        ? await api.updateMenuItem(item.id, payload)
        : await api.createMenuItem({ ...payload, tenant_id: tenantId, is_active: true })
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
        <Input label="Item Name" value={form.name}
          onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
          placeholder="e.g. Full House Kota" />
        <Input label="Category" value={form.category}
          onChange={e => setForm(f => ({ ...f, category: e.target.value }))}
          placeholder="e.g. Kotas" />
        <Input label="Price (R)" value={form.price} inputMode="decimal"
          onChange={e => setForm(f => ({ ...f, price: e.target.value }))}
          placeholder="45" />
        <Input label="Prep (min)" type="number" value={form.prep_minutes}
          onChange={e => setForm(f => ({ ...f, prep_minutes: e.target.value }))}
          min={0} />
        <Input label="Sort" type="number" value={form.sort_order}
          onChange={e => setForm(f => ({ ...f, sort_order: e.target.value }))}
          title="Lower shows first. Also decides which category leads." />
        <Button onClick={submit} loading={saving} style={{ marginBottom: 0 }}>Save</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
      <Toast toast={toast} />
    </Card>
  )
}

export default function Menu({ tenants }) {
  const orderingTenants = tenants.filter(t => t.mode === 'orders')
  const [selectedTenantId, setSelectedTenantId] = useState(orderingTenants[0]?.id || null)
  const [items, setItems]     = useState([])
  const [loading, setLoading] = useState(false)
  const [adding, setAdding]   = useState(false)
  const [editing, setEditing] = useState(null)
  const { toast, show }       = useToast()

  async function load(id) {
    setLoading(true)
    try { setItems(await api.getMenu(id)) }
    catch { show('Failed to load the menu.', 'error') }
    finally { setLoading(false) }
  }

  useEffect(() => { if (selectedTenantId) load(selectedTenantId) }, [selectedTenantId])
  useEffect(() => {
    if (orderingTenants.length && !selectedTenantId) setSelectedTenantId(orderingTenants[0].id)
  }, [tenants])

  async function toggleActive(item) {
    try {
      const updated = await api.updateMenuItem(item.id, { is_active: !item.is_active })
      setItems(list => list.map(x => x.id === item.id ? updated : x))
      show(`${item.name} ${item.is_active ? 'marked sold out' : 'back on the menu'}.`, 'success')
    } catch { show('Failed to update.', 'error') }
  }

  async function remove(item) {
    if (!window.confirm(`Remove ${item.name} from the menu?\n\nPast orders keep their own copy of the name and price, so takings are unaffected.`)) return
    try {
      await api.deleteMenuItem(item.id)
      setItems(list => list.filter(x => x.id !== item.id))
      show(`${item.name} removed.`, 'success')
    } catch { show('Failed to remove.', 'error') }
  }

  const tenant = tenants.find(t => t.id === selectedTenantId)
  const symbol = tenant?.currency_symbol || 'R'

  // The API returns items already in the order customers see them, categories
  // and all — grouping here just draws the headings it implies.
  const groups = []
  for (const item of items) {
    const name = item.category || 'More'
    const last = groups[groups.length - 1]
    if (last && last.name === name) last.items.push(item)
    else groups.push({ name, items: [item] })
  }

  if (!orderingTenants.length) {
    return (
      <Card>
        <Empty message="No takeaway businesses yet."
          hint="Set a business to 'Takeaway orders' mode to give it a menu." />
      </Card>
    )
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800 }}>Menu</h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
            {tenant ? `What customers can order from ${tenant.business_name}` : ''}
          </p>
        </div>
        <div className="page-header-actions">
          {orderingTenants.length > 1 && (
            <select value={selectedTenantId || ''} onChange={e => setSelectedTenantId(parseInt(e.target.value))}
              style={{ background: '#ffffff', border: '1px solid #d1d5db', borderRadius: 8, padding: '8px 14px', fontSize: 13, color: 'var(--text)', fontFamily: 'var(--sans)', fontWeight: 600, outline: 'none', cursor: 'pointer', boxShadow: '0 1px 2px rgba(0,0,0,0.04)' }}>
              {orderingTenants.map(t => <option key={t.id} value={t.id}>{t.business_name}</option>)}
            </select>
          )}
          <Button onClick={() => { setAdding(true); setEditing(null) }}>+ Add Item</Button>
        </div>
      </div>

      {adding && (
        <MenuItemForm tenantId={selectedTenantId}
          onSave={item => { setItems(list => [...list, item]); setAdding(false); show('Item added.', 'success') }}
          onCancel={() => setAdding(false)} />
      )}

      {loading ? <Loading message="Loading the menu..." /> : items.length === 0 && !adding ? (
        <Card><Empty message="Nothing on the menu yet." hint="Use '+ Add Item' to put something up." /></Card>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
          {groups.map(group => (
            <div key={group.name}>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase', color: 'var(--muted2)', marginBottom: 10 }}>
                {group.name}
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {group.items.map((item, i) => (
                  editing === item.id ? (
                    <MenuItemForm key={item.id} tenantId={selectedTenantId} item={item}
                      onSave={updated => { setItems(list => list.map(x => x.id === item.id ? updated : x)); setEditing(null); show('Updated.', 'success') }}
                      onCancel={() => setEditing(null)} />
                  ) : (
                    <Card key={item.id} className="animate-fade-up" style={{ padding: '16px 20px', animationDelay: `${i * 40}ms`, opacity: item.is_active ? 1 : 0.6 }}>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
                          <div style={{ minWidth: 56, height: 40, padding: '0 8px', background: 'var(--accent-dim)', borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--accent)', fontWeight: 700 }}>
                            {money(item.price_cents, symbol)}
                          </div>
                          <div>
                            <div style={{ fontWeight: 700, fontSize: 14 }}>{item.name}</div>
                            <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>
                              {item.prep_minutes} min to make
                            </div>
                          </div>
                        </div>
                        <div className="card-row-actions">
                          <Badge color={item.is_active ? 'green' : 'red'}>
                            {item.is_active ? 'On the menu' : 'Sold out'}
                          </Badge>
                          <Button variant="ghost" size="sm" onClick={() => setEditing(item.id)}>Edit</Button>
                          <Button variant={item.is_active ? 'danger' : 'outline'} size="sm" onClick={() => toggleActive(item)}>
                            {item.is_active ? 'Sold out' : 'Back on'}
                          </Button>
                          <Button variant="ghost" size="sm" onClick={() => remove(item)}>Remove</Button>
                        </div>
                      </div>
                    </Card>
                  )
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
      <Toast toast={toast} />
    </div>
  )
}
