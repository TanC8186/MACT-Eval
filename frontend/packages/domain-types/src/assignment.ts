export type AssignmentStatus = 'draft' | 'published' | 'closed'
export type SubmissionStatus = 'pending' | 'submitted' | 'grading' | 'graded' | 'returned'

export interface Assignment {
  id: string
  lessonSessionId: string
  title: string
  description?: string
  questionIds: string[]
  attachments?: string[]
  dueDate: string
  status: AssignmentStatus
  totalScore: number
  createdAt: string
  updatedAt: string
}

export interface Submission {
  id: string
  assignmentId: string
  studentId: string
  answers: SubmissionAnswer[]
  attachments?: string[]
  submittedAt: string
  status: SubmissionStatus
  totalScore?: number
  teacherComment?: string
  aiGradingDraft?: AIGradingDraft
  teacherConfirmed: boolean
  teacherConfirmedAt?: string
}

export interface SubmissionAnswer {
  questionId: string
  answer: string
  isCorrect?: boolean
  score?: number
}

export interface AIGradingDraft {
  suggestedScore: number
  confidence: number
  evidence: string
  feedback: string
  gradingCriteria: string
  modelVersion: string
  generatedAt: string
}
