# Research status and evidence boundaries

Source scope: reviewed local implementation as of 2026-11-09; snapshot prepared on 2026-11-16.

## Implemented

The source contains the Spring Boot API, FastAPI video/speech workers, Redis queues, 60-second alignment, decision-level weighted fusion, versioned reports, and the Vue 3 teacher/administrator dashboard. Candidate actions and timestamped frames support human review. Attendance, trends, and intervention-effect tracking have been retired. Historical student clients are excluded from this public snapshot.

## Recorded component evaluations

| Component | Protocol | Result |
| --- | --- | --- |
| Three-class YOLOv8n | SCB-Dataset3-S; 3,989 train / 1,026 val; 100 epochs; 640 input | mAP50 0.7290; mAP50-95 0.5486 |
| Existing six-class checkpoint | SCB5; validation only; 1,176 val images; 640 input | mAP50 0.773862; mAP50-95 0.596482 |
| faster-whisper small | THCHS-30; 2,495 utterances; CUDA/float16; normalized Chinese reference text | Corpus CER 0.150174; 12,185 edits / 81,139 reference characters |

The six-class evaluation is not a new training result and cannot be compared directly with the separate three-class protocol. CER uses corrected transcript reference resolution; the initial uncorrected evaluation is excluded. Read-speech CER is not a classroom far-field speech result.

## Work in progress

The public release corrects the chance baseline for performance-derived fusion weights: macro one-vs-rest ROC AUC uses 0.5, rather than the three-class accuracy baseline of 1/3. Weights now use `max(0, AUC - 0.5)` before normalization; if neither modality exceeds chance, they fall back to equal weights. Synthetic regression tests cover chance-level scores, below-chance scores, and unequal positive excess. This correction does not change the reported vision or ASR component metrics, and no completed fusion experiment is claimed.

Classroom-level double annotation, agreement analysis, fusion evaluation, and independent target-view action validation remain incomplete in the inspected records. Eight-class training has a successful one-epoch pipeline check, but a final 20-epoch result is not present in the local evidence reviewed for this release. No accuracy improvement or production-model replacement is claimed.

## Reproduction assets

External data and weights are obtained separately under their own terms. This repository includes evaluation utilities and source tests, but it excludes classroom media, identifiable frames, datasets, checkpoints, private launch records, and runtime databases. The JSON example in `examples/` is manually constructed and is an explanatory illustration, not a measured classroom result.

Historical internal verification records are deliberately not reproduced as public screenshots or logs. Test commands in the README can be run against this checkout; results must be reported for the actual revision and environment used.
