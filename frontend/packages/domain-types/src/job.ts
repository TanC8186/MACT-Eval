export type JobType = 'courseware-analysis' | 'video-analysis' | 'audio-analysis' | 'grading' | 'report-generation'
export type JobStatus = 'queued' | 'running' | 'partial' | 'succeeded' | 'failed' | 'cancelled'
export type JobPriority = 'high' | 'normal' | 'low'

export interface AnalysisJob {
  id: string
  type: JobType
  status: JobStatus
  priority: JobPriority
  progress: number
  sourceHash: string
  params: Record<string, unknown>
  resultMetadata?: {
    modelVersion: string
    confidence: number
    qualityFlags: string[]
    generatedAt: string
  }
  errorMessage?: string
  retryCount: number
  createdAt: string
  startedAt?: string
  completedAt?: string
}
