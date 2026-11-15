-- 同一课堂同一题目每个学生只能提交一次，防止重复作答刷高统计
CREATE UNIQUE INDEX uk_answer_lesson_question_student
    ON in_class_answers (lesson_session_id, question_id, student_id);
