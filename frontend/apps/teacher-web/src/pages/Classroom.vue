<template>
  <div class="page">
    <PageHeader title="课程与课堂" description="选择课程，安排课堂，进入分析。教学记录在这里统一管理。" eyebrow="TEACHING">
      <template #extra>
        <a-button type="primary" @click="openCourseModal()"><PlusOutlined aria-hidden="true" /> 新建课程</a-button>
      </template>
    </PageHeader>

    <div class="teaching-layout">
      <a-card class="course-catalog" title="我的课程">
        <template #extra><span class="count-label">{{ courses.length }} 门</span></template>
        <a-input-search v-model:value="courseSearch" placeholder="搜索课程名称或代码" aria-label="搜索课程" allow-clear />
        <a-alert v-if="coursesError" type="error" show-icon message="课程列表加载失败" :description="coursesError" class="load-alert">
          <template #action><a-button size="small" @click="loadPage">重试</a-button></template>
        </a-alert>
        <a-skeleton v-else-if="coursesLoading" active :paragraph="{ rows: 6 }" class="course-skeleton" />
        <template v-else-if="filteredCourses.length">
          <div class="course-list" aria-label="课程列表">
            <button v-for="course in pagedCourses" :key="course.id" type="button" class="course-item"
              :class="{ selected: course.id === selectedCourseId }" :aria-pressed="course.id === selectedCourseId"
              @click="selectCourse(course.id)">
              <span class="course-icon" aria-hidden="true"><BookOutlined /></span>
              <span class="course-item-copy"><strong>{{ course.name }}</strong><small>{{ course.code || '未设置课程代码' }}</small></span>
              <RightOutlined class="course-arrow" aria-hidden="true" />
            </button>
          </div>
          <a-pagination v-if="filteredCourses.length > coursePageSize" v-model:current="coursePage" :pageSize="coursePageSize"
            :total="filteredCourses.length" :showSizeChanger="false" simple class="course-pagination" />
        </template>
        <a-empty v-else :description="courseSearch.trim() ? '没有匹配的课程' : '暂无课程，点击右上方新建课程。'" class="course-empty" />
      </a-card>

      <section class="classroom-panel" aria-label="课程课堂">
        <a-card v-if="selectedCourse && !coursesError" class="session-card">
          <div class="course-heading">
            <div class="course-details">
              <span class="section-kicker">当前课程</span>
              <h2>{{ selectedCourse.name }}</h2>
              <div v-if="selectedCourse.code || selectedCourse.department" class="course-meta">
                <span v-if="selectedCourse.code">{{ selectedCourse.code }}</span>
                <span v-if="selectedCourse.department">{{ selectedCourse.department }}</span>
              </div>
              <p v-if="selectedCourse.description" class="course-description">{{ selectedCourse.description }}</p>
            </div>
            <a-space class="course-actions" wrap>
              <a-button size="small" @click="openCourseModal(selectedCourse)">编辑课程</a-button>
              <a-popconfirm title="确定删除这门课程？" description="已有课堂的课程无法删除。" @confirm="deleteCourse(selectedCourse.id)">
                <a-button size="small" type="text" danger :loading="deletingCourse">删除课程</a-button>
              </a-popconfirm>
            </a-space>
          </div>
          <div class="session-heading">
            <div><h3>课堂列表 <span>{{ sessionPagination.total }} 节</span></h3><p>选择一节课堂，上传素材并查看分析结果。</p></div>
            <a-button type="primary" @click="openSessionModal"><PlusOutlined aria-hidden="true" /> 新建课堂</a-button>
          </div>
          <a-alert v-if="error" type="error" show-icon message="课堂数据加载失败" :description="error" class="load-alert">
            <template #action><a-button size="small" @click="store.fetchSessions(1)">重试</a-button></template>
          </a-alert>
          <p v-if="!error" class="table-scroll-note">左右滑动表格，查看完整信息。</p>
          <a-table v-if="!error" :dataSource="sessions" :columns="columns" :loading="loading" rowKey="id" :scroll="{ x: 720 }"
            :pagination="sessionPagination" @change="handleTableChange">
            <template #emptyText><a-empty description="这门课程还没有课堂，点击“新建课堂”开始安排。" /></template>
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'status'"><a-tag :color="statusColor(record.status)">{{ statusLabel(record.status) }}</a-tag></template>
              <template v-if="column.key === 'date'"><span class="session-date">{{ record.date?.slice(0, 10) || '—' }}<small v-if="record.date?.includes('T')">{{ record.date.slice(11, 16) }}</small></span></template>
              <template v-if="column.key === 'action'">
                <a-space>
                  <a @click="router.push(`/evaluation?sessionId=${record.id}`)">课堂分析</a>
                  <a-divider v-if="record.status === 'SCHEDULED' || record.status === 'IN_PROGRESS'" type="vertical" />
                  <a v-if="record.status === 'SCHEDULED'" @click="updateStatus(record.id, 'IN_PROGRESS')">开始</a>
                  <a v-if="record.status === 'IN_PROGRESS'" @click="updateStatus(record.id, 'COMPLETED')">结束</a>
                </a-space>
              </template>
            </template>
          </a-table>
        </a-card>
        <a-card v-else class="no-course-card">
          <a-skeleton v-if="coursesLoading" active />
          <a-empty v-else :description="coursesError ? '加载课程后可查看课堂安排。' : '先新建一门课程，再安排课堂。'" />
        </a-card>
      </section>
    </div>

    <a-modal v-model:open="showCourseModal" :title="editingCourse ? '编辑课程' : '新建课程'" @ok="saveCourse"
      :confirmLoading="savingCourse" :maskClosable="!savingCourse" :closable="!savingCourse" :cancelButtonProps="{ disabled: savingCourse }" :keyboard="!savingCourse">
      <a-form ref="courseFormRef" :model="courseForm" :rules="courseRules" layout="vertical">
        <a-form-item label="课程名称" name="name"><a-input v-model:value="courseForm.name" placeholder="如：教育测量与评价" :maxlength="200" show-count /></a-form-item>
        <a-form-item label="课程代码" name="code"><a-input v-model:value="courseForm.code" placeholder="如：EDU401" :maxlength="50" /></a-form-item>
        <a-form-item label="院系" name="department"><a-input v-model:value="courseForm.department" placeholder="如：教育技术系" :maxlength="200" /></a-form-item>
        <a-form-item label="课程描述" name="description"><a-textarea v-model:value="courseForm.description" :rows="3" :maxlength="2000" show-count /></a-form-item>
      </a-form>
    </a-modal>

    <a-modal v-model:open="showSessionModal" title="新建课堂" @ok="createSession" :confirmLoading="saving"
      :maskClosable="!saving" :closable="!saving" :cancelButtonProps="{ disabled: saving }" :keyboard="!saving">
      <p class="modal-course">所属课程：<strong>{{ selectedCourse?.name }}</strong></p>
      <a-alert v-if="classesError" type="error" show-icon :message="classesError" class="load-alert">
        <template #action><a-button size="small" @click="store.fetchClasses">重试</a-button></template>
      </a-alert>
      <a-form ref="sessionFormRef" :model="sessionForm" :rules="sessionRules" layout="vertical">
        <a-form-item label="课堂标题" name="title"><a-input v-model:value="sessionForm.title" placeholder="如：第三章 教育评价方法" :maxlength="200" show-count /></a-form-item>
        <a-form-item label="班级" name="classId"><a-select v-model:value="sessionForm.classId" :options="classOptions" placeholder="选择班级" :disabled="!!classesError" /></a-form-item>
        <a-form-item label="教学目标" name="teachingObjectives"><a-textarea v-model:value="sessionForm.teachingObjectives" :rows="3" placeholder="本课教学目标" :maxlength="2000" show-count /></a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { message } from 'ant-design-vue'
import type { FormInstance } from 'ant-design-vue'
import { BookOutlined, PlusOutlined, RightOutlined } from '@ant-design/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import apiClient from '@/api/client'
import type { ApiResponse, CourseDTO } from '@/api/endpoints'
import { useClassroomStore } from '@/stores/classroom'

const router = useRouter()
const route = useRoute()
const store = useClassroomStore()
const { courses, coursesLoading, coursesError, classesError, sessions, loading, saving, error,
  sessionPagination, classOptions, selectedCourseId } = storeToRefs(store)
const selectedCourse = computed(() => courses.value.find(course => course.id === selectedCourseId.value))

const courseSearch = ref('')
const coursePage = ref(1)
const coursePageSize = 6
const filteredCourses = computed(() => {
  const query = courseSearch.value.trim().toLocaleLowerCase()
  return courses.value.filter(course => `${course.name} ${course.code || ''}`.toLocaleLowerCase().includes(query))
})
const pagedCourses = computed(() => filteredCourses.value.slice((coursePage.value - 1) * coursePageSize, coursePage.value * coursePageSize))
watch(courseSearch, () => { coursePage.value = 1 })
watch(() => filteredCourses.value.length, total => {
  coursePage.value = Math.min(coursePage.value, Math.max(1, Math.ceil(total / coursePageSize)))
})

const showCourseModal = ref(false)
const editingCourse = ref<CourseDTO | null>(null)
const savingCourse = ref(false)
const deletingCourse = ref(false)
const emptyCourseForm = () => ({ name: '', code: '', department: '', description: '' })
const courseForm = ref(emptyCourseForm())
const courseFormRef = ref<FormInstance>()
const courseRules = {
  name: [{ required: true, whitespace: true, message: '请输入课程名称' }, { max: 200, message: '课程名称不能超过 200 个字符' }],
  code: [{ max: 50, message: '课程代码不能超过 50 个字符' }],
  department: [{ max: 200, message: '院系不能超过 200 个字符' }],
  description: [{ max: 2000, message: '课程描述不能超过 2000 个字符' }],
}

async function openCourseModal(course?: CourseDTO) {
  editingCourse.value = course ?? null
  courseForm.value = course ? { name: course.name, code: course.code || '', department: course.department || '', description: course.description || '' } : emptyCourseForm()
  showCourseModal.value = true
  await nextTick()
  courseFormRef.value?.clearValidate()
}

async function saveCourse() {
  try { await courseFormRef.value?.validate() } catch { return }
  savingCourse.value = true
  try {
    const payload = { ...courseForm.value, name: courseForm.value.name.trim(), code: courseForm.value.code.trim(), department: courseForm.value.department.trim() }
    const editing = editingCourse.value
    const response = editing
      ? await apiClient.put<ApiResponse<CourseDTO>>(`/courses/${editing.id}`, payload)
      : await apiClient.post<ApiResponse<CourseDTO>>('/courses', payload)
    message.success(editing ? '课程已更新' : '课程已创建')
    showCourseModal.value = false
    courseSearch.value = ''
    await store.fetchCourses()
    const savedId = response.data.data.id
    if (courses.value.some(course => course.id === savedId)) {
      coursePage.value = Math.floor(courses.value.findIndex(course => course.id === savedId) / coursePageSize) + 1
      await selectCourse(savedId)
    }
  } catch (err: any) {
    message.error(err?.response?.data?.message || '保存课程失败')
  } finally { savingCourse.value = false }
}

async function deleteCourse(id: string) {
  deletingCourse.value = true
  try {
    await apiClient.delete(`/courses/${id}`)
    message.success('课程已删除')
    await loadPage()
  } catch (err: any) {
    message.error(err?.response?.data?.message || '删除课程失败')
  } finally { deletingCourse.value = false }
}

async function selectCourse(id: string | null) {
  store.setSelectedCourseId(id)
  showSessionModal.value = false
  await router.replace({ path: '/classroom', query: { ...route.query, courseId: id || undefined } })
  await store.fetchSessions(1)
}

const showSessionModal = ref(false)
const sessionForm = ref({ title: '', classId: undefined as string | undefined, teachingObjectives: '' })
const sessionFormRef = ref<FormInstance>()
const sessionRules = {
  title: [{ required: true, whitespace: true, message: '请输入课堂标题' }, { max: 200, message: '课堂标题不能超过 200 个字符' }],
  classId: [{ required: true, message: '请选择班级' }],
  teachingObjectives: [{ max: 2000, message: '教学目标不能超过 2000 个字符' }],
}

async function openSessionModal() {
  sessionForm.value = { title: '', classId: undefined, teachingObjectives: '' }
  showSessionModal.value = true
  await nextTick()
  sessionFormRef.value?.clearValidate()
}

async function createSession() {
  try { await sessionFormRef.value?.validate() } catch { return }
  if (classesError.value || !sessionForm.value.classId) return
  try {
    await store.createSession({ ...sessionForm.value, title: sessionForm.value.title.trim(), classId: sessionForm.value.classId })
    message.success('课堂已创建')
    showSessionModal.value = false
  } catch (err: any) { message.error(err?.response?.data?.message || err?.message || '创建课堂失败') }
}

const columns = [
  { title: '课堂标题', dataIndex: 'title', key: 'title', width: 220 },
  { title: '班级', dataIndex: 'className', key: 'className', width: 140 },
  { title: '日期', dataIndex: 'date', key: 'date', width: 120 },
  { title: '状态', key: 'status', width: 100 },
  { title: '操作', key: 'action', width: 140 },
]
function statusColor(status: string) {
  return ({ SCHEDULED: 'default', IN_PROGRESS: 'processing', COMPLETED: 'success', CANCELLED: 'error' } as Record<string, string>)[status] || 'default'
}
function statusLabel(status: string) {
  return ({ SCHEDULED: '待开始', IN_PROGRESS: '进行中', COMPLETED: '已结束', CANCELLED: '已取消' } as Record<string, string>)[status] || status
}
function handleTableChange(pagination: { current: number; pageSize: number }) {
  sessionPagination.value = { ...sessionPagination.value, current: pagination.current, pageSize: pagination.pageSize }
  void store.fetchSessions(pagination.current)
}
async function updateStatus(id: string, status: string) {
  try { await store.updateSessionStatus(id, status); message.success('状态已更新') }
  catch (err: any) { message.error(err?.response?.data?.message || err?.message || '操作失败') }
}

let initialized = false
watch(() => route.query.courseId, courseId => {
  if (!initialized || route.path !== '/classroom' || courseId === selectedCourseId.value) return
  const id = typeof courseId === 'string' && courses.value.some(course => course.id === courseId)
    ? courseId : courses.value[0]?.id ?? null
  void selectCourse(id)
})

async function loadPage() {
  initialized = false
  await store.fetchCourses()
  if (coursesError.value) { initialized = true; return }
  const query = route.query.courseId
  const requestedId = typeof query === 'string' ? query : selectedCourseId.value
  const id = courses.value.some(course => course.id === requestedId) ? requestedId : courses.value[0]?.id ?? null
  const index = courses.value.findIndex(course => course.id === id)
  coursePage.value = Math.max(1, Math.floor(index / coursePageSize) + 1)
  await selectCourse(id)
  initialized = true
}
onMounted(() => { void loadPage(); void store.fetchClasses() })
</script>

<style scoped>
.teaching-layout { display: grid; grid-template-columns: 270px minmax(0, 1fr); gap: 20px; align-items: start; }
.teaching-layout > * { min-width: 0; }
.count-label { color: var(--color-text-secondary); font-size: 12px; }
.course-catalog :deep(.ant-card-body) { padding: 18px 14px; }
.course-list { display: grid; gap: 7px; margin-top: 16px; }
.course-item { display: flex; align-items: center; gap: 10px; text-align: left; width: 100%; padding: 13px 10px; border: 1px solid transparent; border-radius: 8px; background: transparent; cursor: pointer; color: var(--color-ink); }
.course-item:hover { background: var(--color-surface-soft); }
.course-item.selected { background: #edf2fc; border-color: #dce4f7; }
.course-item:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 2px; }
.course-icon { display: grid; place-items: center; background: #f1f4f9; color: #7589aa; width: 32px; height: 36px; border-radius: 7px; flex: none; }
.selected .course-icon { color: var(--color-primary); background: #fff; }
.course-item-copy { min-width: 0; flex: 1; }
.course-item-copy strong { display: block; font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.course-item-copy small { display: block; font-size: 11px; color: var(--color-text-secondary); margin-top: 5px; overflow-wrap: anywhere; }
.course-arrow { color: #a4b1c7; font-size: 10px; flex: none; }
.course-pagination { margin-top: 18px; text-align: center; }
.course-empty, .course-skeleton { margin-top: 24px; }
.course-heading { display: flex; gap: 18px; align-items: flex-start; justify-content: space-between; padding-bottom: 22px; border-bottom: 1px solid var(--color-divider); }
.course-details { min-width: 0; }
.section-kicker { font-size: 11px; color: var(--color-text-secondary); }
.course-details h2 { font-size: 21px; font-weight: 650; margin: 7px 0 9px; overflow-wrap: anywhere; }
.course-meta { display: flex; flex-wrap: wrap; gap: 6px 14px; color: var(--color-text-secondary); font-size: 12px; overflow-wrap: anywhere; }
.course-description { font-size: 12px; line-height: 1.8; margin: 12px 0 0; color: var(--color-text-secondary); white-space: pre-wrap; overflow-wrap: anywhere; }
.course-actions { flex-shrink: 0; }
.session-heading { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 23px 0 18px; }
.session-heading h3 { margin: 0; font-size: 15px; font-weight: 650; }
.session-heading h3 span { font-weight: 400; font-size: 12px; color: var(--color-text-secondary); margin-left: 8px; }
.session-heading p { font-size: 12px; color: var(--color-text-secondary); margin: 6px 0 0; }
.session-heading > .ant-btn { flex-shrink: 0; }
.session-date { white-space: nowrap; font-variant-numeric: tabular-nums; }
.session-date small { display: block; color: var(--color-text-secondary); font-size: 11px; margin-top: 4px; }
.load-alert { margin: 16px 0; }
.modal-course { padding: 12px; background: var(--color-surface-soft); border-radius: 7px; font-size: 12px; overflow-wrap: anywhere; }
.no-course-card { padding: 55px 0; }
@media (max-width: 1200px) { .teaching-layout { grid-template-columns: 230px minmax(0, 1fr); gap: 16px; } .course-heading { flex-direction: column; gap: 12px; } }
@media (max-width: 760px) { .teaching-layout { grid-template-columns: minmax(0, 1fr); } .course-catalog :deep(.ant-card-body), .session-card :deep(.ant-card-body) { padding: 16px; } .session-heading { align-items: flex-start; flex-direction: column; } .course-details h2 { font-size: 19px; } .course-item { padding: 10px; } }
</style>
