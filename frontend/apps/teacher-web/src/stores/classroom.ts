import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import apiClient from '@/api/client'
import type { CourseDTO } from '@/api/endpoints'

export interface Session {
  id: string
  title: string
  courseId: string
  classId: string
  className?: string
  date: string
  status: 'SCHEDULED' | 'IN_PROGRESS' | 'COMPLETED' | 'CANCELLED'
  teachingObjectives?: string
}

export type Course = CourseDTO

export interface ClassItem {
  id: string
  name: string
  /** Class size from /classes ClassGroupDTO.studentCount; may be missing on older payloads */
  studentCount?: number
}

export const useClassroomStore = defineStore('classroom', () => {
  const selectedCourseId = ref<string | null>(localStorage.getItem('selectedCourseId'))
  const activeSessionId = ref<string | null>(null)
  const sessions = ref<Session[]>([])
  const courses = ref<Course[]>([])
  const classes = ref<ClassItem[]>([])
  const loading = ref(false)
  const saving = ref(false)
  const error = ref('')
  const coursesLoading = ref(false)
  const coursesError = ref('')
  const classesError = ref('')
  const sessionPagination = ref({ current: 1, pageSize: 10, total: 0 })
  let sessionRequest = 0

  const activeSession = computed(() =>
    sessions.value.find(s => s.id === activeSessionId.value) ?? null
  )

  const inProgressSession = computed(() =>
    sessions.value.find(s => s.status === 'IN_PROGRESS') ?? null
  )

  const courseOptions = computed(() =>
    courses.value.map(c => ({ label: c.name, value: c.id }))
  )

  const classOptions = computed(() =>
    classes.value.map(c => ({ label: c.name, value: c.id }))
  )

  function setSelectedCourseId(id: string | null) {
    if (id !== selectedCourseId.value) {
      sessionRequest++
      sessions.value = []
      activeSessionId.value = null
      loading.value = false
      error.value = ''
      sessionPagination.value = { ...sessionPagination.value, current: 1, total: 0 }
    }
    selectedCourseId.value = id
    if (id) {
      localStorage.setItem('selectedCourseId', id)
    } else {
      localStorage.removeItem('selectedCourseId')
    }
  }

  function setActiveSessionId(id: string | null) {
    activeSessionId.value = id
  }

  async function fetchCourses() {
    coursesLoading.value = true
    coursesError.value = ''
    try {
      const allCourses: Course[] = []
      let page = 0
      let total = 0
      do {
        const res = await apiClient.get('/courses/page', { params: { page, size: 100 } })
        allCourses.push(...res.data.data.items)
        total = res.data.data.total
        page++
      } while (page * 100 < total)
      courses.value = allCourses
      // 账号切换或课程删除后，localStorage 里可能残留其他教师的课程 ID。
      // 只保留当前账号可见的课程，避免用旧 ID 触发“课程不存在”。
      if (selectedCourseId.value
        && !courses.value.some(course => course.id === selectedCourseId.value)) {
        setSelectedCourseId(courses.value[0]?.id ?? null)
      }
    } catch (err: any) {
      coursesError.value = err?.response?.data?.message || '课程列表加载失败'
    } finally {
      coursesLoading.value = false
    }
  }

  async function fetchClasses() {
    classesError.value = ''
    try {
      const res = await apiClient.get('/classes')
      classes.value = res.data.data || []
    } catch (err: any) {
      classesError.value = err?.response?.data?.message || '班级列表加载失败'
    }
  }

  async function fetchSessions(page = sessionPagination.value.current) {
    const request = ++sessionRequest
    const courseId = selectedCourseId.value
    error.value = ''
    if (!courseId) {
      sessions.value = []
      activeSessionId.value = null
      loading.value = false
      sessionPagination.value = { ...sessionPagination.value, current: 1, total: 0 }
      return
    }
    loading.value = true
    sessions.value = []
    try {
      const res = await apiClient.get('/sessions/page', {
        params: {
          courseId,
          page: Math.max(0, page - 1),
          size: sessionPagination.value.pageSize,
        },
      })
      if (request !== sessionRequest || courseId !== selectedCourseId.value) return
      sessions.value = res.data.data.items || []
      sessionPagination.value = {
        ...sessionPagination.value,
        current: page,
        total: res.data.data.total || 0,
      }
      const active = sessions.value.find(s => s.status === 'IN_PROGRESS')
      activeSessionId.value = active?.id ?? null
    } catch (err: any) {
      if (request !== sessionRequest || courseId !== selectedCourseId.value) return
      // 保留错误原因，页面显示“加载失败 + 重试”，而不是伪装成没有课堂。
      error.value = err?.response?.data?.message || '课堂列表加载失败'
    } finally {
      if (request === sessionRequest) loading.value = false
    }
  }

  async function createSession(data: {
    title: string
    classId: string
    teachingObjectives: string
  }) {
    const courseId = selectedCourseId.value
    if (!courseId) throw new Error('请先选择课程')
    saving.value = true
    try {
      const res = await apiClient.post('/sessions', {
        ...data,
        courseId,
      })
      if (courseId === selectedCourseId.value) await fetchSessions(1)
      return res.data.data as Session
    } finally {
      saving.value = false
    }
  }

  async function updateSessionStatus(id: string, status: string) {
    try {
      await apiClient.patch(`/sessions/${id}/status`, { status })
      await fetchSessions()
    } catch {
      // re-throw so callers can handle
      throw new Error('操作失败')
    }
  }

  return {
    selectedCourseId,
    activeSessionId,
    sessions,
    courses,
    classes,
    loading,
    saving,
    error,
    coursesLoading,
    coursesError,
    classesError,
    sessionPagination,
    activeSession,
    inProgressSession,
    courseOptions,
    classOptions,
    setSelectedCourseId,
    setActiveSessionId,
    fetchCourses,
    fetchClasses,
    fetchSessions,
    createSession,
    updateSessionStatus,
  }
})
