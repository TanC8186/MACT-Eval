<template>
  <div class="register-page">
    <div class="register-card">
      <div class="register-header">
        <h1 class="register-title">注册教师账号</h1>
        <p class="register-subtitle">MACT-Eval 多模态课堂评价系统 · 教师端</p>
      </div>
      <a-form :model="formState" :rules="rules" @finish="handleRegister" layout="vertical">
        <a-form-item label="用户名/手机号" name="username">
          <a-input
            v-model:value="formState.username"
            placeholder="用户名/手机号"
            size="large"
            autocomplete="username"
            aria-label="用户名或手机号"
          >
            <template #prefix><UserOutlined aria-hidden="true" /></template>
          </a-input>
        </a-form-item>
        <a-form-item label="姓名" name="name">
          <a-input
            v-model:value="formState.name"
            placeholder="姓名"
            size="large"
            autocomplete="name"
            aria-label="姓名"
          >
            <template #prefix><IdcardOutlined aria-hidden="true" /></template>
          </a-input>
        </a-form-item>
        <a-form-item label="密码" name="password">
          <a-input-password
            v-model:value="formState.password"
            placeholder="密码（至少6位）"
            size="large"
            autocomplete="new-password"
            aria-label="密码"
          >
            <template #prefix><LockOutlined aria-hidden="true" /></template>
          </a-input-password>
        </a-form-item>
        <a-form-item label="确认密码" name="confirmPassword">
          <a-input-password
            v-model:value="formState.confirmPassword"
            placeholder="确认密码"
            size="large"
            autocomplete="new-password"
            aria-label="确认密码"
          >
            <template #prefix><LockOutlined aria-hidden="true" /></template>
          </a-input-password>
        </a-form-item>
        <a-form-item>
          <a-button type="primary" html-type="submit" block size="large" :loading="loading">
            注册并登录
          </a-button>
        </a-form-item>
        <div class="register-footer">
          已有账号？
          <router-link to="/login">返回登录</router-link>
        </div>
      </a-form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { UserOutlined, LockOutlined, IdcardOutlined } from '@ant-design/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { sanitizeRedirect } from '@/api/authSession'
import { message } from 'ant-design-vue'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const loading = ref(false)

const formState = reactive({
  username: '',
  name: '',
  password: '',
  confirmPassword: '',
})

const rules = {
  username: [{ required: true, message: '请输入用户名/手机号' }],
  name: [{ required: true, message: '请输入姓名' }],
  password: [
    { required: true, message: '请输入密码' },
    { min: 6, message: '密码至少6位' },
  ],
  confirmPassword: [
    { required: true, message: '请确认密码' },
    {
      validator: (_rule: any, value: string) => {
        if (value !== formState.password) {
          return Promise.reject('两次输入的密码不一致')
        }
        return Promise.resolve()
      },
    },
  ],
}

async function handleRegister() {
  loading.value = true
  try {
    await auth.register(formState.username, formState.name, formState.password)
    message.success('注册成功，已自动登录')
    const redirect = sanitizeRedirect(route.query.redirect)
    router.push(redirect && redirect !== '/login' && redirect !== '/register' ? redirect : '/dashboard')
  } catch (err: any) {
    message.error(err?.response?.data?.message || '注册失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.register-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #111827 0%, #1F2937 50%, #111827 100%);
  padding: 24px;
}
.register-card {
  width: 100%;
  max-width: 420px;
  background: #fff;
  border-radius: 12px;
  padding: 40px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
}
.register-header {
  text-align: center;
  margin-bottom: 32px;
}
.register-title {
  font-size: 24px;
  font-weight: 700;
  color: #315FE8;
  margin-bottom: 4px;
}
.register-subtitle {
  font-size: 14px;
  color: #667085;
}
.register-footer {
  text-align: center;
  font-size: 14px;
  color: #667085;
  margin-top: 8px;
}
</style>
