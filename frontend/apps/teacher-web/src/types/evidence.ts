export interface ActionDetection {
  subjectId: string
  code: string
  label: string
  confidence: number
  bbox: [number, number, number, number]
}
// 后端 action_evidence.py 还会下发 className（英文类别名），前端不用它渲染，
// 因此不在此声明，避免留下无人读取的死字段。

export interface EvidenceFrame {
  id: string
  windowIndex: number
  timestampMs: number | null
  code: string
  label: string
  url: string | null
  metadata?: {
    detectionSource?: 'behavior_model' | 'position_proxy' | 'overview'
    modelVersion?: string
    modelDigest?: string
    imageWidth?: number
    imageHeight?: number
    primarySubjectId?: string
    identityScope?: string
    detections?: ActionDetection[]
    supportedActions?: string[]
    /** 请求的采样间隔（秒），来自报告生成时的分析参数。 */
    requestedSampleIntervalSec?: number
    /** 本次任务实际生效的采样间隔（秒），随课堂时长自适应放大。 */
    effectiveSampleIntervalSec?: number
  }
}
