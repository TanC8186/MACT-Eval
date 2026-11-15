import type { AttendanceSummary } from './attendance.js'
import type { BehaviorTrend } from './behavior.js'
import type { QuestionStatistics } from './question.js'
import type { FeedbackSummary } from './feedback.js'

export interface EvaluationReport {
  id: string
  lessonSessionId: string
  courseId: string
  classId: string
  teacherId: string
  title: string
  summary: string
  dataCoverage: {
    attendance: boolean
    behavior: boolean
    quiz: boolean
    assignment: boolean
    audioInteraction: boolean
    feedback: boolean
  }
  attendanceSummary?: AttendanceSummary
  behaviorTrend?: BehaviorTrend
  quizStatistics?: QuestionStatistics[]
  assignmentSummary?: {
    submissionRate: number
    averageScore: number
    commonIssues: string[]
  }
  feedbackSummary?: FeedbackSummary
  suggestions: SuggestionItem[]
  status: 'draft' | 'published'
  publishedAt?: string
  createdAt: string
  updatedAt: string
}

export interface SuggestionItem {
  id: string
  category: string
  priority: 'high' | 'medium' | 'low'
  content: string
  evidence: string
  status: 'pending' | 'accepted' | 'ignored'
  acceptedAt?: string
  ignoredAt?: string
}
