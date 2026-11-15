import type { KnowledgePoint } from './knowledge.js'

export interface Course {
  id: string
  name: string
  code?: string
  description?: string
  teacherId: string
  department?: string
  knowledgePoints: KnowledgePoint[]
  createdAt: string
  updatedAt: string
}

export interface Class {
  id: string
  name: string
  grade?: string
  department?: string
  studentCount: number
  createdAt: string
  updatedAt: string
}

export type ActivityType = 'lecture' | 'discussion' | 'group-work' | 'quiz' | 'presentation' | 'practice' | 'other'

export interface LessonSession {
  id: string
  courseId: string
  classId: string
  teacherId: string
  title: string
  date: string
  startTime: string
  endTime?: string
  status: 'scheduled' | 'in-progress' | 'completed' | 'cancelled'
  activities: Activity[]
  knowledgePointIds: string[]
  teachingObjectives?: string
  createdAt: string
  updatedAt: string
}

export interface Activity {
  id: string
  lessonSessionId: string
  type: ActivityType
  label: string
  startTime: string
  endTime?: string
  duration?: number
}
