# v3 Training / LoRA / Artifact Lifecycle Plan

Updated: 2026-07-02 JST
Status: plan (v3 is an extension of the v2 governance routing contract, not a retrofit)

## Objective

Extend the electricity model beyond single-shot inference into long-running
artifact work: `train`, `fine-tune`, LoRA creation, checkpointing, resumable
jobs, and a governed artifact lifecycle with provenance and cleanup proof.

## What Already Exists (v2 foundation, verified 2026-07-02)

| Foundation | Anchor | State |
| --- | --- | --- |
| Task types `train` / `fine-tune` / `split-infer` in the contract | `TaskRequest`, SDK `SUPPORTED_TASKS`, recipes `train-lora-draft.yml`, `fine-tune-lora-draft.yml` | contract present, async-only, `auto_select: false` |
| Persistent job kernel | job id / attempt id / idempotency key / phase state (SQLite+Postgres stores) | implemented and tested |
| Artifact registry | `gpucall/artifacts.py`: manifest, chain id, version, compare-and-set latest, classification | implemented |
| Object store lifecycle | presign put/get, tenant prefixing, SHA-256 validation, SSRF hardening | implemented |
| Cleanup evidence | leases, `cleanup-audit`, `lease-reaper`, ownership-tagged provider cleanup | implemented, no cryptographic proof yet |
| Metering hooks | dispatcher async success/terminal-failure callbacks, tenant ledger | implemented |
| Budget hard ceilings | estimate → reserve → commit/release, `/v2/estimate` (v2.5) | implemented |

## Gap List (v3 scope)

1. **Training workload contract**: extend recipe grammar with
   `checkpoint_interval`, `max_steps/epochs`, `dataset_refs` (DataRef list),
   `base_model_ref`, `adapter_output_contract` (LoRA rank/alpha/target modules),
   and a `resume_from` artifact reference. Deterministic validation as today.
2. **Checkpoint lifecycle**: worker contract addition — workers must emit
   checkpoint artifacts to the presigned object-store path at the declared
   interval; the gateway records each checkpoint in the artifact chain with
   step metadata. Resumable jobs re-compile with `resume_from` pointing at the
   newest accepted checkpoint.
3. **Resumable jobs**: new job phase `INTERRUPTED_RESUMABLE`; the dispatcher
   treats provider preemption/timeout on a checkpointed job as resumable, not
   terminal, within lease and budget ceilings.
4. **Artifact manifest and provenance**: extend `ArtifactManifest` with
   `produced_by` (job id, attempt id, plan hash, governance hash, tuple,
   worker contract hash), `input_provenance` (dataset DataRef SHA-256 list),
   and `derivation` (base model → adapter chain). All fields deterministic.
5. **Encrypted artifact transfer**: client-side envelope encryption for
   `restricted` artifacts — presign flow unchanged, payload sealed with an
   operator-held key (age or AES-GCM via `cryptography`, already a dep);
   manifest records key fingerprint, never the key.
6. **Cleanup proof**: signed cleanup receipt — after provider-side deletion,
   record `{resource, deletion_api_evidence, verified_absent_at, probe}` and
   hash-chain it into the audit trail. Verification probe = non-generation
   GET expecting 404/absence.
7. **Validation of produced artifacts**: post-training validation gate —
   a governed `infer` smoke with the produced adapter on the exact target
   route before the artifact chain can be marked `production-ready`.
8. **Cost estimate/cap for long jobs**: `/v2/estimate` already covers
   compile-time estimates; add per-checkpoint metering commit so a training
   job draws down budget incrementally and stops at the cap with a resumable
   checkpoint instead of a lost job.
9. **DataRef-reusable artifacts**: an accepted artifact is addressable as a
   DataRef (`artifact://chain/version` resolved by the gateway to a presigned
   ref) so external systems consume outputs exactly like inputs.

## Non-Negotiables Carried Forward

- No inference in control decisions; artifact promotion is deterministic.
- Fail closed on missing object store, missing checkpoint contract, stale
  provider evidence, or budget exhaustion (with resumable checkpoint).
- Secrets, presigned URLs, and DataRef URIs never appear in manifests/logs.
- v3 execution reuses the v2 route validation gates: a training tuple must
  have fresh provider evidence and accepted route validation evidence.

## Sequencing

1. Recipe grammar + contract validation (deterministic tests only)
2. Artifact provenance fields + artifact-as-DataRef resolution
3. Checkpoint worker contract on Modal (happy path), echo-based local
   conformance for the checkpoint cycle
4. Resumable dispatcher phase + incremental metering
5. Encrypted artifact envelope + cleanup receipts
6. Post-training validation gate + production promotion
7. OOB extension: `gpucall setup` object-store gate already blocks file
   workflows; extend the same gate wording to train workloads

## Acceptance (machine-checkable)

- `pytest` suites for each stage; echo-adapter checkpoint-cycle conformance
- a full train→checkpoint→interrupt→resume→artifact→validate→cleanup cycle
  on the Modal happy path within an explicit budget cap
- artifact chain queryable with provenance and cleanup receipts
- caller can consume the artifact as a DataRef in a subsequent infer request
