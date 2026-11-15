"""Tests for ASR normalisation, CER and the evaluate_asr CLI.

No test requires faster-whisper: the transcription contract is exercised with
fake model objects, and the missing-dependency path is simulated by hiding the
module from the import system.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

from public_data import evaluate_asr
from public_data.asr import (
    SIMPLIFIED_TOOL,
    cer_pair,
    corpus_cer,
    normalize_text,
    transcribe_audio,
)


class _FakeSegment:
    def __init__(
        self,
        start: float,
        end: float,
        text: str,
        avg_logprob: float | None = -0.1,
    ) -> None:
        self.start = start
        self.end = end
        self.text = text
        self.avg_logprob = avg_logprob


class _FakeInfo:
    language = "zh"
    language_probability = 0.99


def test_normalize_text_removes_whitespace_and_punctuation() -> None:
    assert (
        normalize_text("  今天\t我们，学习。ABC １２３！? ")
        == "今天我们学习abc123"
    )
    assert normalize_text("今天，我们。学习！？；：“”‘’（）") == "今天我们学习"
    assert normalize_text("Hello, World! (2024)") == "helloworld2024"


def test_normalize_text_keeps_only_chinese_digits_and_latin() -> None:
    assert normalize_text("课堂 2024 Test-01") == "课堂2024test01"
    assert normalize_text("a_b+c=d") == "abcd"
    assert normalize_text("ABC") == "abc"


def test_normalize_text_uses_converter_when_available() -> None:
    converted = normalize_text("我們學習")
    if SIMPLIFIED_TOOL == "none":
        # Documented fallback: no converter installed, traditional text is
        # kept and the run summary records "none".
        assert converted == "我們學習"
    else:
        assert converted == "我们学习"


def test_normalize_text_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        normalize_text(None)  # type: ignore[arg-type]


def test_cer_pair_ignores_punctuation_and_whitespace() -> None:
    assert cer_pair("今天 我们 学习。", "今天我们学习") == (0, 6)
    assert cer_pair("ABC １２３", "abc123") == (0, 6)


def test_cer_pair_substitution_counts_one_edit() -> None:
    assert cer_pair("今天天气很好", "今天天气很差") == (1, 6)


def test_cer_pair_insertion_counts_one_edit() -> None:
    assert cer_pair("今天学习", "今天学习啊") == (1, 4)


def test_cer_pair_deletion_counts_one_edit() -> None:
    assert cer_pair("今天学习啊", "今天学习") == (1, 5)


def test_cer_pair_empty_reference_has_zero_length() -> None:
    assert cer_pair("", "abc") == (3, 0)


def test_corpus_cer_weights_by_reference_length() -> None:
    # Sentence CERs are 1/2 and 0/10.  The corpus CER must be 1/12, not the
    # arithmetic mean 1/4.
    result = corpus_cer(
        [
            ("春天", "夏天"),
            ("一二三四五六七八九十", "一二三四五六七八九十"),
        ]
    )
    assert result["corpus_cer"] == pytest.approx(1 / 12)
    assert result["total_edits"] == 1
    assert result["total_ref_chars"] == 12
    assert result["sentence_cer_mean"] == pytest.approx(0.25)
    assert result["n"] == 2


def test_corpus_cer_without_reference_characters_is_undefined() -> None:
    result = corpus_cer([("", "abc")])
    assert result["corpus_cer"] is None
    assert result["total_edits"] == 3
    assert result["total_ref_chars"] == 0
    assert result["sentence_cer_mean"] is None
    assert result["n"] == 1


def test_corpus_cer_requires_at_least_one_pair() -> None:
    with pytest.raises(ValueError):
        corpus_cer([])


def test_transcribe_audio_requires_faster_whisper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"")
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(RuntimeError, match="faster-whisper is not installed"):
        transcribe_audio(audio, model_size="tiny")


def test_transcribe_audio_missing_file_fails_before_model_use(
    tmp_path: Path,
) -> None:
    class ExplodingModel:
        def transcribe(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("model must not be called")

    with pytest.raises(FileNotFoundError):
        transcribe_audio(tmp_path / "missing.wav", model=ExplodingModel())


def test_transcribe_audio_returns_text_segments_logprob_and_language(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"")

    class FakeModel:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def transcribe(
            self,
            audio_path: str,
            language: str | None = None,
            beam_size: int = 5,
        ):
            self.calls.append(
                {
                    "audio": audio_path,
                    "language": language,
                    "beam_size": beam_size,
                }
            )
            return (
                iter(
                    [
                        _FakeSegment(0.0, 1.0, " 今天 学习", -0.2),
                        _FakeSegment(1.0, 2.0, "数学。", -0.4),
                    ]
                ),
                _FakeInfo(),
            )

    fake_model = FakeModel()
    result = transcribe_audio(
        audio, model=fake_model, language="zh", beam_size=3
    )

    assert result["text"] == "今天 学习 数学。"
    assert result["avg_logprob"] == pytest.approx(-0.3)
    assert result["language"] == "zh"
    assert result["language_probability"] == pytest.approx(0.99)
    assert result["segments"] == [
        {
            "start": 0.0,
            "end": 1.0,
            "text": "今天 学习",
            "avg_logprob": -0.2,
        },
        {
            "start": 1.0,
            "end": 2.0,
            "text": "数学。",
            "avg_logprob": -0.4,
        },
    ]
    assert fake_model.calls == [
        {"audio": str(audio), "language": "zh", "beam_size": 3}
    ]


def _write_thchs_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "data_thchs30"
    wav_dir = root / "data"
    wav_dir.mkdir(parents=True)
    wav_path = wav_dir / "A11_0.wav"
    wav_path.write_bytes(b"")
    (wav_dir / "A11_0.wav.trn").write_text(
        "今天 天气 很好\njin1 tian1 tian1 qi4 hen3 hao3\n",
        encoding="utf-8",
    )
    return root, wav_path


def test_collect_thchs_items_parses_first_trn_line(tmp_path: Path) -> None:
    root, wav_path = _write_thchs_fixture(tmp_path)

    items = evaluate_asr.collect_thchs_items(root)
    assert len(items) == 1
    assert items[0].item_id == "A11_0"
    assert items[0].audio_path == wav_path
    assert items[0].reference == "今天 天气 很好"

    # Passing the data directory itself is also supported.
    direct_items = evaluate_asr.collect_thchs_items(root / "data")
    assert direct_items == items


def test_collect_thchs_items_requires_matching_trn(tmp_path: Path) -> None:
    root, wav_path = _write_thchs_fixture(tmp_path)
    (wav_path.parent / "A11_0.wav.trn").unlink()

    with pytest.raises(FileNotFoundError, match="transcript"):
        evaluate_asr.collect_thchs_items(root)


def test_read_trn_transcript_falls_back_to_gb18030(tmp_path: Path) -> None:
    trn = tmp_path / "A11_0.wav.trn"
    trn.write_bytes("今天学习\njin1 tian1 xue2 xi2\n".encode("gb18030"))

    assert evaluate_asr.read_trn_transcript(trn) == "今天学习"


def test_read_trn_transcript_resolves_pointer_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    test_dir = tmp_path / "test"
    data_dir.mkdir()
    test_dir.mkdir()
    real = data_dir / "A11_0.wav.trn"
    real.write_text("今天学习\njin1 tian1 xue2 xi2\n", encoding="utf-8")
    pointer = test_dir / "A11_0.wav.trn"
    pointer.write_text("../data/A11_0.wav.trn\n", encoding="utf-8")

    assert evaluate_asr.read_trn_transcript(pointer) == "今天学习"


def test_read_manifest_items_resolves_relative_paths(tmp_path: Path) -> None:
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"")
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "Audio_Path,Transcript\nclip.wav,今天学习\n",
        encoding="utf-8",
    )

    items = evaluate_asr.read_manifest_items(manifest)
    assert len(items) == 1
    assert items[0].item_id == "clip"
    assert items[0].audio_path == audio
    assert items[0].reference == "今天学习"


def test_read_manifest_items_rejects_missing_column(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.csv"
    manifest.write_text("audio_path\nclip.wav\n", encoding="utf-8")

    with pytest.raises(ValueError, match="transcript"):
        evaluate_asr.read_manifest_items(manifest)


def test_read_manifest_items_rejects_missing_audio(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "audio_path,transcript\nmissing.wav,今天学习\n", encoding="utf-8"
    )

    with pytest.raises(FileNotFoundError, match="missing.wav"):
        evaluate_asr.read_manifest_items(manifest)


def test_cli_requires_exactly_one_source() -> None:
    with pytest.raises(SystemExit) as excinfo:
        evaluate_asr.build_parser().parse_args([])
    assert excinfo.value.code != 0

    with pytest.raises(SystemExit) as excinfo:
        evaluate_asr.build_parser().parse_args(["--help"])
    assert excinfo.value.code == 0


class _ScriptedModel:
    """Fake faster-whisper model returning scripted transcripts by file stem."""

    def __init__(self, transcripts: dict[str, str]) -> None:
        self.transcripts = transcripts

    def transcribe(
        self,
        audio: str,
        language: str | None = None,
        beam_size: int = 5,
    ):
        stem = Path(audio).stem
        if stem not in self.transcripts:
            raise RuntimeError(f"no scripted transcript for {stem}")
        return (
            iter(
                [_FakeSegment(0.0, 1.0, self.transcripts[stem], -0.1)]
            ),
            _FakeInfo(),
        )


def _write_two_clip_manifest(tmp_path: Path) -> Path:
    (tmp_path / "a.wav").write_bytes(b"")
    (tmp_path / "b.wav").write_bytes(b"")
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "audio_path,transcript\n"
        "a.wav,今天 学习。\n"
        "b.wav,明天考试\n",
        encoding="utf-8",
    )
    return manifest


def test_main_supports_thchs_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _wav_path = _write_thchs_fixture(tmp_path)
    model = _ScriptedModel({"A11_0": "今天天气很好"})
    monkeypatch.setattr(
        evaluate_asr, "load_model", lambda **kwargs: model
    )
    out_dir = tmp_path / "out"

    code = evaluate_asr.main(
        ["--thchs-root", str(root), "--out", str(out_dir)]
    )

    assert code == 0
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["dataset"]["name"] == "thchs30"
    assert summary["corpus_cer"] == 0.0
    assert summary["total_ref_chars"] == 6


def test_main_limit_truncates_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _write_two_clip_manifest(tmp_path)
    model = _ScriptedModel({"a": "今天学习", "b": "明天考试"})
    monkeypatch.setattr(
        evaluate_asr, "load_model", lambda **kwargs: model
    )
    out_dir = tmp_path / "out"

    code = evaluate_asr.main(
        ["--manifest", str(manifest), "--out", str(out_dir), "--limit", "1"]
    )

    assert code == 0
    with (out_dir / "per_file.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["item_id"] == "a"
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["n"] == 1
    assert summary["input"]["limit"] == 1


def test_main_writes_per_file_csv_summary_json_and_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _write_two_clip_manifest(tmp_path)
    model = _ScriptedModel({"a": "今天学习", "b": "明天考试"})
    monkeypatch.setattr(
        evaluate_asr, "load_model", lambda **kwargs: model
    )
    out_dir = tmp_path / "out"

    code = evaluate_asr.main(
        [
            "--manifest",
            str(manifest),
            "--out",
            str(out_dir),
            "--limit",
            "2",
            "--model-size",
            "tiny",
        ]
    )

    assert code == 0
    with (out_dir / "per_file.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["status"] for row in rows] == ["ok", "ok"]
    assert rows[0]["hypothesis"] == "今天学习"
    assert rows[0]["edits"] == "0"
    assert rows[0]["ref_chars"] == "4"
    assert float(rows[0]["cer"]) == 0.0

    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["n"] == 2
    assert summary["n_success"] == 2
    assert summary["n_failed"] == 0
    assert summary["corpus_cer"] == 0.0
    assert summary["total_edits"] == 0
    assert summary["total_ref_chars"] == 8
    assert summary["sentence_cer_mean"] == 0.0
    assert summary["n_scored"] == 2
    assert summary["model"]["model_size"] == "tiny"
    assert (
        summary["normalization"]["traditional_to_simplified"]
        == SIMPLIFIED_TOOL
    )

    markdown = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "# ASR CER Summary" in markdown
    assert "| Corpus CER | 0.000000 |" in markdown
    assert "None." in markdown


def test_main_exits_nonzero_without_results_when_model_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _write_two_clip_manifest(tmp_path)

    def unavailable(**kwargs: object) -> None:
        raise RuntimeError(
            "faster-whisper is not installed; no transcript was produced"
        )

    monkeypatch.setattr(evaluate_asr, "load_model", unavailable)
    out_dir = tmp_path / "out"

    code = evaluate_asr.main(
        ["--manifest", str(manifest), "--out", str(out_dir)]
    )

    assert code != 0
    assert "faster-whisper is not installed" in capsys.readouterr().err
    assert not out_dir.exists()


def test_main_records_per_file_failure_and_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "good.wav").write_bytes(b"")
    (tmp_path / "bad.wav").write_bytes(b"")
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "audio_path,transcript\n"
        "good.wav,今天学习\n"
        "bad.wav,明天考试\n",
        encoding="utf-8",
    )
    model = _ScriptedModel({"good": "今天学习"})
    monkeypatch.setattr(
        evaluate_asr, "load_model", lambda **kwargs: model
    )
    out_dir = tmp_path / "out"

    code = evaluate_asr.main(
        ["--manifest", str(manifest), "--out", str(out_dir)]
    )

    assert code == 2
    with (out_dir / "per_file.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["status"] == "ok"
    assert rows[1]["status"] == "FAILED"
    assert "no scripted transcript" in rows[1]["error"]
    assert rows[1]["hypothesis"] == ""

    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["n_success"] == 1
    assert summary["n_failed"] == 1
    assert summary["n_scored"] == 1
    assert summary["corpus_cer"] == 0.0
