const BASE = import.meta.env.VITE_API_URL || ''

// JWT from /auth/login, kept in localStorage (never baked into the bundle) and
// sent as a Bearer token on every request.
const TOKEN_KEY = 'auth_token'
export const getToken   = () => localStorage.getItem(TOKEN_KEY) || ''
export const setToken   = (t) => localStorage.setItem(TOKEN_KEY, t)
export const clearToken = () => localStorage.removeItem(TOKEN_KEY)

// Subscribers (e.g. App) get notified to drop to the login screen on 401.
let onUnauthorized = () => {}
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn }

async function request(path, options = {}) {
  const token = getToken()
  const res = await fetch(`${BASE}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
    ...options,
  })
  if (res.status === 401) {
    clearToken()
    onUnauthorized()
    throw new Error('Unauthorized — please sign in again.')
  }
  if (!res.ok) {
    const err = await res.text()
    throw new Error(err || `HTTP ${res.status}`)
  }
  return res.json()
}

// ── Auth ───────────────────────────────────────────────────────────────────
export async function login(email, password) {
  const data = await request('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
  setToken(data.access_token)
  return data.user
}

export const getMe = () => request('/auth/me')

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

  // Agent working hours — recurring weekly windows, plus one-off blocks that
  // override them. An agent with no windows falls back to the tenant's hours.
  getSchedule:   (agentId)  => request(`/admin/agents/${agentId}/schedule`),
  setSchedule:   (agentId, windows) => request(`/admin/agents/${agentId}/schedule`, { method: 'PUT', body: JSON.stringify({ windows }) }),
  getBlocks:     (agentId, fromDate) => request(`/admin/agents/${agentId}/blocks${fromDate ? `?from_date=${fromDate}` : ''}`),
  createBlock:   (agentId, data) => request(`/admin/agents/${agentId}/blocks`, { method: 'POST', body: JSON.stringify(data) }),
  deleteBlock:   (blockId)  => request(`/admin/blocks/${blockId}`, { method: 'DELETE' }),
  getWindows:    (agentId, date) => request(`/admin/agents/${agentId}/windows${date ? `?queue_date=${date}` : ''}`),

  // Reports
  getAnalytics:  (tenantId, fromDate, toDate) => request(
    `/admin/analytics/${tenantId}` + (fromDate || toDate
      ? `?${[fromDate && `from_date=${fromDate}`, toDate && `to_date=${toDate}`].filter(Boolean).join('&')}`
      : '')),

  // Queue
  getQueue:      (tenantId, date) => request(`/admin/queue/${tenantId}${date ? `?queue_date=${date}` : ''}`),
  updateStatus:  (entryId, status) => request(`/admin/queue/${entryId}/status`, { method: 'PATCH', body: JSON.stringify({ status }) }),
  addWalkin:     (data)     => request('/admin/queue/walkin', { method: 'POST', body: JSON.stringify(data) }),

  // The same day as a calendar: working hours per agent, with the work already
  // placed inside them. Answers "where are the gaps?", which the queue list
  // cannot — a free hour is the absence of a row.
  getTimeline:   (tenantId, date) => request(`/admin/timeline/${tenantId}${date ? `?queue_date=${date}` : ''}`),

  // Menu (ordering tenants) — prices are in cents everywhere
  getMenu:        (tenantId) => request(`/admin/menu/${tenantId}`),
  createMenuItem: (data)     => request('/admin/menu', { method: 'POST', body: JSON.stringify(data) }),
  updateMenuItem: (id, data) => request(`/admin/menu/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteMenuItem: (id)       => request(`/admin/menu/${id}`, { method: 'DELETE' }),

  // Orders — the kitchen board
  getOrders:         (tenantId, date) => request(`/admin/orders/${tenantId}${date ? `?order_date=${date}` : ''}`),
  updateOrderStatus: (orderId, status) => request(`/admin/orders/${orderId}/status`, { method: 'PATCH', body: JSON.stringify({ status }) }),
  addCounterOrder:   (data)  => request('/admin/orders', { method: 'POST', body: JSON.stringify(data) }),

  // Users (super-admin only) — provision client logins
  getUsers:      ()         => request('/admin/users'),
  createUser:    (data)     => request('/admin/users', { method: 'POST', body: JSON.stringify(data) }),
  updateUser:    (id, data) => request(`/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
}
