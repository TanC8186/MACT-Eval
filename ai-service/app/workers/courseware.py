"""
Courseware parsing worker.
Extracts text content from uploaded PPT, Word, PDF, and text files.
"""
from __future__ import annotations

import logging
import os

from app.core.config import settings

logger = logging.getLogger(__name__)


def parse_courseware(job_params: dict) -> dict:
    """
    Parse uploaded courseware file and extract structured text content.

    Synchronous parser. Call via run_in_threadpool (REST) or
    asyncio.to_thread (Redis queue) — never call it on the event loop.

    Job params expected:
    {
        "file_path": str,         # Path to the uploaded file
        "original_name": str,     # Original filename
        "lesson_session_id": str, # Associated lesson
    }

    Returns structured content with metadata.
    """
    file_path = job_params.get("file_path", "")
    original_name = job_params.get("original_name", "unknown")
    lesson_id = job_params.get("lesson_session_id", "unknown")
    allow_demo = bool(job_params.get("allow_demo", False))

    logger.info(f"Parsing courseware: {original_name} for lesson {lesson_id}")

    if not file_path or not os.path.exists(file_path):
        if settings.demo_enabled and allow_demo:
            logger.warning(f"File not found: {file_path}, using explicit demo mode")
            return _generate_demo_content(original_name, lesson_id)
        return {
            "status": "failed",
            "error_code": "FILE_NOT_FOUND",
            "error": "file_path does not exist",
            "lesson_session_id": lesson_id,
        }

    ext = os.path.splitext(original_name)[1].lower()

    try:
        if ext == '.pptx':
            content = _parse_pptx(file_path)
            content_type = "presentation"
        elif ext == '.docx':
            content = _parse_docx(file_path)
            content_type = "document"
        elif ext == '.pdf':
            content = _parse_pdf(file_path)
            content_type = "pdf"
        elif ext in ('.txt', '.md', '.csv'):
            content = _parse_text(file_path)
            content_type = "text"
        else:
            logger.warning(f"Unsupported format: {ext}")
            return {"status": "failed", "error": f"Unsupported format: {ext}"}

        # Extract key information
        paragraphs = [p.strip() for p in content.split('\n') if p.strip()]
        if not paragraphs:
            return {
                "status": "failed",
                "error_code": "EMPTY_CONTENT",
                "error": "courseware contains no extractable text",
                "lesson_session_id": lesson_id,
            }
        # 中文按字/词混合统计：英文单词 + 中日韩字符，避免中文几乎恒为段落数
        import re
        joined = "\n".join(paragraphs)
        word_count = len(re.findall(r'[A-Za-z0-9]+|[一-鿿]', joined))

        # Try to identify teaching objectives and key points
        objectives = []
        key_points = []
        for p in paragraphs:
            lower = p.lower()
            if any(kw in lower for kw in ['教学目标', '学习目标', '教学目的', 'objective', 'goal']):
                objectives.append(p)
            if any(kw in lower for kw in ['重点', '难点', '关键点', 'key point', 'important']):
                key_points.append(p)

        logger.info(f"Parsed {original_name}: {len(paragraphs)} paragraphs, {word_count} words")

        return {
            "status": "succeeded",
            "mode": "real",
            "content_type": content_type,
            "original_name": original_name,
            "paragraph_count": len(paragraphs),
            "word_count": word_count,
            "objectives": objectives[:10],  # Limit to top 10
            "key_points": key_points[:10],
            "full_text": content[:50000],  # Limit to 50KB
            "model_version": "mact-eval-courseware-0.1.0",
        }

    except Exception as e:
        logger.error(f"Failed to parse {original_name}: {e}", exc_info=True)
        return {"status": "failed", "error": str(e)}


def _parse_pptx(file_path: str) -> str:
    """Extract text from PowerPoint file."""
    try:
        from pptx import Presentation
    except ImportError:
        return "python-pptx not installed. Run: pip install python-pptx"

    prs = Presentation(file_path)
    slides_text = []
    for i, slide in enumerate(prs.slides, 1):
        slide_text = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    text = paragraph.text.strip()
                    if text:
                        slide_text.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        text = cell.text.strip()
                        if text:
                            slide_text.append(text)
        if slide_text:
            slides_text.append(f"--- Slide {i} ---\n" + '\n'.join(slide_text))
    return '\n\n'.join(slides_text)


def _parse_docx(file_path: str) -> str:
    """Extract text from Word document."""
    try:
        from docx import Document
    except ImportError:
        return "python-docx not installed. Run: pip install python-docx"

    doc = Document(file_path)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                text = cell.text.strip()
                if text:
                    paragraphs.append(text)
    return '\n'.join(paragraphs)


def _parse_pdf(file_path: str) -> str:
    """Extract text from PDF file."""
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        return "PyPDF2 not installed. Run: pip install PyPDF2"

    reader = PdfReader(file_path)
    pages_text = []
    for i, page in enumerate(reader.pages, 1):
        text = page.extract_text()
        if text and text.strip():
            pages_text.append(f"--- Page {i} ---\n{text.strip()}")
    return '\n\n'.join(pages_text)


def _parse_text(file_path: str) -> str:
    """Read plain text file."""
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def _generate_demo_content(original_name: str, lesson_id: str) -> dict:
    """Generate demo courseware parsing result for demonstration."""
    demo_text = """
--- Slide 1 ---
教育测量与评价
第三章：课堂教学评价方法

--- Slide 2 ---
教学目标
1. 理解课堂评价的基本概念和分类
2. 掌握形成性评价与总结性评价的区别
3. 能够设计简单的课堂评价工具

--- Slide 3 ---
教学重点
- 课堂评价的三种主要类型：诊断性、形成性、总结性
- 评价工具的选择与设计原则
- 评价结果的反馈与应用

--- Slide 4 ---
教学难点
- 如何平衡评价的客观性与教学的人文关怀
- 形成性评价在课堂中的即时应用策略

--- Slide 5 ---
课堂评价的定义
课堂评价是指教师在教学过程中，通过多种方式收集学生学习信息，并据此做出教学决策的系统过程。

--- Slide 6 ---
形成性评价 vs 总结性评价
形成性评价：教学过程中的评价，目的是改进教与学
总结性评价：教学结束后的评价，目的是判断学习成果

--- Slide 7 ---
思考题
1. 你在教学中使用了哪些评价方式？
2. 如何确保评价的公平性？
3. 评价结果如何反馈给学生更有助于学习？
"""
    return {
        "status": "succeeded",
        "content_type": "presentation",
        "original_name": original_name,
        "mode": "demo",
        "paragraph_count": len(demo_text.split('\n')),
        "word_count": len(demo_text.split()),
        "objectives": [
            "教学目标：1. 理解课堂评价的基本概念和分类 2. 掌握形成性评价与总结性评价的区别 3. 能够设计简单的课堂评价工具"
        ],
        "key_points": [
            "教学重点 - 课堂评价的三种主要类型：诊断性、形成性、总结性",
            "教学难点 - 如何平衡评价的客观性与教学的人文关怀"
        ],
        "full_text": demo_text,
        "model_version": "mact-eval-courseware-demo-0.1.0",
    }
