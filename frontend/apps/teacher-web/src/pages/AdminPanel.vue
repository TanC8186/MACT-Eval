<template>
  <div class="page">
    <page-header title="管理后台" description="数据管理、用户导入、系统设置">
      <template #extra>
        <a-tag color="red">管理员</a-tag>
      </template>
    </page-header>

    <a-alert
      v-if="statsError || classesError"
      type="warning"
      show-icon
      message="部分管理数据加载失败"
      :description="[statsError, classesError].filter(Boolean).join('；')"
      style="margin-bottom:16px"
    >
      <template #action>
        <a-button size="small" @click="loadManagementData">重试</a-button>
      </template>
    </a-alert>

    <a-row :gutter="16">
      <a-col :span="8">
        <a-card title="系统概览">
          <a-statistic title="学生总数" :value="statsLoaded ? stats.studentCount : '--'" suffix="人" />
          <a-divider />
          <a-statistic title="班级数" :value="statsLoaded ? stats.classCount : '--'" />
          <a-divider />
          <a-statistic title="课程数" :value="statsLoaded ? stats.courseCount : '--'" />
          <a-divider />
          <a-statistic title="课堂数" :value="statsLoaded ? stats.lessonCount : '--'" />
        </a-card>
      </a-col>

      <a-col :span="16">
        <a-card title="批量导入学生">
          <a-form layout="vertical">
            <a-form-item label="班级">
              <a-select v-model:value="importClassId" placeholder="选择班级" style="width:300px" :options="classOptions" />
            </a-form-item>
            <a-form-item label="学生数据 (JSON)">
              <a-textarea v-model:value="importData" rows="8"
                placeholder='[{"studentId":"S001","name":"张三","seatRow":1,"seatCol":1}]' />
            </a-form-item>
            <div style="margin-bottom:12px;font-size:12px;color:#667085">
              预期 {{ importPreview }} 行；学号与姓名必填，座位号必须为非负整数。
            </div>
            <a-alert
              v-if="importErrors.length"
              type="error"
              show-icon
              message="导入数据校验未通过"
              style="margin-bottom:12px"
            >
              <template #description>
                <div v-for="(err, idx) in importErrors.slice(0, 8)" :key="idx">{{ err }}</div>
                <div v-if="importErrors.length > 8">其余 {{ importErrors.length - 8 }} 条错误…</div>
              </template>
            </a-alert>
            <a-button
              type="primary"
              @click="handleImport"
              :loading="importing"
              :disabled="!importClassId || !importData.trim() || importErrors.length > 0"
            >
              开始导入
            </a-button>
          </a-form>
          <a-alert v-if="importResult" :message="importResult" type="info" style="margin-top:12px" />
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import PageHeader from '@/components/PageHeader.vue'
import apiClient from '@/api/client'

const stats = ref({ studentCount: 0, classCount: 0, courseCount: 0, lessonCount: 0 })
const importClassId = ref('')
const importData = ref('')
const importing = ref(false)
const importResult = ref('')
const statsLoaded = ref(false)
const statsError = ref('')
const classesError = ref('')
const classes = ref<any[]>([])
const classOptions = computed(() => classes.value.map((c: any) => ({ label: c.name, value: c.id })))

const parsedStudents = computed<any[] | null>(() => {
  if (!importData.value.trim()) return null
  try {
    const value = JSON.parse(importData.value)
    return Array.isArray(value) ? value : null
  } catch {
    return null
  }
})
const importPreview = computed(() => parsedStudents.value?.length ?? 0)
const importErrors = computed<string[]>(() => {
  if (!importData.value.trim()) return []
  if (parsedStudents.value === null) return ['JSON 格式错误，或顶层不是数组']
  const errors: string[] = []
  const seen = new Set<string>()
  parsedStudents.value.forEach((row: any, index: number) => {
    const rowNo = index + 1
    if (!row || typeof row !== 'object' || Array.isArray(row)) {
      errors.push(`第 ${rowNo} 行：必须是对象`)
      return
    }
    const studentId = typeof row.studentId === 'string' ? row.studentId.trim() : ''
    const name = typeof row.name === 'string' ? row.name.trim() : ''
    if (!studentId) errors.push(`第 ${rowNo} 行：学号不能为空`)
    else if (studentId.length > 50) errors.push(`第 ${rowNo} 行：学号不能超过 50 字符`)
    else if (seen.has(studentId)) errors.push(`第 ${rowNo} 行：文件内学号重复 ${studentId}`)
    else seen.add(studentId)
    if (!name) errors.push(`第 ${rowNo} 行：姓名不能为空`)
    else if (name.length > 100) errors.push(`第 ${rowNo} 行：姓名不能超过 100 字符`)
    for (const field of ['seatRow', 'seatCol']) {
      const value = row[field]
      if (value !== undefined && value !== null
        && (!Number.isInteger(value) || value < 0 || value > 500)) {
        errors.push(`第 ${rowNo} 行：${field} 必须是 0-500 的整数`)
      }
    }
  })
  return errors
})

async function loadManagementData() {
  statsError.value = ''
  classesError.value = ''
  try {
    const res = await apiClient.get('/admin/stats')
    stats.value = res.data.data || {}
    statsLoaded.value = true
  } catch (err: any) {
    // 统计失败显示 “--”，不能伪装成真实的 0。
    statsLoaded.value = false
    statsError.value = err?.response?.data?.message || '系统统计加载失败'
  }
  try {
    const res = await apiClient.get('/classes')
    classes.value = res.data.data || []
  } catch (err: any) {
    classesError.value = err?.response?.data?.message || '班级列表加载失败'
  }
}

onMounted(loadManagementData)

async function handleImport() {
  if (importErrors.value.length || !parsedStudents.value) {
    message.error('请先修正导入数据中的错误')
    return
  }
  importing.value = true; importResult.value = ''
  try {
    const res = await apiClient.post('/admin/students/import', {
      classId: importClassId.value,
      students: parsedStudents.value,
    })
    const result = res.data.data
    importResult.value = '导入成功: ' + result.imported + ' 条, 跳过: ' + result.skipped + ' 条'
    if (result.errors?.length) importResult.value += ', 错误: ' + result.errors.join('; ')
    message.success('导入完成')
  } catch (err: any) { message.error(err?.response?.data?.message || '导入失败') }
  finally { importing.value = false }
}
</script>
