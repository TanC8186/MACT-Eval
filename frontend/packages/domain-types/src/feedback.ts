export type FeedbackType = 'anonymous' | 'identified'
export type FeedbackStatus = 'submitted' | 'read' | 'replied' | 'resolved'

export interface FeedbackEntry {
  id: string
  lessonSessionId: string
  type: FeedbackType
  studentId?: string
  content: string
  theme?: string
  emotionTag?: string
  status: FeedbackStatus
  teacherReply?: string
  teacherRepliedAt?: string
  createdAt: string
  updatedAt: string
}

export interface FeedbackSummary {
  lessonSessionId: string
  totalCount: number
  anonymousCount: number
  identifiedCount: number
  themeDistribution: Record<string, number>
  emotionTrend?: string
}
