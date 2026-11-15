export type UserRole = 'teacher' | 'student' | 'admin'

export interface User {
  id: string
  role: UserRole
  name: string
  avatar?: string
  createdAt: string
  updatedAt: string
}

export interface Teacher extends User {
  role: 'teacher'
  teacherId: string
  title?: string
  department?: string
  wechatBound?: boolean
}

export interface Student extends User {
  role: 'student'
  studentId: string
  classId: string
  seatRow?: number
  seatCol?: number
  wechatBound?: boolean
  faceConsentGranted?: boolean
  faceConsentGrantedAt?: string
}

export interface Admin extends User {
  role: 'admin'
  adminId: string
  mfaEnabled?: boolean
}
