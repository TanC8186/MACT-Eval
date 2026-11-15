# MACT-Eval: Video-Speech Classroom Evaluation System

**Undergraduate thesis project · Junxi Tan**

Reviewing a classroom recording requires moving between video, speech, and analysis results. I built MACT-Eval to organize these observations into aligned 60-second windows and make the supporting evidence available in one teacher dashboard.

## What I built

- **Analysis pipeline:** YOLOv8n behavior candidates and faster-whisper transcription, coordinated through FastAPI workers and Redis queues.
- **Temporal alignment and fusion:** window-level video/speech features, rule-based baselines, and configurable decision-level weighted fusion.
- **Review workflow:** course and lesson management, timestamped evidence frames, versioned reports, and explicit handling of missing measurements.
- **Full-stack implementation:** Spring Boot API, Vue 3/TypeScript interface, PostgreSQL records, and private MinIO storage with short-lived media links.

```text
Video + speech → analysis workers → 60-second windows → features / late fusion
                                                        ↓
                                       teacher review + evidence + reports
```

## Key evidence

These are separate component evaluations, each with its own protocol.

| Component | Recorded result | Evaluation scope |
| --- | --- | --- |
| Three-class YOLOv8n | **72.90% mAP@50** | SCB-Dataset3-S: 3,989 training / 1,026 validation images; 100 epochs. |
| Existing six-class SCB5 checkpoint | **77.3862% mAP@50** | Validation only: 1,176 images, input size 640. Not a new training result. |
| faster-whisper `small` | **15.0174% corpus CER** | THCHS-30: 2,495 utterances, CUDA/float16, corrected Chinese references. |

See [research status and evidence boundaries](docs/research-status.md) for the protocols and remaining work. The synthetic JSON in [`examples/`](examples/synthetic-window.json) illustrates the data flow; it is not a measured result or an API import fixture.

## Current status and limitations

The core analysis and teacher/administrator workflow are implemented. Classroom-level annotations and fusion validation remain in progress, so component metrics do not establish teaching effectiveness or the validity of a classroom-atmosphere score. SCB images and THCHS-30 read speech also do not establish performance on arbitrary classroom recordings.

Behavior labels are candidate observations for human review. Head direction and posture do not directly measure attention or learning quality; unavailable measurements remain null. An eight-class detector extension is exploratory, with no final accuracy improvement or deployment claimed in this release.

The supported interface is the teacher/administrator dashboard. Attendance, trends, and intervention-effect tracking have been retired; historical student clients are excluded. Speaker diarization, cross-attention fusion, and an independent text modality are outside the implemented scope.

## Explore the source

| Path | Purpose |
| --- | --- |
| [`frontend/apps/teacher-web/`](frontend/apps/teacher-web/) | Vue 3 teacher/administrator dashboard |
| [`backend/`](backend/) | API, alignment, fusion, reports, and database migrations |
| [`ai-service/`](ai-service/) | Vision and speech workers |
| [`tools/public_data/`](tools/public_data/) | Preparation, annotation, and component evaluation utilities |
| [`scripts/verify/`](scripts/verify/) | Source and environment verification |

For installation, configuration, and test commands, see [Getting started](docs/GETTING_STARTED.md). Model weights and input data must be supplied separately.

## Public release

This repository contains the reviewed source, synthetic illustration, and aggregate results. Classroom recordings, identifiable classroom frames, original datasets, trained checkpoints, credentials, runtime databases, and internal acceptance screenshots are excluded. External data and models remain subject to their own terms; use synthetic or consented material for demonstrations.

Junxi Tan · Software Engineering undergraduate, Guangdong University of Finance and Economics.
