<template>
  <div class="login-page">
    <div class="login-shell">
      <!-- Brand panel -->
      <div class="brand-panel">
        <div class="brand-inner">
          <div class="login-brand"><span class="brand-mark">M</span><span>MACT <b>Eval</b></span></div>
          <div class="brand-message"><span class="brand-kicker">课堂教学分析平台</span><h1>看见课堂，<br />理解教学。</h1><p>连接课堂记录、行为画面与教学反馈，<br />让每一次教学反思都有迹可循。</p></div>
          <div class="login-workflow"><div><span>01</span><strong>记录课堂</strong><p>上传视频与音频素材</p></div><div><span>02</span><strong>复核证据</strong><p>结合画面核对行为识别</p></div><div><span>03</span><strong>回顾教学</strong><p>查看分析报告与改进建议</p></div></div>
          <div class="brand-footnote">多模态课堂教学质量评价系统</div>
        </div>
      </div>

      <!-- Form -->
      <div class="form-panel">
        <div class="form-inner">
          <div class="form-eyebrow">TEACHER PORTAL</div>
          <h2 class="form-title">欢迎回到教学工作台</h2>
          <p class="form-subtitle">登录教师账号，继续你的课堂分析。</p>
          <a-form :model="formState" :rules="rules" @finish="handleLogin" layout="vertical">
            <a-form-item label="用户名" name="username">
              <a-input
                v-model:value="formState.username"
                placeholder="请输入用户名"
                size="large"
                autocomplete="username"
                aria-label="用户名"
              >
                <template #prefix><UserOutlined class="input-icon" aria-hidden="true" /></template>
              </a-input>
            </a-form-item>
            <a-form-item label="密码" name="password">
              <a-input-password
                v-model:value="formState.password"
                placeholder="请输入密码"
                size="large"
                autocomplete="current-password"
                aria-label="密码"
              >
                <template #prefix><LockOutlined class="input-icon" aria-hidden="true" /></template>
              </a-input-password>
            </a-form-item>
            <a-button type="primary" html-type="submit" block size="large" class="login-btn" :loading="loading">
              登录工作台
            </a-button>
          </a-form>
          <p class="register-tip">
            还没有账号？<router-link to="/register">注册教师账号</router-link>
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { UserOutlined, LockOutlined } from '@ant-design/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { sanitizeRedirect } from '@/api/authSession'
import { message } from 'ant-design-vue'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const loading = ref(false)

const formState = reactive({ username: '', password: '' })
const rules = {
  username: [{ required: true, message: '请输入用户名' }],
  password: [{ required: true, message: '请输入密码' }],
}

async function handleLogin() {
  loading.value = true
  try {
    await auth.login(formState.username, formState.password)
    const role = auth.user?.role
    if (role === 'student') {
      auth.logout()
      message.error('当前版本仅提供教师/管理员端')
      return
    }
    message.success('登录成功')
    if (auth.user?.mustChangePassword) {
      message.warning('首次登录请先修改初始密码')
      router.push('/profile')
      return
    }
    const redirect = sanitizeRedirect(route.query.redirect)
    const landing = role === 'admin' ? '/admin' : '/dashboard'
    router.push(redirect && redirect !== '/login' && redirect !== '/register' ? redirect : landing)
  } catch (err: any) {
    message.error(err?.response?.data?.message || '用户名或密码错误')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page { min-height: 100vh; display: grid; place-items: center; padding: 40px; background: #f4f7fb; }
.login-shell { display: grid; grid-template-columns: 1.1fr 1fr; width: 100%; max-width: 1080px; min-height: 660px; background: #fff; border: 1px solid #e0e6ef; border-radius: 16px; overflow: hidden; box-shadow: 0 18px 65px rgba(31,50,85,.06); }
.brand-panel { background: #edf2fb; padding: 44px 48px 32px; border-right: 1px solid #e0e7f3; display: flex; }
.brand-inner { display: flex; flex-direction: column; width: 100%; }
.login-brand { display: flex; align-items: center; gap: 11px; color: #293b5b; font-size: 20px; font-weight: 650; letter-spacing: -.6px; }
.login-brand b { font-weight: 400; }
.brand-mark { width: 34px; height: 36px; display: grid; place-items: center; background: var(--color-primary); color: #fff; border-radius: 10px 10px 4px 10px; font-size: 19px; font-weight: 700; }
.brand-message { margin-top: 65px; }
.brand-kicker { font-size: 12px; color: #657c9f; letter-spacing: 2px; }
.brand-message h1 { font-size: 39px; font-weight: 600; color: #2d4268; line-height: 1.55; margin: 13px 0 16px; letter-spacing: -1px; }
.brand-message > p { color: #7586a1; font-size: 12px; line-height: 2; margin: 0; }
.login-workflow { margin-top: 42px; display: flex; gap: 22px; }
.login-workflow > div { flex: 1; min-width: 0; }
.login-workflow span { display: block; color: #8da1c3; font-family: var(--font-numeric); font-size: 12px; margin-bottom: 9px; }
.login-workflow strong { font-size: 12px; font-weight: 550; color: #445c80; }
.login-workflow p { font-size: 12px; color: #7d8fa9; line-height: 1.7; margin: 5px 0 0; }
.brand-footnote { margin-top: auto; padding-top: 45px; font-size: 12px; color: #8999b1; }
.form-panel { display: flex; align-items: center; justify-content: center; padding: 48px; }
.form-inner { width: 100%; max-width: 340px; }
.form-eyebrow { font-size: 12px; color: var(--color-primary); letter-spacing: 2px; font-weight: 550; margin-bottom: 14px; }
.form-title { font-size: 23px; font-weight: 650; color: var(--color-ink); margin: 0 0 10px; letter-spacing: -.5px; }
.form-subtitle { font-size: 12px; color: var(--color-text-secondary); margin: 0 0 34px; line-height: 1.8; }
.input-icon { color: #99a6ba; margin-right: 4px; }
.form-panel :deep(.ant-input-affix-wrapper) { height: 44px; border-radius: 7px; font-size: 13px; }
.form-panel :deep(.ant-form-item-label > label) { font-size: 12px; font-weight: 500; }
.login-btn { height: 44px; margin-top: 9px; font-size: 13px; border-radius: 7px; }
.register-tip { font-size: 12px; color: var(--color-text-secondary); text-align: center; margin-top: 26px; }
.register-tip a { margin-left: 5px; }
@media (max-width: 800px) { .login-page { padding: 24px; } .login-shell { grid-template-columns: 1fr; max-width: 460px; min-height: auto; } .brand-panel { padding: 25px 28px; border-right: 0; border-bottom: 1px solid #e0e7f3; } .brand-message { margin-top: 25px; } .brand-message h1 { font-size: 28px; line-height: 1.4; margin: 12px 0 4px; } .brand-message h1 br { display: none; } .brand-message > p, .brand-footnote, .login-workflow { display: none; } .form-panel { padding: 34px 28px; } .form-title { font-size: 21px; } }
</style>
