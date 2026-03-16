import { useState, useEffect } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client.js'
import { Input, Button, Card, useToast, Toast } from '../components/UI.jsx'

const EMPTY = {
  business_name: '', whatsapp_number: '', evolution_instance: '',
  evolution_api_url: '', evolution_api_key: '',
  working_hours_start: 9, working_hours_end: 17,
  service_name: '', is_active: true,
}

export default function TenantForm({ tenants, reload }) {
  const { id } = useParams()
  const isEdit = Boolean(id)
  const navigate = useNavigate()
  const { toast, show } = useToast()

  const [form, setForm] = useState(EMPTY)
  const [errors, setErrors] = useState({})
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (isEdit) {
      const t = tenants.find(t => t.id === parseInt(id))
      if (t) setForm(t)
    }
  }, [id, tenants])

  function set(field, value) {
    setForm(f => ({ ...f, [field]: value }))
    if (errors[field]) setErrors(e => ({ ...e, [field]: null }))
  }

  function validate() {
    const e = {}
    if (!form.business_name.trim())     e.business_name = 'Required'
    if (!form.whatsapp_number.trim())   e.whatsapp_number = 'Required — e.g. 27813130871'
    if (!form.evolution_instance.trim()) e.evolution_instance = 'Required'
    if (!form.evolution_api_url.trim()) e.evolution_api_url = 'Required'
    if (!form.evolution_api_key.trim()) e.evolution_api_key = 'Required'
    if (form.working_hours_start >= form.working_hours_end) e.working_hours_end = 'Must be after start hour'
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
        await api.createTenant({ ...form, service_name: form.service_name || 'Appointment' })
        show('Business registered!', 'success')
      }
      reload()
      setTimeout(() => navigate('/tenants'), 800)
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
    <div style={{ maxWidth: 680 }}>
      <div style={{ marginBottom: 28 }}>
        <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.01em' }}>
          {isEdit ? 'Edit Business' : 'Add Business'}
        </h1>
        <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
          {isEdit ? 'Update this tenant\'s configuration.' : 'Register a new business on the bot.'}
        </p>
      </div>

      <Card className="animate-fade-up" style={{ padding: 28 }}>
        {/* Section: Business Info */}
        <div style={{ marginBottom: 24 }}>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)', marginBottom: 16 }}>
            Business Info
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            {field('Business Name', 'business_name', { placeholder: 'e.g. Glow Hair Studio' })}
            {field('WhatsApp Number', 'whatsapp_number', { placeholder: '27813130871' })}
            {field('Service Name', 'service_name', { placeholder: 'e.g. Hair Appointment' })}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              {field('Opens (24h)', 'working_hours_start', { type: 'number', min: 0, max: 23 })}
              {field('Closes (24h)', 'working_hours_end', { type: 'number', min: 1, max: 24 })}
            </div>
          </div>
        </div>

        <div style={{ height: 1, background: 'var(--border)', margin: '4px 0 24px' }} />

        {/* Section: Evolution API */}
        <div style={{ marginBottom: 28 }}>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)', marginBottom: 16 }}>
            Evolution API Config
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
              {field('Instance Name', 'evolution_instance', { placeholder: 'GlowHairStudio' })}
              {field('API URL', 'evolution_api_url', { placeholder: 'https://evo.yourdomain.com' })}
            </div>
            {field('API Key', 'evolution_api_key', { placeholder: 'Instance API key', type: 'password' })}
          </div>
        </div>

        {/* Actions */}
        <div style={{ display: 'flex', gap: 10 }}>
          <Button onClick={submit} loading={saving}>
            {isEdit ? 'Save Changes' : 'Register Business'}
          </Button>
          <Button variant="ghost" onClick={() => navigate('/tenants')}>Cancel</Button>
        </div>
      </Card>

      <Toast toast={toast} />
    </div>
  )
}
