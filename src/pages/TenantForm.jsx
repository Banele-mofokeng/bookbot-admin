import { useState, useEffect } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client.js'
import { Button, Card, useToast, Toast } from '../components/UI.jsx'

const EMPTY = {
  business_name:      '',
  business_type:      'Hair Salon',
  whatsapp_number:    '',
  owner_number:       '',
  agent_label:        'Stylist',
  service_label:      'Hair Service',
  queue_opens:        8,
  queue_closes:       17,
  advance_days:       1,
  evolution_instance: '',
  evolution_api_url:  '',
  evolution_api_key:  '',
  is_active:          true,
}

function Field({ label, hint, error, children }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <label style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: error ? 'var(--red)' : 'var(--muted2)' }}>
        {label}
      </label>
      {children}
      {hint && !error && <span style={{ fontSize: 11, color: 'var(--muted)' }}>{hint}</span>}
      {error && <span style={{ fontSize: 11, color: 'var(--red)' }}>{error}</span>}
    </div>
  )
}

function TextInput({ value, onChange, placeholder, type = 'text', error }) {
  return (
    <input
      type={type}
      value={value}
      onChange={onChange}
      placeholder={placeholder}
      style={{
        background: 'var(--surface2)',
        border: `1px solid ${error ? 'var(--red)' : 'var(--border2)'}`,
        borderRadius: 8, padding: '10px 14px', fontSize: 13,
        color: 'var(--text)', outline: 'none', width: '100%',
        fontFamily: 'var(--sans)',
      }}
      onFocus={e => { if (!error) e.target.style.borderColor = 'var(--accent)' }}
      onBlur={e => { if (!error) e.target.style.borderColor = 'var(--border2)' }}
    />
  )
}

function NumberInput({ value, onChange, min, max }) {
  return (
    <input
      type="number"
      value={value}
      min={min}
      max={max}
      onChange={onChange}
      style={{
        background: 'var(--surface2)', border: '1px solid var(--border2)',
        borderRadius: 8, padding: '10px 14px', fontSize: 13,
        color: 'var(--text)', outline: 'none', width: '100%',
        fontFamily: 'var(--sans)',
      }}
      onFocus={e => e.target.style.borderColor = 'var(--accent)'}
      onBlur={e => e.target.style.borderColor = 'var(--border2)'}
    />
  )
}

function SectionHeader({ label, sub }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)' }}>{label}</div>
      {sub && <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 3 }}>{sub}</div>}
    </div>
  )
}

function Divider() {
  return <div style={{ height: 1, background: 'var(--border)', margin: '24px 0' }} />
}

export default function TenantForm({ tenants, reload }) {
  const { id }          = useParams()
  const isEdit          = Boolean(id)
  const navigate        = useNavigate()
  const { toast, show } = useToast()

  const [form, setForm]     = useState(EMPTY)
  const [errors, setErrors] = useState({})
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (isEdit) {
      const t = tenants.find(t => t.id === parseInt(id))
      if (t) setForm({ ...EMPTY, ...t })
    }
  }, [id, tenants])

  function setText(field) {
    return e => {
      setForm(f => ({ ...f, [field]: e.target.value }))
      if (errors[field]) setErrors(e => ({ ...e, [field]: null }))
    }
  }

  function setNum(field) {
    return e => {
      const val = parseInt(e.target.value)
      setForm(f => ({ ...f, [field]: isNaN(val) ? 0 : val }))
    }
  }

  function validate() {
    const e = {}
    if (!form.business_name.trim())      e.business_name      = 'Required'
    if (!form.whatsapp_number.trim())    e.whatsapp_number    = 'Required'
    if (!form.evolution_instance.trim()) e.evolution_instance = 'Required'
    if (!form.evolution_api_url.trim())  e.evolution_api_url  = 'Required'
    if (!form.evolution_api_key.trim())  e.evolution_api_key  = 'Required'
    if (form.queue_opens >= form.queue_closes) e.queue_closes = 'Must be later than opens'
    setErrors(e)
    return Object.keys(e).length === 0
  }

  async function submit() {
    if (!validate()) return
    setSaving(true)
    try {
      if (isEdit) {
        await api.updateTenant(id, form)
        show('Business updated.', 'success')
      } else {
        await api.createTenant(form)
        show('Business registered!', 'success')
      }
      await reload()
      navigate('/businesses')
    } catch (err) {
      show(err.message || 'Failed to save.', 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div style={{ maxWidth: 680 }}>
      <div style={{ marginBottom: 28 }}>
        <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.01em' }}>
          {isEdit ? 'Edit Business' : 'Add Business'}
        </h1>
        <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
          {isEdit ? 'Update this business configuration.' : 'Register a new business on QueueBot.'}
        </p>
      </div>

      <Card className="animate-fade-up" style={{ padding: 28 }}>

        {/* ── Business Info ── */}
        <SectionHeader label="Business Info" />
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          <Field label="Business Name" error={errors.business_name}>
            <TextInput value={form.business_name} onChange={setText('business_name')}
              placeholder="e.g. Porsche Hair Salon" error={errors.business_name} />
          </Field>
          <Field label="Business Type">
            <TextInput value={form.business_type} onChange={setText('business_type')}
              placeholder="e.g. Hair Salon, Clinic, Tyre Shop" />
          </Field>
          <Field label="WhatsApp Number" hint="Country code, no + e.g. 27813130871" error={errors.whatsapp_number}>
            <TextInput value={form.whatsapp_number} onChange={setText('whatsapp_number')}
              placeholder="27813130871" error={errors.whatsapp_number} />
          </Field>
          <Field label="Owner Number" hint="Receives new queue notifications (optional)">
            <TextInput value={form.owner_number} onChange={setText('owner_number')}
              placeholder="27813130871" />
          </Field>
        </div>

        <Divider />

        {/* ── Labels ── */}
        <SectionHeader label="Custom Labels" sub="Makes the bot speak the language of this business" />
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          <Field label="Agent Label" hint="Who serves the customer">
            <TextInput value={form.agent_label} onChange={setText('agent_label')}
              placeholder="Stylist / Doctor / Bay / Technician" />
          </Field>
          <Field label="Service Label" hint="What the customer is getting">
            <TextInput value={form.service_label} onChange={setText('service_label')}
              placeholder="Hair Service / Procedure / Job Type" />
          </Field>
        </div>

        <Divider />

        {/* ── Queue Config ── */}
        <SectionHeader label="Queue Config" />
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 16 }}>
          <Field label="Opens (24h)" hint="e.g. 8 = 08:00">
            <NumberInput value={form.queue_opens} onChange={setNum('queue_opens')} min={0} max={23} />
          </Field>
          <Field label="Closes (24h)" hint="e.g. 17 = 17:00" error={errors.queue_closes}>
            <NumberInput value={form.queue_closes} onChange={setNum('queue_closes')} min={1} max={24} />
          </Field>
          <Field label="Advance Days" hint="0 = today only, 1 = today + tomorrow">
            <NumberInput value={form.advance_days} onChange={setNum('advance_days')} min={0} max={14} />
          </Field>
        </div>

        <Divider />

        {/* ── Evolution API ── */}
        <SectionHeader label="Evolution API Config" />
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <Field label="Instance Name" error={errors.evolution_instance}>
              <TextInput value={form.evolution_instance} onChange={setText('evolution_instance')}
                placeholder="e.g. PorscheHairSalon" error={errors.evolution_instance} />
            </Field>
            <Field label="API URL" error={errors.evolution_api_url}>
              <TextInput value={form.evolution_api_url} onChange={setText('evolution_api_url')}
                placeholder="https://evo.yourdomain.com" error={errors.evolution_api_url} />
            </Field>
          </div>
          <Field label="API Key" error={errors.evolution_api_key}>
            <TextInput value={form.evolution_api_key} onChange={setText('evolution_api_key')}
              type="password" placeholder="Instance API key" error={errors.evolution_api_key} />
          </Field>
        </div>

        {/* ── Actions ── */}
        <div style={{ display: 'flex', gap: 10, marginTop: 28 }}>
          <Button onClick={submit} loading={saving}>
            {isEdit ? 'Save Changes' : 'Register Business'}
          </Button>
          <Button variant="ghost" onClick={() => navigate('/businesses')}>Cancel</Button>
        </div>
      </Card>

      <Toast toast={toast} />
    </div>
  )
}
