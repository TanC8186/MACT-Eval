export interface User {
  id: string
  username: string
  name: string
  role: 'teacher' | 'student' | 'admin'
}

export interface AuthResponse {
  token: string
  refreshToken: string
  user: User
}

export interface LoginRequest {
  username: string
  password: string
}

export interface RegisterRequest {
  username: string
  password: string
  name: string
  role: 'teacher' | 'student'
}
