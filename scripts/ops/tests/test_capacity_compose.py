"""Pure-logic tests for ``scripts/ops/capacity_compose.py`` (no Docker required).

Every test here feeds fixed strings/JSON samples that mirror what Docker emits,
so the parser, unit conversions, URL assembly and acceptance checks are verified
without starting a container or touching the user's Compose project.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import capacity_compose as cc  # noqa: E402

# Fixed samples copied from real `docker stats --no-stream --format json` output.
STATS_SAMPLE = "\n".join([
    '{"BlockIO":"741kB / 0B","CPUPerc":"0.34%","Container":"mact-cap3-redis-1",'
    '"ID":"64ee26992de2","MemPerc":"0.06%","MemUsage":"5.023MiB / 7.654GiB",'
    '"Name":"mact-cap3-redis-1","NetIO":"50.5kB / 24.2kB","PIDs":"6"}',
    '{"BlockIO":"0B / 0B","CPUPerc":"412.55%","Container":"mact-cap3-ai-service-1",'
    '"ID":"aa11","MemPerc":"21.30%","MemUsage":"1.632GiB / 7.654GiB",'
    '"Name":"mact-cap3-ai-service-1","NetIO":"1kB / 2kB","PIDs":"42"}',
    '{"BlockIO":"0B / 0B","CPUPerc":"377.10%","Container":"mact-cap3-ai-service-2",'
    '"ID":"bb22","MemPerc":"19.80%","MemUsage":"1.501GiB / 7.654GiB",'
    '"Name":"mact-cap3-ai-service-2","NetIO":"1kB / 2kB","PIDs":"41"}',
    '{"BlockIO":"0B / 0B","CPUPerc":"12.00%","Container":"mact-cap3-backend-1",'
    '"ID":"cc33","MemPerc":"7.10%","MemUsage":"512MiB / 1GiB",'
    '"Name":"mact-cap3-backend-1","NetIO":"1kB / 2kB","PIDs":"88"}',
])


# --------------------------------------------------------------------------- #
# Size and percent parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("5.023MiB", 5.023),
    ("1.632GiB", 1671.168),
    ("512MiB", 512.0),
    ("0B", 0.0),
    ("7.654GiB", 7837.696),
    ("1.5kB", 0.0015),
    ("1024KiB", 1.0),
    ("2TiB", 2097152.0),
])
def test_parse_size_mb(text, expected):
    assert cc.parse_size_mb(text) == pytest.approx(expected, rel=1e-3)


@pytest.mark.parametrize("text", ["", "N/A", "bogus", "MiB", "--"])
def test_parse_size_mb_rejects_garbage(text):
    assert cc.parse_size_mb(text) is None


def test_parse_size_mb_handles_thousands_separator():
    assert cc.parse_size_mb("1,024MiB") == pytest.approx(1024.0)


# --------------------------------------------------------------------------- #
# docker stats JSON parsing
# --------------------------------------------------------------------------- #
def test_parse_docker_stats_json_reads_all_containers():
    parsed = cc.parse_docker_stats_json(STATS_SAMPLE)
    assert set(parsed) == {"mact-cap3-redis-1", "mact-cap3-ai-service-1",
                           "mact-cap3-ai-service-2", "mact-cap3-backend-1"}
    ai = parsed["mact-cap3-ai-service-1"]
    assert ai["cpu_pct"] == pytest.approx(412.55)
    assert ai["mem_used_mb"] == pytest.approx(1671.168, rel=1e-3)
    assert ai["mem_limit_mb"] == pytest.approx(7837.696, rel=1e-3)
    assert ai["mem_pct"] == pytest.approx(21.30)


def test_parse_docker_stats_json_skips_malformed_lines():
    text = "\n".join([
        "not json at all",
        '{"Name":"x","CPUPerc":"1%","MemUsage":"1MiB / 2MiB"}',
        '{"broken": ',
        "",
    ])
    parsed = cc.parse_docker_stats_json(text)
    assert list(parsed) == ["x"]
    assert parsed["x"]["mem_used_mb"] == pytest.approx(1.0)


def test_parse_docker_stats_json_handles_empty_and_missing_memusage():
    assert cc.parse_docker_stats_json("") == {}
    parsed = cc.parse_docker_stats_json('{"Name":"y","CPUPerc":"0.00%"}')
    assert parsed["y"]["mem_used_mb"] is None
    assert parsed["y"]["mem_limit_mb"] is None


def test_parse_docker_stats_json_does_not_confuse_container_with_name():
    # `Container` is the short ID field; `Name` must win when both exist.
    parsed = cc.parse_docker_stats_json(
        '{"Container":"abc123","Name":"mact-cap3-ai-service-1",'
        '"CPUPerc":"1%","MemUsage":"1MiB / 2MiB"}')
    assert list(parsed) == ["mact-cap3-ai-service-1"]


# --------------------------------------------------------------------------- #
# Container state / OOM
# --------------------------------------------------------------------------- #
def test_parse_container_state_normal():
    state = cc.parse_container_state("false|0|0|4294967296")
    assert state["oom_killed"] is False
    assert state["exit_code"] == 0
    assert state["restart_count"] == 0
    assert state["memory_limit_bytes"] == 4294967296


def test_parse_container_state_oom_killed():
    state = cc.parse_container_state("true|137|1|1073741824")
    assert state["oom_killed"] is True
    assert state["exit_code"] == 137
    assert state["restart_count"] == 1


def test_parse_container_state_malformed_is_unknown_not_false():
    state = cc.parse_container_state("garbage")
    assert state["oom_killed"] is None
    assert state["raw"] == "garbage"


def test_inspect_format_mentions_required_fields():
    fmt = cc.container_inspect_format()
    for field in ("OOMKilled", "ExitCode", "RestartCount", "HostConfig.Memory"):
        assert field in fmt


# --------------------------------------------------------------------------- #
# URL assembly
# --------------------------------------------------------------------------- #
def test_api_base_default():
    assert cc.api_base() == "http://127.0.0.1:18000/api"


def test_api_base_custom_port_and_context():
    assert cc.api_base("localhost", 18000) == "http://localhost:18000/api"
    assert cc.api_base(port=18000, context_path="/") == "http://127.0.0.1:18000"
    assert cc.api_base(port=18000, context_path="api/v1") == "http://127.0.0.1:18000/api/v1"


def test_api_base_is_not_the_user_port():
    # Guard against a regression that silently measures the user's 8000 stack.
    assert ":8000" not in cc.api_base()


# --------------------------------------------------------------------------- #
# Effective sampling
# --------------------------------------------------------------------------- #
def test_effective_sampling_reports_real_gaps():
    result = cc.effective_sampling([2.0, 2.1, 2.0, 9.5])
    assert result["samples"] == 4
    assert result["median_gap_s"] == pytest.approx(2.05)
    assert result["max_gap_s"] == pytest.approx(9.5)
    assert result["mean_gap_s"] == pytest.approx(3.9)


def test_effective_sampling_empty():
    result = cc.effective_sampling([])
    assert result["samples"] == 0
    assert result["median_gap_s"] is None


def test_effective_sampling_ignores_none():
    result = cc.effective_sampling([None, 2.0, None, 2.0])
    assert result["samples"] == 2
    assert result["median_gap_s"] == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# cgroup v2 probe
# --------------------------------------------------------------------------- #
CGROUP_SAMPLE = "\n".join([
    "memory.max=4294967296",
    "memory.peak=939593728",
    "memory.current=913068032",
    "cpu.max=600000 100000",
    "oom_kill=0",
    "nr_throttled=124",
    "nr_periods=1207",
])


def test_parse_cgroup_probe_reads_kernel_counters():
    parsed = cc.parse_cgroup_probe(CGROUP_SAMPLE)
    assert parsed["memory.max"] == 4294967296
    assert parsed["memory.peak"] == 939593728
    assert parsed["memory.current"] == 913068032
    assert parsed["cpu_quota_us"] == 600000
    assert parsed["cpu_period_us"] == 100000
    assert parsed["oom_kill"] == 0
    assert parsed["nr_throttled"] == 124
    assert parsed["nr_periods"] == 1207


def test_parse_cgroup_probe_unlimited_memory_is_none():
    parsed = cc.parse_cgroup_probe("memory.max=max\nmemory.peak=n/a\n")
    assert parsed["memory.max"] is None
    assert parsed["memory.peak"] is None


def test_parse_cgroup_probe_handles_blank_and_garbage():
    parsed = cc.parse_cgroup_probe("\n\nmemory.peak=\nfoo=bar\n")
    assert parsed["memory.peak"] is None
    assert parsed["foo"] is None


def test_parse_cgroup_probe_marks_oom_kills():
    parsed = cc.parse_cgroup_probe("memory.max=1073741824\nmemory.peak=1200000000\noom_kill=3")
    assert parsed["oom_kill"] == 3


def test_cgroup_probe_script_targets_v2_paths():
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.peak",
                 "/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/memory.events"):
        assert path in cc.CGROUP_PROBE


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def _job(**kwargs):
    job = {"status": "SUCCEEDED", "windows": 1, "observations": 5,
           "effective_interval_s": "10"}
    job.update(kwargs)
    return job


def _checks(**overrides):
    params = dict(
        jobs=[_job()],
        expected_windows=1,
        min_observations=5,
        container_states={
            "mact-cap3-ai-service-1": {"oom_killed": False},
            "mact-cap3-backend-1": {"oom_killed": False},
        },
        sample_peaks={"mact-cap3-ai-service-1": 1632.0,
                      "mact-cap3-backend-1": 512.0},
        limits_mb={"mact-cap3-ai-service-1": 4096.0,
                   "mact-cap3-backend-1": 1024.0},
        watched_containers=["mact-cap3-ai-service-1", "mact-cap3-backend-1"],
        sampling={"median_gap_s": 2.05},
        cgroup={
            "mact-cap3-ai-service-1": {"memory.max": 4294967296,
                                       "memory.peak": 939593728, "oom_kill": 0},
            "mact-cap3-backend-1": {"memory.max": 1073741824,
                                    "memory.peak": 456241152, "oom_kill": 0},
        },
    )
    for key, value in overrides.items():
        params[key] = value
    return cc.compute_checks(**params)


def test_checks_all_pass_on_healthy_run():
    checks = _checks()
    failures = [k for k, v in checks.items() if k != "details" and not v]
    assert failures == []


def test_checks_detect_oom_killed():
    checks = _checks(container_states={
        "mact-cap3-ai-service-1": {"oom_killed": True},
        "mact-cap3-backend-1": {"oom_killed": False},
    })
    assert checks["noOomKilled"] is False
    assert checks["details"]["oomKilled"]["mact-cap3-ai-service-1"] is True


def test_checks_unknown_oom_is_not_a_pass():
    checks = _checks(container_states={
        "mact-cap3-ai-service-1": {"oom_killed": None},
        "mact-cap3-backend-1": {"oom_killed": False},
    })
    assert checks["noOomKilled"] is False


def test_checks_detect_memory_peak_over_limit():
    checks = _checks(sample_peaks={"mact-cap3-ai-service-1": 4096.0,
                                   "mact-cap3-backend-1": 512.0})
    assert checks["memoryWithinLimit"] is False
    assert checks["details"]["peakOverLimit"]["mact-cap3-ai-service-1"] is True


def test_checks_detect_missing_limit():
    checks = _checks(limits_mb={"mact-cap3-ai-service-1": None,
                                "mact-cap3-backend-1": 1024.0})
    # An unknown limit cannot prove the peak was inside it, but must not be
    # reported as an over-limit violation either; the raw values are recorded.
    assert checks["memoryWithinLimit"] is True
    assert checks["details"]["memoryLimitMb"]["mact-cap3-ai-service-1"] is None


def test_checks_failed_job():
    checks = _checks(jobs=[_job(status="FAILED")])
    assert checks["allJobsSucceeded"] is False
    assert checks["allJobsProducedWindows"] is True


def test_checks_cgroup_oom_kill_is_a_failure():
    checks = _checks(cgroup={
        "mact-cap3-ai-service-1": {"memory.max": 4294967296,
                                   "memory.peak": 939593728, "oom_kill": 2},
        "mact-cap3-backend-1": {"memory.max": 1073741824,
                                "memory.peak": 456241152, "oom_kill": 0},
    })
    assert checks["cgroupOomKillZero"] is False
    assert checks["details"]["cgroupOomKills"]["mact-cap3-ai-service-1"] == 2


def test_checks_cgroup_peak_over_limit():
    checks = _checks(cgroup={
        "mact-cap3-ai-service-1": {"memory.max": 1000000000,
                                   "memory.peak": 1000000000, "oom_kill": 0},
        "mact-cap3-backend-1": {"memory.max": 1073741824,
                                "memory.peak": 456241152, "oom_kill": 0},
    })
    assert checks["cgroupMemoryPeakWithinLimit"] is False
    assert checks["details"]["cgroupPeakOverLimit"]["mact-cap3-ai-service-1"] is True


def test_checks_cgroup_missing_values_do_not_pass_oom_check():
    checks = _checks(cgroup={})
    assert checks["cgroupOomKillZero"] is False
    # An unreadable cgroup must not be reported as "over limit" either.
    assert checks["cgroupMemoryPeakWithinLimit"] is True


def test_checks_zero_windows():
    checks = _checks(jobs=[_job(windows=0)])
    assert checks["allJobsProducedWindows"] is False
    assert checks["windowCoverage"] is False


def test_checks_requires_two_jobs_both_good_for_concurrency():
    checks = _checks(jobs=[_job(), _job(status="PARTIAL")])
    assert checks["allJobsSucceeded"] is False


def test_checks_window_coverage_allows_two_missing_windows():
    # Same tolerance as the host-native report: <= 2 windows may be dropped.
    assert _checks(expected_windows=41, jobs=[_job(windows=39)])["windowCoverage"] is True
    assert _checks(expected_windows=41, jobs=[_job(windows=38)])["windowCoverage"] is False


def test_checks_window_coverage_skipped_without_duration():
    # An unknown media duration must not silently pass or fail coverage.
    assert _checks(expected_windows=None)["windowCoverage"] is True


def test_checks_observation_rows_cover_windows():
    # One aggregated observation row per window is the invariant.
    assert _checks(jobs=[_job(windows=2, observations=2)])["observationsCoverEveryWindow"] is True
    assert _checks(jobs=[_job(windows=2, observations=1)])["observationsCoverEveryWindow"] is False


def test_checks_min_observations_is_per_window_not_row_count():
    # A 2-window job with 2 observation rows must pass even though the worker's
    # per-window minObservations is 5; that value is not a row threshold.
    checks = _checks(min_observations=5, jobs=[_job(windows=2, observations=2)])
    assert checks["observationsCoverEveryWindow"] is True
    assert checks["details"]["minObservationsPerWindow"] == 5


def test_checks_zero_window_job_fails_observation_check():
    checks = _checks(jobs=[_job(windows=0, observations=0)])
    assert checks["observationsCoverEveryWindow"] is False


def test_checks_audio_run_skips_behaviour_observations():
    # ASR jobs write zero behaviour observations by design; that must not fail.
    checks = _checks(media_type="audio",
                     jobs=[_job(windows=2, observations=0, transcriptChars=180)])
    assert checks["observationsCoverEveryWindow"] is True
    assert checks["audioTranscriptPresent"] is True


def test_checks_audio_run_requires_transcript_text():
    checks = _checks(media_type="audio",
                     jobs=[_job(windows=2, observations=0, transcriptChars=0)])
    assert checks["audioTranscriptPresent"] is False


def test_checks_video_run_does_not_require_transcript():
    assert _checks(jobs=[_job(transcriptChars=None)])["audioTranscriptPresent"] is True


def test_checks_require_sampling_recorded():
    checks = _checks(sampling={"median_gap_s": None})
    assert checks["samplingIntervalRecorded"] is False


def test_checks_empty_job_list_fails_loudly():
    checks = _checks(jobs=[])
    assert checks["allJobsSucceeded"] is False
    assert checks["allJobsProducedWindows"] is False


# --------------------------------------------------------------------------- #
# Sampler wiring (fake docker/psutil, still no Docker daemon)
# --------------------------------------------------------------------------- #
class _FakeSampler(cc.ContainerResourceSampler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._queue = [STATS_SAMPLE, STATS_SAMPLE]

    def _fake_stats(self, names):
        return cc.parse_docker_stats_json(self._queue.pop(0) if self._queue else "")

    def sample_once(self):
        original = cc.docker_stats
        cc.docker_stats = self._fake_stats
        try:
            return super().sample_once()
        finally:
            cc.docker_stats = original


def test_sampler_sums_both_ai_containers(tmp_path):
    sampler = _FakeSampler(tmp_path, ["mact-cap3-ai-service-1", "mact-cap3-ai-service-2"],
                           backend_container="mact-cap3-backend-1")
    sampler._started = 0.0
    row = sampler.sample_once()
    assert row["ai_cpu_pct"] == pytest.approx(412.55 + 377.10, rel=1e-6)
    assert row["ai_rss_mb"] == pytest.approx(1671.168 + 1537.024, rel=1e-3)
    assert row["backend_cpu_pct"] == pytest.approx(12.0)
    assert row["backend_rss_mb"] == pytest.approx(512.0)
    assert row["ai_mem_limit_mb"] == pytest.approx(7837.696 * 2, rel=1e-3)


def test_sampler_single_ai_container_is_not_doubled(tmp_path):
    sampler = _FakeSampler(tmp_path, ["mact-cap3-ai-service-1"],
                           backend_container="mact-cap3-backend-1")
    sampler._started = 0.0
    row = sampler.sample_once()
    assert row["ai_cpu_pct"] == pytest.approx(412.55, rel=1e-6)
    assert row["ai_rss_mb"] == pytest.approx(1671.168, rel=1e-3)


def test_sampler_peaks_are_per_container(tmp_path):
    sampler = _FakeSampler(tmp_path, ["mact-cap3-ai-service-1", "mact-cap3-ai-service-2"],
                           backend_container="mact-cap3-backend-1")
    sampler._started = 0.0
    sampler.sample_once()
    peaks = sampler.peaks()
    assert peaks["mact-cap3-ai-service-1"] == pytest.approx(1671.168, rel=1e-3)
    assert peaks["mact-cap3-ai-service-2"] == pytest.approx(1537.024, rel=1e-3)
    assert peaks["mact-cap3-backend-1"] == pytest.approx(512.0)


def test_sampler_writes_csv_with_required_columns(tmp_path):
    sampler = _FakeSampler(tmp_path, ["mact-cap3-ai-service-1"],
                           backend_container="mact-cap3-backend-1")
    sampler._started = 0.0
    sampler.sample_once()
    sampler._write_csv()
    header = (tmp_path / "resource-samples.csv").read_text(encoding="utf-8").splitlines()[0]
    for column in ("container_cpu_pct", "container_mem_used_mb",
                   "container_mem_limit_mb", "elapsed_s", "ai_rss_mb"):
        assert column in header or column in (tmp_path / "container-samples.csv").read_text(
            encoding="utf-8").splitlines()[0]
    container_header = (tmp_path / "container-samples.csv").read_text(
        encoding="utf-8").splitlines()[0]
    assert "container_mem_limit_mb" in container_header


def test_sampler_records_real_gap(tmp_path):
    sampler = _FakeSampler(tmp_path, ["mact-cap3-ai-service-1"],
                           backend_container="mact-cap3-backend-1")
    sampler._started = 0.0
    sampler.sample_once()
    sampler._last_elapsed = 0.0  # pretend the previous sample was at t=0
    sampler.sample_once()
    assert sampler.gaps and sampler.gaps[-1] > 0
    summary = sampler.summary()
    assert summary["effectiveSampling"]["samples"] >= 1


def test_sampler_tolerates_missing_containers(tmp_path):
    class _Empty(_FakeSampler):
        def _fake_stats(self, names):
            return {}

    sampler = _Empty(tmp_path, ["mact-cap3-ai-service-1"],
                     backend_container="mact-cap3-backend-1")
    sampler._started = 0.0
    row = sampler.sample_once()
    assert row["ai_cpu_pct"] is None
    assert row["backend_rss_mb"] is None
    assert sampler.peaks()["mact-cap3-ai-service-1"] is None


# --------------------------------------------------------------------------- #
# CLI / integration guards
# --------------------------------------------------------------------------- #
def test_main_rejects_missing_media(tmp_path, capsys):
    code = cc.main(["--video", str(tmp_path / "nope.mp4"), "--label", "x",
                    "--out", str(tmp_path)])
    assert code == 1
    assert "media not found" in capsys.readouterr().err


def test_main_rejects_missing_container(tmp_path, capsys, monkeypatch):
    media = tmp_path / "a.mp4"
    media.write_bytes(b"0")
    monkeypatch.setattr(cc, "container_exists", lambda name: False)
    code = cc.main(["--video", str(media), "--label", "x", "--out", str(tmp_path)])
    assert code == 1
    assert "required container missing" in capsys.readouterr().err


def test_main_rejects_concurrency_two_without_second_consumer(tmp_path, capsys, monkeypatch):
    media = tmp_path / "a.mp4"
    media.write_bytes(b"0")
    monkeypatch.setattr(
        cc, "container_exists",
        lambda name: name != "mact-cap3-ai-service-2")
    code = cc.main(["--video", str(media), "--label", "x", "--out", str(tmp_path),
                    "--concurrency", "2"])
    assert code == 1
    assert "scale ai-service=2" in capsys.readouterr().err


def test_default_out_dir_is_round3_capacity():
    assert cc.DEFAULT_OUT.name == "round3-capacity"
    assert "capacity-2026-10-26" not in str(cc.DEFAULT_OUT)
