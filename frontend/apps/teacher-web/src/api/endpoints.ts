import apiClient from './client'
import type { AuthResponse } from '@/types/auth'

/* ===================================================================
 * Generic API response wrapper — mirrors backend ApiResponse<T>
 * =================================================================== */
export interface ApiResponse<T> {
  code: number
  message: string
  data: T
}

/* ===================================================================
 * Domain types — mirrors backend DTOs (camelCase → camelCase)
 * =================================================================== */

/** GET /courses → CourseDTO */
export interface CourseDTO {
  id: string
  name: string
  code: string
  description: string
  teacherId: string
  department: string
  createdAt: string
  updatedAt: string
}

/** GET /sessions → LessonSessionDTO */
export interface LessonSessionDTO {
  id: string
  courseId: string
  courseName: string
  classId: string
  className: string
  teacherId: string
  title: string
  date: string
  startTime: string
  endTime: string
  status: string
  knowledgePointIds: string[]
  teachingObjectives: string
  createdAt: string
}

/** GET /dashboard/summary → DashboardSummaryDTO */
export interface DashboardSummaryDTO {
  totalLessons: number
  avgEngagement: number
  avgTeaching: number
  avgClimate: number
  trend: string // "improving" | "declining" | "stable"
  dataSource: 'REAL' | 'DEMO' | 'EMPTY' | string
}

/** GET /dashboard/suggestions → array of SuggestionDTO */
export interface SuggestionDTO {
  id: string
  category: string
  priority: string   // "HIGH" | "MEDIUM" | "LOW"
  content: string
  evidence: string
  status: string
  createdAt: string
  resolvedAt: string | null
}

/** GET /dashboard/comparison → array of ComparisonDataDTO */
export interface ComparisonDataDTO {
  dimension: string
  selfScore: number
  classAvg: number
  gradeAvg: number
}

/* ===================================================================
 * Endpoint URL constants (keep for direct use or reference)
 * =================================================================== */
export const endpoints = {
  auth: {
    login: '/auth/login',
    register: '/auth/register',
    logout: '/auth/logout',
    refresh: '/auth/refresh',
    profile: '/auth/profile',
  },
  dashboard: {
    summary: '/dashboard/summary',
    comparison: '/dashboard/comparison',
    suggestions: '/dashboard/suggestions',
  },
  course: {
    list: '/courses',
    detail: (id: string) => `/courses/${id}`,
  },
  session: {
    list: '/sessions',
    detail: (id: string) => `/sessions/${id}`,
  },
  classroom: {
    list: '/classrooms',
    detail: (id: string) => `/classrooms/${id}`,
    config: (id: string) => `/classrooms/${id}/config`,
    seats: (id: string) => `/classrooms/${id}/seats`,
  },
  evaluation: {
    create: '/evaluations',
    list: '/evaluations',
    detail: (id: string) => `/evaluations/${id}`,
    report: (id: string) => `/evaluations/${id}/report`,
  },
  suggestion: {
    adopt: (id: string) => `/suggestions/${id}/adopt`,
    accept: (id: string) => `/suggestions/${id}/accept`,
    ignore: (id: string) => `/suggestions/${id}/ignore`,
  },
}

/* ===================================================================
 * Typed API functions — single-responsibility, return unwrapped data
 * =================================================================== */

// ── Courses ──────────────────────────────────────────────────────────

/**
 * GET /courses — list courses for the current teacher.
 * Backend: CourseController.listCourses()
 */
export async function getCourses(): Promise<CourseDTO[]> {
  const res = await apiClient.get<ApiResponse<CourseDTO[]>>('/courses')
  return res.data.data
}

/**
 * GET /courses/:id — get a single course by id.
 * Backend: CourseController.getCourse()
 */
export async function getCourse(id: string): Promise<CourseDTO> {
  const res = await apiClient.get<ApiResponse<CourseDTO>>(`/courses/${id}`)
  return res.data.data
}

// ── Sessions ─────────────────────────────────────────────────────────

/**
 * GET /sessions — list lesson sessions, optionally filtered by courseId.
 * Backend: LessonSessionController.listSessions()
 */
export async function getSessions(courseId?: string): Promise<LessonSessionDTO[]> {
  const params = courseId ? { courseId } : undefined
  const res = await apiClient.get<ApiResponse<LessonSessionDTO[]>>('/sessions', { params })
  return res.data.data
}

/**
 * GET /sessions/:id — get a single session by id.
 * Backend: LessonSessionController.getSession()
 */
export async function getSession(id: string): Promise<LessonSessionDTO> {
  const res = await apiClient.get<ApiResponse<LessonSessionDTO>>(`/sessions/${id}`)
  return res.data.data
}

// ── Dashboard ────────────────────────────────────────────────────────

/**
 * GET /dashboard/summary — aggregate overview metrics.
 * Backend: DashboardController.getSummary()
 */
export async function getDashboardSummary(): Promise<DashboardSummaryDTO> {
  const res = await apiClient.get<ApiResponse<DashboardSummaryDTO>>('/dashboard/summary')
  return res.data.data
}

/**
 * GET /dashboard/comparison — self vs class vs grade comparison data.
 * Backend: DashboardController.getComparison()
 */
export async function getComparison(): Promise<ComparisonDataDTO[]> {
  const res = await apiClient.get<ApiResponse<ComparisonDataDTO[]>>('/dashboard/comparison')
  return res.data.data
}

/** GET /reports/{lessonSessionId}/versions → 已发布报告版本摘要 */
export interface ReportVersionSummary {
  version: number
  status: string
  publishedAt: string | null
  publishedBy: string | null
  contentHash: string | null
  analysisVideoJobId: string | null
  analysisAudioJobId: string | null
  /** 该版本快照的数据来源；旧快照缺失时为 null，不回填默认值。 */
  dataSource?: string | null
  /** 该版本快照的课堂氛围分；旧快照缺失时为 null。 */
  climateScore?: number | null
}

/**
 * GET /reports/{lessonSessionId}/versions — 历史发布版本摘要列表（追溯用）。
 * 不吞异常：调用方需要区分 403（无权）/ 404（课堂不存在）与网络错误。
 */
export async function getReportVersions(lessonSessionId: string): Promise<ReportVersionSummary[]> {
  const res = await apiClient.get<ApiResponse<ReportVersionSummary[]>>(`/reports/${lessonSessionId}/versions`)
  return res.data.data
}

/**
 * GET /reports/{lessonSessionId}/versions/{version} — 指定版本的完整快照，
 * 字段与 GET /reports/{lessonSessionId} 一致。
 * 不吞异常：403/404 必须让调用方看到，不能伪装成空报告。
 */
export async function getReportVersion(lessonSessionId: string, version: number): Promise<any> {
  const res = await apiClient.get<ApiResponse<any>>(`/reports/${lessonSessionId}/versions/${version}`)
  return res.data.data
}

export async function getReports(): Promise<any[]> {
  try { const r = await apiClient.get<ApiResponse<any[]>>('/reports'); return r.data.data; }
  catch { return [] }
}

export async function getCourseDetail(id: string): Promise<any> {
  try { const r = await apiClient.get<ApiResponse<any>>(`/courses/${id}`); return r.data.data; }
  catch { return null }
}

export async function getProfile(): Promise<any> {
  try { const r = await apiClient.get<ApiResponse<any>>('/auth/profile'); return r.data.data; }
  catch { return null }
}

export async function updateProfile(data: any): Promise<void> {
  await apiClient.put('/auth/profile', data)
}

export async function changePassword(data: { oldPassword: string; newPassword: string }): Promise<AuthResponse> {
  const res = await apiClient.put<ApiResponse<AuthResponse>>('/auth/password', data)
  return res.data.data
}

/**
 * GET /dashboard/suggestions — AI-generated teaching suggestions.
 * Backend: DashboardController.getSuggestions()
 */
export async function getSuggestions(): Promise<SuggestionDTO[]> {
  const res = await apiClient.get<ApiResponse<SuggestionDTO[]>>('/dashboard/suggestions')
  return res.data.data
}

// ── Suggestion Actions ──────────────────────────────────────────────

/**
 * POST /suggestions/:id/adopt — adopt a suggestion, recording adoptedAt.
 * Backend: SuggestionController.adopt()
 */
export async function adoptSuggestion(id: string): Promise<void> {
  await apiClient.post(`/suggestions/${id}/adopt`)
}

/**
 * POST /suggestions/:id/accept — mark a suggestion as accepted.
 * Backend: SuggestionController.accept()
 */
export async function acceptSuggestion(id: string): Promise<void> {
  await apiClient.post(`/suggestions/${id}/accept`)
}

/**
 * POST /suggestions/:id/ignore — mark a suggestion as ignored.
 * Backend: SuggestionController.ignore()
 */
export async function ignoreSuggestion(id: string): Promise<void> {
  await apiClient.post(`/suggestions/${id}/ignore`)
}
