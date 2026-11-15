import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import {
  clearStoredSession,
  decodeJwtPayload,
  ensureAccessToken,
  getAccessToken,
  getRefreshToken,
  sanitizeRedirect,
} from '@/api/authSession'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    meta: { requiresAuth: false },
    component: () => import('@/pages/Login.vue'),
  },
  {
    path: '/register',
    name: 'Register',
    meta: { requiresAuth: false },
    component: () => import('@/pages/Register.vue'),
  },
  {
    path: '/',
    component: () => import('@/components/Layout.vue'),
    meta: { requiresAuth: true },
    redirect: '/dashboard',
    children: [
      {
        path: 'courses',
        name: 'Courses',
        meta: { roles: ['teacher', 'admin'] },
        redirect: to => ({ path: '/classroom', query: to.query, hash: to.hash }),
      },
      {
        path: 'dashboard',
        name: 'Dashboard',
        meta: { roles: ['teacher', 'admin'] },
        component: () => import('@/pages/Dashboard.vue'),
      },
      {
        path: 'evaluation',
        name: 'Evaluation',
        meta: { roles: ['teacher', 'admin'] },
        component: () => import('@/pages/Evaluation.vue'),
      },
      {
        path: 'classroom',
        name: 'Classroom',
        meta: { roles: ['teacher', 'admin'] },
        component: () => import('@/pages/Classroom.vue'),
      },
      {
        path: 'trends',
        name: 'Trends',
        meta: { roles: ['teacher', 'admin'] },
        redirect: '/dashboard',
      },
      {
        path: 'effects',
        name: 'Effects',
        meta: { roles: ['teacher', 'admin'] },
        redirect: '/dashboard',
      },
      {
        path: 'reports',
        name: 'Reports',
        meta: { roles: ['teacher', 'admin'] },
        component: () => import('@/pages/Reports.vue'),
      },
      {
        path: 'profile',
        name: 'Profile',
        component: () => import('@/pages/Profile.vue'),
      },
      {
        path: 'admin',
        name: 'AdminPanel',
        meta: { roles: ['admin'] },
        component: () => import('@/pages/AdminPanel.vue'),
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/pages/NotFound.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

function roleLanding(role: string | null) {
  if (role === 'admin') return '/admin'
  return '/dashboard'
}

function usableRoleFromToken(token: string | null): string | null {
  const payload = decodeJwtPayload(token)
  if (!payload || typeof payload.exp !== 'number' || Date.now() >= payload.exp * 1000) return null
  return typeof payload.role === 'string' ? payload.role : null
}

/**
 * 解析当前会话角色。access token 过期但 refresh token 仍有效时先尝试续期，
 * 不因为 access 过期就清空可续期会话——这正是深链接被提前踢出的根因。
 */
async function resolveRole(): Promise<string | null> {
  const role = usableRoleFromToken(getAccessToken())
  if (role) return role
  if (!getRefreshToken()) return null
  const refreshed = await ensureAccessToken()
  return usableRoleFromToken(refreshed)
}

router.beforeEach(async (to) => {
  const role = await resolveRole()

  // 教师端不接受学生令牌，避免两个客户端混用账号。
  if (role === 'student') {
    clearStoredSession()
    if (to.path !== '/login') {
      return { path: '/login', query: { redirect: to.fullPath } }
    }
    return true
  }

  if (to.meta.requiresAuth !== false && !role && to.path !== '/login') {
    // 不清除 refresh token；仅当续期确实失败时 ensureAccessToken 已清理会话。
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  if ((to.path === '/login' || to.path === '/register') && role) {
    const redirect = sanitizeRedirect(to.query.redirect)
    if (redirect && redirect !== '/login' && redirect !== '/register') return redirect
    return roleLanding(role)
  }

  const roles = to.meta.roles as string[] | undefined
  if (roles && (!role || !roles.includes(role))) return roleLanding(role)
})

export default router
