export type QuestionType = 'single-choice' | 'multiple-choice' | 'true-false' | 'fill-blank' | 'short-answer'

export interface QuestionOption {
  label: string
  content: string
  isCorrect?: boolean
}

export interface Question {
  id: string
  courseId: string
  knowledgePointIds: string[]
  type: QuestionType
  content: string
  options?: QuestionOption[]
  correctAnswer?: string
  answerExplanation?: string
  score: number
  difficulty?: 'easy' | 'medium' | 'hard'
  createdBy: string
  createdAt: string
  updatedAt: string
}

export interface InClassAnswer {
  id: string
  lessonSessionId: string
  questionId: string
  studentId: string
  answer: string
  isCorrect?: boolean
  score?: number
  submittedAt: string
  gradingStatus?: 'auto-graded' | 'pending-review' | 'teacher-graded'
  teacherScore?: number
  teacherComment?: string
}

export interface QuestionStatistics {
  questionId: string
  totalSubmissions: number
  submittedCount: number
  correctCount: number
  correctRate: number
  optionDistribution: Record<string, number>
  commonErrors?: string[]
  averageTime?: number
}
