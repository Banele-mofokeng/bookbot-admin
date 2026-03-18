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
  getTenants:    ()         => request('/admin/tenants'),
  createTenant:  (data)     => request('/admin/tenants', { method: 'POST', body: JSON.stringify(data) }),
  updateTenant:  (id, data) => request(`/admin/tenants/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),

  // Services
  getServices:   (tenantId) => request(`/admin/services/${tenantId}`),
  createService: (data)     => request('/admin/services', { method: 'POST', body: JSON.stringify(data) }),
  updateService: (id, data) => request(`/admin/services/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),

  // Agents
  getAgents:     (tenantId) => request(`/admin/agents/${tenantId}`),
  createAgent:   (data)     => request('/admin/agents', { method: 'POST', body: JSON.stringify(data) }),
  updateAgent:   (id, data) => request(`/admin/agents/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),

  // Queue
  getQueue:      (tenantId, date) => request(`/admin/queue/${tenantId}${date ? `?queue_date=${date}` : ''}`),
  updateStatus:  (entryId, status) => request(`/admin/queue/${entryId}/status`, { method: 'PATCH', body: JSON.stringify({ status }) }),
  addWalkin:     (data)     => request('/admin/queue/walkin', { method: 'POST', body: JSON.stringify(data) }),
}
