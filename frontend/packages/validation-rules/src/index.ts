export interface ValidationRule {
  required?: boolean
  message: string
  pattern?: RegExp
  min?: number
  max?: number
  type?: 'string' | 'number' | 'email'
  validator?: (value: any) => boolean | Promise<boolean>
}

export const required = (fieldName: string): ValidationRule => ({
  required: true,
  message: `请输入${fieldName}`,
})

export const requiredSelect = (fieldName: string): ValidationRule => ({
  required: true,
  message: `请选择${fieldName}`,
})

export const email: ValidationRule = {
  type: 'email',
  message: '请输入有效的邮箱地址',
}

export const phone: ValidationRule = {
  pattern: /^1[3-9]\d{9}$/,
  message: '请输入有效的手机号',
}

export const maxLength = (max: number): ValidationRule => ({
  max,
  message: `最多输入 ${max} 个字符`,
})

export const minLength = (min: number): ValidationRule => ({
  min,
  message: `至少输入 ${min} 个字符`,
})

export const password: ValidationRule[] = [
  { required: true, message: '请输入密码' },
  { min: 6, message: '密码至少 6 位' },
]

export const confirmPassword = (getPassword: () => string): ValidationRule => ({
  validator: (value: string) => value === getPassword(),
  message: '两次输入的密码不一致',
})

export const score = (max: number): ValidationRule => ({
  type: 'number',
  validator: (value: number) => value >= 0 && value <= max,
  message: `评分必须在 0 到 ${max} 之间`,
})
