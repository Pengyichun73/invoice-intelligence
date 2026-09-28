import { UserManager, WebStorageStateStore } from 'oidc-client-ts'

const issuer = import.meta.env?.VITE_OIDC_AUTHORITY
const clientId = import.meta.env?.VITE_OIDC_CLIENT_ID || 'invoice-intelligence-console'
const configured = Boolean(issuer)
let currentUser = null
const manager = configured
  ? new UserManager({
      authority: issuer,
      client_id: clientId,
      redirect_uri: `${window.location.origin}/`,
      post_logout_redirect_uri: `${window.location.origin}/`,
      response_type: 'code',
      scope: 'openid profile',
      userStore: new WebStorageStateStore({ store: window.sessionStorage }),
      automaticSilentRenew: true,
      monitorSession: false,
    })
  : null

export function loginEnabled() { return configured }

export async function restoreSession() {
  if (!manager) return null
  if (new URLSearchParams(window.location.search).has('code')) {
    currentUser = await manager.signinRedirectCallback()
    return currentUser
  }
  currentUser = await manager.getUser()
  return currentUser
}

export async function signIn() {
  if (manager) await manager.signinRedirect()
}

export async function signOut() {
  if (manager) {
    currentUser = null
    await manager.signoutRedirect()
  }
}

export function accessToken() {
  if (!manager) return null
  return (async () => {
    let user = currentUser || await manager.getUser()
    if (user?.expired) user = await manager.signinSilent().catch(() => null)
    currentUser = user
    if (user && !user.expired) return user.access_token
    window.dispatchEvent(new Event('invoice:auth-expired'))
    return null
  })()
}
