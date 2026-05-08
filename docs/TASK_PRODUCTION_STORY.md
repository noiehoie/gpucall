# Task Production Story

v2.0 production execution is enabled for `infer` and `vision`.

The following task families have deterministic control-plane recipe contracts in the catalog, but remain `auto_select: false` until an operator adds worker contracts, tuple candidates, billable validation evidence, and production activation:

- `transcribe`: requires audio DataRefs and `speech_to_text` capability.
- `convert`: requires document DataRefs and `document_conversion` capability.
- `train`: requires DataRefs, artifact export, and key-release evidence.
- `fine-tune`: same artifact and key-release gates as `train`.
- `split-infer`: requires activation refs and split-learning evidence.

This keeps unknown or partially supported workloads fail-closed while allowing caller-side preflight intake and admin-side catalog materialization to converge on a stable recipe before any billable provider smoke runs.
