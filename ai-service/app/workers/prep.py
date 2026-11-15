"""
DeepSeek LLM courseware prep analysis worker.
Calls the DeepSeek chat API and returns a structured prep-quality report.
"""
import json
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是一位资深教研专家，擅长教学设计分析与备课指导。"
    "请基于给定的课件内容与课程背景，对这节课的备课质量进行专业分析。"
    "分析要客观、具体、可操作。"
)

DIMENSION_KEYS = ["goalClarity", "structure", "keyPoints", "coverage", "alignment"]


def _extract_json(content: str):
    """Parse a JSON object out of the LLM text, tolerating stray prose."""
    try:
        return json.loads(content)
    except Exception:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(content[start:end + 1])
            except Exception:
                return None
        return None


async def analyze_prep(job_params: dict) -> dict:
    """Run DeepSeek prep analysis on the given courseware params."""
    if not settings.deepseek_api_key:
        return {"status": "failed", "error": "DEEPSEEK_API_KEY 未配置"}

    full_text = str(job_params.get("full_text", ""))
    course_name = str(job_params.get("courseName", ""))
    department = str(job_params.get("department", ""))
    course_description = str(job_params.get("courseDescription", ""))
    objectives = str(job_params.get("objectives", "[]"))
    key_points = str(job_params.get("keyPoints", "[]"))

    user_prompt = f"""课件内容：
{full_text}

课件教学目标：{objectives}
课件重点：{key_points}

课程背景：
课程名称：{course_name}
院系：{department}
课程简介：{course_description}

请从以下 5 个维度评估备课质量，每个维度给出 0-100 的分数和具体依据：
1. goalClarity  教学目标清晰度
2. structure    内容组织与逻辑性
3. keyPoints    重难点突出程度
4. coverage     知识覆盖与完整性
5. alignment    与课标/学情匹配度

并给出：
- suggestions：3-5 条具体、可操作的教学建议
- strengths：2-3 条亮点总结
- objectives：从全文语义提炼 2-4 条教学目标（知识/技能/素养）
- keyPointsList：从全文语义提炼 2-4 条重难点（注意不与上面的 keyPoints 维度混淆）

严格输出 JSON（不要输出任何其它文字）：
{{
  "goalClarity": {{"score": 80, "reason": "..."}},
  "structure": {{"score": 80, "reason": "..."}},
  "keyPoints": {{"score": 80, "reason": "..."}},
  "coverage": {{"score": 80, "reason": "..."}},
  "alignment": {{"score": 80, "reason": "..."}},
  "objectives": ["教学目标1", "教学目标2"],
  "keyPointsList": ["重难点1", "重难点2"],
  "suggestions": ["...", "..."],
  "strengths": ["..."]
}}"""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{settings.deepseek_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.deepseek_model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("DeepSeek call failed: %s", e, exc_info=True)
        return {"status": "failed", "error": f"DeepSeek 调用失败: {e}"}

    parsed = _extract_json(content)
    if parsed is None:
        return {"status": "failed", "error": "DeepSeek 返回无法解析为 JSON"}

    dimensions = {k: parsed.get(k) for k in DIMENSION_KEYS if isinstance(parsed.get(k), dict)}
    return {
        "status": "succeeded",
        "dimensions": dimensions,
        "objectives": parsed.get("objectives", []),
        "keyPointsList": parsed.get("keyPointsList", []),
        "suggestions": parsed.get("suggestions", []),
        "strengths": parsed.get("strengths", []),
        "raw_llm_text": content,
        "model": settings.deepseek_model,
    }
