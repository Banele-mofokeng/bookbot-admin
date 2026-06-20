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

  // Queue
  getQueue:      (tenantId, date) => request(`/admin/queue/${tenantId}${date ? `?queue_date=${date}` : ''}`),
  updateStatus:  (entryId, status) => request(`/admin/queue/${entryId}/status`, { method: 'PATCH', body: JSON.stringify({ status }) }),
  addWalkin:     (data)     => request('/admin/queue/walkin', { method: 'POST', body: JSON.stringify(data) }),

  // Users (super-admin only) — provision client logins
  getUsers:      ()         => request('/admin/users'),
  createUser:    (data)     => request('/admin/users', { method: 'POST', body: JSON.stringify(data) }),
  updateUser:    (id, data) => request(`/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
}
