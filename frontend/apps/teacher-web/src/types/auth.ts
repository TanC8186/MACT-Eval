export interface AuthResponse {
  token: string
  refreshToken: string
  userId: string
  role: 'teacher' | 'student' | 'admin'
  name: string
  mustChangePassword?: boolean
}

export interface UserProfile {
  id: string
  name: string
  role: 'teacher' | 'student' | 'admin'
  phone?: string
  avatar?: string
  wechatBound?: boolean
  teacherId?: string
  studentId?: string
  mustChangePassword?: boolean
}

export interface LoginRequest {
  username: string
  password: string
}

export interface RegisterRequest {
  username: string
  password: string
  name?: string
  phone?: string
  role?: string
}
