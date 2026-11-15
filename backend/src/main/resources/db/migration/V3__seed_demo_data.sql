-- MACT-Eval 演示数据
-- 版本: V3
-- 注意: 仅用于开发环境，生产环境不执行

-- 管理员账号
INSERT INTO users (id, name, password, role, admin_id, mfa_enabled, created_at, updated_at)
VALUES ('admin-001', '系统管理员', '$2a$10$dummyhashfordevelopment', 'admin', 'ADM001', false, NOW(), NOW())
ON CONFLICT (id) DO NOTHING;

-- 教师账号
INSERT INTO users (id, name, password, role, teacher_id, title, department, created_at, updated_at)
VALUES ('teacher-001', '张老师', '$2a$10$dummyhashfordevelopment', 'teacher', 'TCH001', '副教授', '教育技术系', NOW(), NOW())
ON CONFLICT (id) DO NOTHING;

-- 示范班级
INSERT INTO class_groups (id, name, grade, department, student_count, created_at, updated_at)
VALUES ('class-001', '2023级教育技术学1班', '大三', '教育技术系', 30, NOW(), NOW())
ON CONFLICT (id) DO NOTHING;

-- 示范课程
INSERT INTO courses (id, name, code, description, teacher_id, department, created_at, updated_at)
VALUES ('course-001', '教育测量与评价', 'EDU401', '本课程介绍教育评价的基本理论与方法', 'teacher-001', '教育技术系', NOW(), NOW())
ON CONFLICT (id) DO NOTHING;
