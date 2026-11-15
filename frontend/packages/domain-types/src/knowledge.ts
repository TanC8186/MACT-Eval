export interface KnowledgePoint {
  id: string
  courseId: string
  name: string
  description?: string
  difficulty?: 'easy' | 'medium' | 'hard'
  parentId?: string
  order: number
  createdAt: string
  updatedAt: string
}
