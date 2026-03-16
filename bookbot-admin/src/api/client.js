const BASE = import.meta.env.VITE_API_URL || ''

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const err = await res.text()
    throw new Error(err || `HTTP ${res.status}`)
  }
  return res.json()
}

export const api = {
  // Tenants
  getTenants: () => request('/admin/tenants'),
  createTenant: (data) => request('/admin/tenants', { method: 'POST', body: JSON.stringify(data) }),
  updateTenant: (id, data) => request(`/admin/tenants/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),

  // Appointments
  getAppointments: (tenantId) => request(`/admin/appointments/${tenantId}`),
  cancelAppointment: (id) => request(`/admin/appointments/${id}/cancel`, { method: 'PATCH' }),
}
