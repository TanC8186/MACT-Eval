"""Courseware structure extraction tests."""
from pathlib import Path

from docx import Document
from pptx import Presentation
from pptx.util import Inches

from app.workers.courseware import _parse_docx, _parse_pptx


def test_pptx_parser_includes_table_cells(tmp_path: Path):
    path = tmp_path / "lesson.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(5), Inches(2)).table
    table.cell(0, 0).text = "教学目标"
    table.cell(0, 1).text = "理解课堂评价"
    table.cell(1, 0).text = "重点"
    table.cell(1, 1).text = "形成性评价"
    prs.save(path)
    result = _parse_pptx(str(path))
    assert "教学目标" in result
    assert "形成性评价" in result


def test_docx_parser_includes_table_cells(tmp_path: Path):
    path = tmp_path / "lesson.docx"
    doc = Document()
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "教学重点"
    table.cell(0, 1).text = "课堂反馈"
    doc.save(path)
    result = _parse_docx(str(path))
    assert "教学重点" in result
    assert "课堂反馈" in result
