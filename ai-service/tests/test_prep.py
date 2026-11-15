"""Tests for the DeepSeek courseware prep analysis worker."""
import httpx
import pytest

from app.workers.prep import analyze_prep


@pytest.mark.asyncio
async def test_analyze_prep_success(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.deepseek_api_key", "test-key")

    llm_content = (
        '{"goalClarity": {"score": 85, "reason": "目标清晰"}, '
        '"structure": {"score": 80, "reason": "结构合理"}, '
        '"keyPoints": {"score": 75, "reason": "重难点突出"}, '
        '"coverage": {"score": 70, "reason": "覆盖完整"}, '
        '"alignment": {"score": 82, "reason": "与学情匹配"}, '
        '"objectives": ["目标1"], '
        '"keyPointsList": ["重难点1"], '
        '"suggestions": ["建议1", "建议2"], "strengths": ["亮点1"]}'
    )
    fake_response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": llm_content}}]},
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
    )

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return fake_response

    monkeypatch.setattr("app.workers.prep.httpx.AsyncClient", lambda **kw: FakeClient())

    result = await analyze_prep({
        "full_text": "课件正文",
        "courseName": "教学评价",
        "department": "教育学院",
        "courseDescription": "课程简介",
    })

    assert result["status"] == "succeeded"
    assert result["dimensions"]["goalClarity"]["score"] == 85
    assert result["suggestions"] == ["建议1", "建议2"]
    assert result["strengths"] == ["亮点1"]
    assert result["objectives"] == ["目标1"]
    assert result["keyPointsList"] == ["重难点1"]
    assert "raw_llm_text" in result


@pytest.mark.asyncio
async def test_analyze_prep_missing_key(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.deepseek_api_key", "")

    result = await analyze_prep({"full_text": "x"})

    assert result["status"] == "failed"
    assert "未配置" in result["error"]


@pytest.mark.asyncio
async def test_handle_prep_maps_backend_camelcase_params(monkeypatch):
    """The backend flattens camelCase params into the Redis message; _handle_prep
    must map them (fullText -> full_text) or the LLM grades empty input."""
    captured = {}

    async def fake_analyze(params):
        captured["params"] = params
        return {"status": "succeeded"}

    monkeypatch.setattr("app.workers.base.analyze_prep", fake_analyze)
    from app.workers.base import _handle_prep

    await _handle_prep({
        "fullText": "课件正文",
        "courseName": "教学评价",
        "courseDescription": "课程简介",
        "department": "教育学院",
        "originalFileName": "a.pptx",
        "objectives": "[\"目标1\"]",
        "keyPoints": "[\"重点1\"]",
    })

    assert captured["params"]["full_text"] == "课件正文"
    assert captured["params"]["courseName"] == "教学评价"
    assert captured["params"]["courseDescription"] == "课程简介"
