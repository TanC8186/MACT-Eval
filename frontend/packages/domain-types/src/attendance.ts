export type AttendanceStatus = 'pending' | 'present' | 'late' | 'absent' | 'excused'
export type AttendanceSource = 'student-checkin' | 'camera-evidence' | 'teacher-correction'

export interface AttendanceRecord {
  id: string
  lessonSessionId: string
  studentId: string
  status: AttendanceStatus
  source: AttendanceSource
  cameraConfidence?: number
  cameraVisible?: boolean
  checkinTime?: string
  teacherConfirmed: boolean
  teacherConfirmedAt?: string
  teacherComment?: string
  createdAt: string
  updatedAt: string
}

export interface AttendanceSummary {
  lessonSessionId: string
  totalStudents: number
  present: number
  late: number
  absent: number
  excused: number
  pending: number
  coverage: number
  dataQuality: 'good' | 'limited' | 'unavailable'
}
