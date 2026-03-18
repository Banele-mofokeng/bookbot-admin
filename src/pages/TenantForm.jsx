import { useState, useEffect } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client.js'
import { Input, Button, Card, useToast, Toast } from '../components/UI.jsx'

const EMPTY = {
  business_name:      '',
  business_type:      'General',
  whatsapp_number:    '',
  owner_number:       '',
  evolution_instance: '',
  evolution_api_url:  '',
  evolution_api_key:  '',
  agent_label:        'Agent',
  service_label:      'Service',
  queue_opens:        8,
  queue_closes:       17,
  advance_days:       1,
  is_active:          true,
}

export default function TenantForm({ tenants, reload }) {
  const { id }      = useParams()
  const isEdit      = Boolean(id)
  const navigate    = useNavigate()
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

  function set(field, value) {
    setForm(f => ({ ...f, [field]: value }))
    if (errors[field]) setErrors(e => ({ ...e, [field]: null }))
  }

  function validate() {
    const e = {}
    if (!form.business_name.trim())      e.business_name      = 'Required'
    if (!form.whatsapp_number.trim())    e.whatsapp_number    = 'Required — e.g. 27813130871'
    if (!form.evolution_instance.trim()) e.evolution_instance = 'Required'
    if (!form.evolution_api_url.trim())  e.evolution_api_url  = 'Required'
    if (!form.evolution_api_key.trim())  e.evolution_api_key  = 'Required'
    if (form.queue_opens >= form.queue_closes) e.queue_closes = 'Must be after opening hour'
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
      reload()
      setTimeout(() => navigate('/businesses'), 800)
    } catch (err) {
      show(err.message || 'Failed to save.', 'error')
    } finally {
      setSaving(false)
    }
  }

  const field = (label, key, props = {}) => (
    <Input
      label={label}
      value={form[key]}
      onChange={e => set(key, e.target.type === 'number' ? parseInt(e.target.value) : e.target.value)}
      error={errors[key]}
      {...props}
    />
  )

  return (
    <div style={{ maxWidth: 700 }}>
      <div style={{ marginBottom: 28 }}>
        <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.01em' }}>
          {isEdit ? 'Edit Business' : 'Add Business'}
        </h1>
        <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
          {isEdit ? 'Update this business configuration.' : 'Register a new business on QueueBot.'}
        </p>
      </div>

      <Card className="animate-fade-up" style={{ padding: 28 }}>

        {/* Business Info */}
        <Section label="Business Info">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            {field('Business Name',    'business_name',   { placeholder: 'e.g. Porsche Hair Salon' })}
            {field('Business Type',    'business_type',   { placeholder: 'e.g. Hair Salon, Clinic, Tyre Shop' })}
            {field('WhatsApp Number',  'whatsapp_number', { placeholder: '27813130871' })}
            {field('Owner Number',     'owner_number',    { placeholder: 'For booking notifications (optional)' })}
          </div>
        </Section>

        <Divider />

        {/* Labels */}
        <Section label="Custom Labels" sub="Makes the bot speak the language of the business">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            {field('Agent Label',   'agent_label',   { placeholder: 'Stylist / Doctor / Bay / Technician' })}
            {field('Service Label', 'service_label', { placeholder: 'Hair Service / Procedure / Job Type' })}
          </div>
        </Section>

        <Divider />

        {/* Queue Config */}
        <Section label="Queue Config">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 16 }}>
            {field('Opens (24h)',       'queue_opens',   { type: 'number', min: 0, max: 23 })}
            {field('Closes (24h)',      'queue_closes',  { type: 'number', min: 1, max: 24 })}
            {field('Advance Days',      'advance_days',  { type: 'number', min: 0, max: 14,
              placeholder: '0 = today only' })}
          </div>
        </Section>

        <Divider />

        {/* Evolution API */}
        <Section label="Evolution API Config">
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
              {field('Instance Name', 'evolution_instance', { placeholder: 'PorscheHairSalon' })}
              {field('API URL',       'evolution_api_url',  { placeholder: 'https://evo.yourdomain.com' })}
            </div>
            {field('API Key', 'evolution_api_key', { type: 'password', placeholder: 'Instance API key' })}
          </div>
        </Section>

        {/* Actions */}
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

function Section({ label, sub, children }) {
  return (
    <div style={{ marginBottom: 4 }}>
      <div style={{ marginBottom: 14 }}>
        <div style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)' }}>{label}</div>
        {sub && <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 3 }}>{sub}</div>}
      </div>
      {children}
    </div>
  )
}

function Divider() {
  return <div style={{ height: 1, background: 'var(--border)', margin: '22px 0' }} />
}
