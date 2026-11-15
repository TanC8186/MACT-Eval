"""Faster-whisper transcription, Chinese text normalisation and CER.

The E2 ASR baseline (spec section 4.4) compares Whisper output against a
reference transcript.  Before comparison both sides are normalised so that
formatting differences do not count as recognition errors:

1. Unicode NFKC (full-width Latin letters/digits become ASCII);
2. traditional -> simplified Chinese when ``zhconv`` or ``opencc`` is
   installed.  When neither package is available the text is left unchanged
   and :data:`SIMPLIFIED_TOOL` is ``"none"``; callers (``evaluate_asr``)
   record this in the run summary rather than pretending conversion happened;
3. only CJK ideographs, ASCII digits and Latin letters are kept - whitespace
   and Chinese/ASCII punctuation are removed;
4. Latin letters are lower-cased (Chinese has no case).

CER is computed with a character-level Levenshtein DP.  ``cer_pair`` returns
``(edits, reference_length)`` so a corpus CER can be aggregated correctly as
``sum(edits) / sum(reference_length)`` instead of averaging per-sentence
ratios, which would over-weight short sentences.

Heavy dependencies are imported lazily: importing this module never requires
``faster-whisper``.  If it is missing, :func:`transcribe_audio` raises
``RuntimeError`` with an actionable message and never fabricates a transcript.
"""
from __future__ import annotations

import importlib.util
import math
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

#: Characters kept after normalisation: CJK ideographs (including extension A
#: and compatibility ideographs), ASCII digits and Latin letters.
_KEEP_CHARACTER_RE = re.compile(
    r"[0-9A-Za-z㐀-䶿一-鿿豈-﫿]"
)


def _detect_simplified_tool() -> str:
    """Return the installed traditional->simplified converter, if any."""
    for module_name, tool_name in (("zhconv", "zhconv"), ("opencc", "opencc")):
        try:
            if importlib.util.find_spec(module_name) is not None:
                return tool_name
        except (ImportError, ValueError):
            # A broken installation behaves like a missing converter; the
            # summary records "none" instead of silently skipping conversion.
            continue
    return "none"


#: ``"zhconv"``, ``"opencc"`` or ``"none"``.  Recorded in run summaries.
SIMPLIFIED_TOOL: str = _detect_simplified_tool()

_opencc_converter: Any = None


def _to_simplified(text: str) -> str:
    """Convert traditional Chinese to simplified when a converter is installed."""
    if SIMPLIFIED_TOOL == "zhconv":
        import zhconv

        return zhconv.convert(text, "zh-cn")
    if SIMPLIFIED_TOOL == "opencc":
        global _opencc_converter
        if _opencc_converter is None:
            from opencc import OpenCC

            _opencc_converter = OpenCC("t2s")
        return _opencc_converter.convert(text)
    return text


def normalization_description() -> dict[str, Any]:
    """Describe the normalisation pipeline for experiment records."""
    return {
        "unicode_form": "NFKC",
        "traditional_to_simplified": SIMPLIFIED_TOOL,
        "lowercase_latin": True,
        "removed": "whitespace, Chinese and ASCII punctuation, all other symbols",
        "kept": "CJK ideographs, ASCII digits, Latin letters",
    }


def normalize_text(text: str) -> str:
    """Normalise a transcript for CER comparison.

    Applies NFKC, optional traditional->simplified conversion, removes
    whitespace/punctuation and lower-cases Latin letters.  When no converter
    is installed, traditional characters are kept as-is; this is reported in
    ``SIMPLIFIED_TOOL`` and must be recorded with any CER result.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be a str, got {type(text).__name__}")
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _to_simplified(normalized)
    kept = _KEEP_CHARACTER_RE.findall(normalized)
    return "".join(kept).lower()


def _levenshtein_distance(reference: str, hypothesis: str) -> int:
    """Character-level Levenshtein distance with two rolling DP rows."""
    # The distance is symmetric; iterate over the longer string as the outer
    # loop so the inner row stays as short as possible.
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference

    previous = list(range(len(hypothesis) + 1))
    for i, reference_char in enumerate(reference, start=1):
        current = [i]
        for j, hypothesis_char in enumerate(hypothesis, start=1):
            deletion = previous[j] + 1
            insertion = current[j - 1] + 1
            substitution = previous[j - 1] + (reference_char != hypothesis_char)
            current.append(min(deletion, insertion, substitution))
        previous = current
    return previous[-1]


def cer_pair(reference: str, hypothesis: str) -> tuple[int, int]:
    """Return ``(edits, reference_length)`` for one sentence pair.

    Both strings are normalised first (see :func:`normalize_text`), so
    punctuation, whitespace and case differences alone produce zero edits.
    ``reference_length`` is the length of the *normalised* reference, i.e. the
    CER denominator.  An empty reference yields ``ref_len == 0``; the pair has
    no defined CER and corpus aggregation excludes it from the denominator.
    """
    if not isinstance(reference, str) or not isinstance(hypothesis, str):
        raise TypeError("reference and hypothesis must be strings")
    normalized_reference = normalize_text(reference)
    normalized_hypothesis = normalize_text(hypothesis)
    edits = _levenshtein_distance(normalized_reference, normalized_hypothesis)
    return edits, len(normalized_reference)


def corpus_cer(pairs: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Aggregate CER over ``(reference, hypothesis)`` pairs.

    Returns::

        {
            "corpus_cer": total_edits / total_ref_chars (or None),
            "total_edits": int,
            "total_ref_chars": int,
            "sentence_cer_mean": mean of per-sentence CERs (or None),
            "n": number of pairs,
        }

    The corpus value is length-weighted; ``sentence_cer_mean`` is reported for
    comparison only.  Pairs whose reference normalises to the empty string are
    counted in ``n`` and their edits still enter ``total_edits``, but they
    cannot contribute to a ratio: if no pair has reference characters the two
    ratio fields are ``None`` rather than a fabricated 0.
    """
    total_edits = 0
    total_ref_chars = 0
    sentence_cers: list[float] = []
    n = 0

    for pair in pairs:
        try:
            reference, hypothesis = pair
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Each pair must be (reference, hypothesis), got {pair!r}"
            ) from exc
        edits, ref_chars = cer_pair(reference, hypothesis)
        n += 1
        total_edits += edits
        total_ref_chars += ref_chars
        if ref_chars > 0:
            sentence_cers.append(edits / ref_chars)

    if n == 0:
        raise ValueError(
            "corpus_cer requires at least one (reference, hypothesis) pair"
        )

    return {
        "corpus_cer": (
            total_edits / total_ref_chars if total_ref_chars > 0 else None
        ),
        "total_edits": total_edits,
        "total_ref_chars": total_ref_chars,
        "sentence_cer_mean": (
            sum(sentence_cers) / len(sentence_cers) if sentence_cers else None
        ),
        "n": n,
    }


def _import_whisper_model() -> Any:
    """Import ``faster_whisper.WhisperModel`` or raise an actionable error."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed; install it with "
            "`pip install faster-whisper` to run ASR. No transcript was produced."
        ) from exc
    return WhisperModel


def load_model(
    model_size: str = "small",
    device: str = "auto",
    compute_type: str = "default",
) -> Any:
    """Load a faster-whisper model once for reuse across many files.

    Raises:
        RuntimeError: if faster-whisper is not installed.  Model download or
            device errors propagate from faster-whisper unchanged.
    """
    whisper_model = _import_whisper_model()
    return whisper_model(model_size, device=device, compute_type=compute_type)


def transcribe_audio(
    audio_path: str | Path,
    *,
    model_size: str = "small",
    device: str = "auto",
    compute_type: str = "default",
    language: str | None = "zh",
    beam_size: int = 5,
    model: Any | None = None,
) -> dict[str, Any]:
    """Transcribe one audio file.

    Args:
        audio_path: Audio file to transcribe.
        model_size: faster-whisper model size/name, used only when ``model``
            is not supplied.
        device: ``"auto"``, ``"cpu"`` or ``"cuda"``.
        compute_type: faster-whisper compute type (``"default"``, ``"int8"``,
            ``"float16"``, ...).
        language: Whisper language code; ``"zh"`` for Mandarin, ``None`` to
            auto-detect.
        beam_size: Decoding beam size (recorded in the run summary).
        model: An already-loaded model; when given, no import or load happens.
            The CLI uses this to load the model once for the whole run.

    Returns:
        ``{"text", "segments", "avg_logprob", "language",
        "language_probability"}``.  ``avg_logprob`` is the unweighted mean of
        the segment log-probabilities (``None`` when no segment reports one).
        ``segments`` contains ``start``/``end``/``text``/``avg_logprob`` per
        segment.

    Raises:
        FileNotFoundError: if ``audio_path`` does not exist.
        RuntimeError: if faster-whisper is not installed and no ``model`` was
            supplied.  No transcript is ever fabricated.
    """
    path = Path(audio_path)
    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")

    if model is None:
        model = load_model(
            model_size=model_size, device=device, compute_type=compute_type
        )

    segment_iterator, info = model.transcribe(
        str(path), language=language, beam_size=beam_size
    )

    text_parts: list[str] = []
    segment_records: list[dict[str, Any]] = []
    logprobs: list[float] = []
    for segment in segment_iterator:
        text = (getattr(segment, "text", "") or "").strip()
        if text:
            text_parts.append(text)

        raw_logprob = getattr(segment, "avg_logprob", None)
        avg_logprob = _finite_or_none(raw_logprob)
        if avg_logprob is not None:
            logprobs.append(avg_logprob)

        segment_records.append(
            {
                "start": round(float(segment.start), 3),
                "end": round(float(segment.end), 3),
                "text": text,
                "avg_logprob": avg_logprob,
            }
        )

    language_probability = _finite_or_none(
        getattr(info, "language_probability", None)
    )

    return {
        "text": " ".join(text_parts),
        "segments": segment_records,
        "avg_logprob": sum(logprobs) / len(logprobs) if logprobs else None,
        "language": getattr(info, "language", None),
        "language_probability": language_probability,
    }


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


__all__ = [
    "SIMPLIFIED_TOOL",
    "cer_pair",
    "corpus_cer",
    "load_model",
    "normalization_description",
    "normalize_text",
    "transcribe_audio",
]
