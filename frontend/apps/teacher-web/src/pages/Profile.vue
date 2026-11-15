<template>
  <div class="page">
    <page-header title="个人设置" description="管理个人资料与偏好设置" />
    <a-alert
      v-if="loadError"
      type="error"
      show-icon
      message="个人资料加载失败"
      :description="loadError"
      style="margin-bottom: 16px"
    />

    <a-row :gutter="24">
      <!-- 左侧：个人资料 -->
      <a-col :xs="24" :lg="16">
        <a-card title="个人资料">
          <a-form :model="form" layout="vertical">
            <a-row :gutter="16">
              <a-col :xs="24" :sm="12">
                <a-form-item label="姓名" required>
                  <a-input v-model:value="form.name" />
                </a-form-item>
              </a-col>
              <a-col :xs="24" :sm="12">
                <a-form-item label="登录账号/手机号" required>
                  <a-input v-model:value="form.phone" />
                </a-form-item>
              </a-col>
            </a-row>
            <a-button type="primary" @click="handleSave" :loading="saving">保存修改</a-button>
          </a-form>
        </a-card>

        <a-card title="修改密码" style="margin-top: 16px">
          <a-form ref="pwdFormRef" :model="pwdForm" :rules="pwdRules" layout="vertical">
            <a-row :gutter="16">
              <a-col :xs="24" :sm="8">
                <a-form-item label="当前密码" name="oldPassword">
                  <a-input-password v-model:value="pwdForm.oldPassword" placeholder="请输入当前密码" />
                </a-form-item>
              </a-col>
              <a-col :xs="24" :sm="8">
                <a-form-item label="新密码" name="newPassword">
                  <a-input-password v-model:value="pwdForm.newPassword" placeholder="请输入新密码" />
                </a-form-item>
              </a-col>
              <a-col :xs="24" :sm="8">
                <a-form-item label="确认新密码" name="confirmPassword">
                  <a-input-password v-model:value="pwdForm.confirmPassword" placeholder="请再次输入新密码" />
                </a-form-item>
              </a-col>
            </a-row>
            <a-button type="primary" :loading="savingPwd" @click="handleChangePwd">修改密码</a-button>
          </a-form>
        </a-card>
      </a-col>

      <!-- 右侧：账号信息 -->
      <a-col :xs="24" :lg="8">
        <a-card title="账号信息">
          <a-descriptions :column="1" size="small">
            <a-descriptions-item label="用户名">{{ user?.name || '--' }}</a-descriptions-item>
            <a-descriptions-item label="角色">
              <a-tag color="blue">{{ user?.role === 'teacher' ? '教师' : user?.role }}</a-tag>
            </a-descriptions-item>
            <a-descriptions-item label="用户ID">{{ user?.id || '--' }}</a-descriptions-item>
          </a-descriptions>
          <a-divider />
          <a-button danger @click="handleLogout">退出登录</a-button>
        </a-card>

        <a-card title="当前版本能力说明" style="margin-top: 16px">
          <a-descriptions :column="1" size="small">
            <a-descriptions-item label="界面语言">
              简体中文（当前版本未提供多语言切换，避免切换后刷新即恢复）
            </a-descriptions-item>
            <a-descriptions-item label="通知">
              任务状态在页面内展示（当前版本未提供通知开关）
            </a-descriptions-item>
          </a-descriptions>
          <p style="margin:8px 0 0;font-size:12px;color:#667085">
            未持久化或未产生实际效果的偏好项不会显示为可编辑控件。
          </p>
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import type { FormInstance } from 'ant-design-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAuthStore } from '@/stores/auth'
import { useRouter } from 'vue-router'
import { getProfile, updateProfile, changePassword } from '@/api/endpoints'

const auth = useAuthStore()
const router = useRouter()
const saving = ref(false)
const loadError = ref('')
const user = auth.user

const form = ref({
  name: user?.name || '',
  phone: '',
})

async function handleSave() {
  if (!form.value.name.trim() || !form.value.phone.trim()) {
    message.warning('请填写姓名和登录账号/手机号')
    return
  }

  saving.value = true
  try {
    await updateProfile({
      name: form.value.name.trim(),
      phone: form.value.phone.trim(),
    })
    await auth.fetchProfile()
    message.success('资料已更新')
  } catch (err: any) {
    message.error(err?.response?.data?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

const pwdForm = ref({
  oldPassword: '',
  newPassword: '',
  confirmPassword: '',
})
const pwdFormRef = ref<FormInstance>()
const savingPwd = ref(false)

const pwdRules = {
  oldPassword: [{ required: true, message: '请输入当前密码' }],
  newPassword: [
    { required: true, message: '请输入新密码' },
    { min: 6, message: '密码至少6位' },
  ],
  confirmPassword: [
    { required: true, message: '请确认新密码' },
    {
      validator: (_rule: any, value: string) => {
        if (value !== pwdForm.value.newPassword) {
          return Promise.reject('两次输入的密码不一致')
        }
        return Promise.resolve()
      },
    },
  ],
}

async function handleChangePwd() {
  try {
    await pwdFormRef.value?.validate()
  } catch {
    return
  }
  if (pwdForm.value.newPassword === pwdForm.value.oldPassword) {
    message.warning('新密码不能与当前密码相同')
    return
  }
  savingPwd.value = true
  try {
    const session = await changePassword({
      oldPassword: pwdForm.value.oldPassword,
      newPassword: pwdForm.value.newPassword,
    })
    auth.applySession(session)
    message.success('密码已修改')
    pwdForm.value.oldPassword = ''
    pwdForm.value.newPassword = ''
    pwdForm.value.confirmPassword = ''
  } catch (err: any) {
    message.error(err?.response?.data?.message || '密码修改失败')
  } finally {
    savingPwd.value = false
  }
}

function handleLogout() {
  auth.logout()
  router.push('/login')
}

onMounted(async () => {
  const profile = await getProfile()
  if (!profile) {
    loadError.value = '未能从后端读取账号资料，请检查服务状态后重试。'
    return
  }
  form.value.name = profile.name || ''
  form.value.phone = profile.phone || ''
})
</script>
