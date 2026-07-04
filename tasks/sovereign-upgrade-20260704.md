# Sovereign Personal Compute Upgrade Report

Date: 2026-07-04 JST
Releases: v2.0.70, v2.0.71 (published, deployed to the production gateway)
Directive: grow gpucall into the owner's sovereign compute stack — no data to
hosted LLM APIs, GPUs rented per-job, all data and training outputs reclaimed
with evidence, while using current-generation large models.

## 1. Frontier model tier (Qwen3 generation) — DONE

| Route | GPU | Context | Billable validation |
| --- | --- | --- | --- |
| modal-h200-qwen3-32b | H200 | 131072 (YaRN) | **passed** |
| modal-h200-qwen3-30b-a3b (MoE fast tier) | H200 | 131072 (YaRN) | **passed** |
| modal-h200x4-qwen3-235b-a22b-fp8 (flagship) | H200:4, TP4 | 131072 (YaRN) | **passed** |

- Worker renders Qwen3 prompts with `enable_thinking=false` (verified: smoke
  responses carry no thinking blocks), YaRN per model card, engine cache keyed
  by context length.
- One real sizing error caught by the first smoke and fixed: 32B bf16 weights
  (64 GB) + 131K KV cache (32 GiB) cannot fit an 80 GB H100 → moved to H200.
- `infer-rank-text-items-standard` now requires the `frontier_reasoning`
  capability → production ranking routes to Qwen3-32B (235B as authored
  fallback). **First production run on Qwen3-32B: 2026-07-04 16:56 JST,
  `Analysis call completed (attempt 1.1)`, `analysis_failed=False`.**
- The 30B-A3B tier is deliberately not frontier-gated; the
  `requested tuple is not eligible` rejection observed during smoke is the
  capability gate working.

## 2. Data reclamation with evidence — DONE

- `gpucall sovereignty report`: object inventory + ages + presign TTL +
  machine-readable provider residue model + receipt history.
- `gpucall sovereignty reap`: dry-run by default; apply deletes and verifies
  each key absent, writing receipts.
- **First real reclamation**: the gateway store held 661 objects / 334 MB,
  oldest 30.7 days — a month of caller inputs nobody had reclaimed. Reaped
  122 objects / 4.9 MB older than 7 days, `all_verified_absent: true`,
  receipt at `<gateway-state>/sovereignty/reclamation-20260704T073227Z.json`.
- **Standing reclamation**: systemd user timer
  `gpucall-sovereignty-reap.timer` (Sun 03:17, 7-day threshold) installed on
  the gateway — 都度回収 is now the default, not a manual chore.

## 3. Training-output reclamation machinery — DONE (engine pending)

- `gpucall sovereignty reclaim-artifact`: fetch → SHA-256 verify → HKDF +
  AES-256-GCM decrypt with the operator-held master key → local write (0600)
  → optional cloud purge with verified-absent receipt. Every s3 access is
  scoped to an operator bucket allowlist.
- Proven end-to-end against the real worker export path in tests (round trip,
  tamper rejection, wrong-key rejection, out-of-scope bucket refusal).
- Fixed a real crypto asymmetry found in review: the worker KDF salt fell
  back to `plan_id`, which the manifest does not record — hash-less exports
  would have been unrecoverable. Fixed before any real artifact existed.
- **Honest boundary**: the worker's train/fine-tune artifact is currently a
  provenance bundle (input hashes + lineage), not real LoRA weights. The
  reclamation/encryption/purge machinery is production-ready; the actual GPU
  training engine is the single missing piece (next step: peft/trl trainer in
  a dedicated Modal image writing checkpoints through the same export path).

## 4. Production pipeline quality — vision concurrency fixed

- Root cause chain: gateway admission defaults (tuple=1/family=2) shed the
  caller's 10-parallel vision stage; first caller fix edited a nested
  `gemini.vision_semaphore` key the code never reads (found by re-verifying,
  not assuming).
- Fix: gateway launcher (`~/bin/gpucall-gateway-start.sh`) pins limits 4/6/8
  across restarts; caller top-level `vision_semaphore: 3`.
- Verification: 6-task parallel vision run through the caller path —
  **6/6 PASS, zero capacity shedding** (previously 21/21 failed).

## Verification discipline

- Full suite before each release: 1023 passed + 65 SDK (v2.0.70, v2.0.71).
- Release gates green both times (scan-secrets, contamination, parity,
  launch-check, release-check go: True).
- Billable spend this session: 3 frontier smokes + light/vision
  revalidations + 1 failed H100 attempt ≈ $6–9 total, each under an explicit
  `--budget-usd` cap.
- multi-ai-review: 2 real findings fixed (KDF asymmetry, bucket scope);
  2 hallucinations rejected with model-card citations and settled empirically
  by the Modal smokes (Qwen3 IDs have no `-Instruct` suffix; YaRN 131072 is
  the documented Qwen3 configuration and passed live).

## Remaining (owner / next artifact)

| Item | Owner | Next artifact |
| --- | --- | --- |
| Real LoRA trainer behind the existing export path | gpucall dev | training image + train recipe activation |
| Morning production run with fixed vision semaphore | automatic (launchd) | tomorrow's orchestrator log: OverseasVision > 0紙 |
| Panopticon coverage for worker-declared context vs catalog | gpucall dev | probe comparing loaded max_model_len to surface declaration (would have caught the H100 sizing miss pre-billing) |
