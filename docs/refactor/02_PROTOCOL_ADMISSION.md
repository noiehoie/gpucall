# Protocol Admission Packet

Read this with `00_PRIME_DIRECTIVE.md` and
`01_README_V2_CLAIM_MATRIX.md` before editing OpenAI facade, app endpoints, SDK
OpenAI resources, request models, or request validation.

## Target Boundary

```text
OpenAI wire contract
  -> protocol admission layer
     - official schema/version validation
     - compatibility classification
     - unsupported feature detection
     - content part and size classification
     - model alias policy
     - metadata/header extraction
     - deterministic rejection reasons
  -> governance routing contract
     - task / intent / mode
     - input kind, size, and DataRef requirements
     - confidentiality class
     - tenant budget context
     - requested capabilities
     - catalog and validation constraints
  -> compiler / dispatcher
```

## Current State

- `/v1/chat/completions` admits an OpenAI-like request and converts it into
  `TaskRequest`.
- The current facade is an OpenAI-compatible strict subset, not full OpenAI
  compatibility.
- Text-only chat content is admitted through the facade.
- image/file/DataRef production paths still use gpucall APIs.
- OpenAI `model` semantics are not yet a fully explicit product policy.

## Required Direction

- Extract `openai_facade` into an explicit protocol admission layer.
- Keep OpenAI field interpretation out of compiler, dispatcher, SDK internals,
  and provider adapters.
- Preserve fail-closed behavior for unsupported, unknown, ambiguous, or unsafe
  OpenAI features.
- Treat `model` as `gpucall:auto`, an approved tenant alias, or request metadata
  unless a product decision explicitly allows more.
- Separate "wire compatibility" tests from "governance semantics" tests.

## Forbidden Outcomes

- Do not turn `model` into a raw provider/model selector.
- Do not infer task, intent, confidentiality, or required capabilities from raw
  prompt text.
- Do not route unsupported OpenAI fields by guessing.
- Do not let provider adapters inspect OpenAI wire fields.

## Completion Evidence

A phase touching protocol admission must report:

- OpenAI fields admitted, ignored, rejected, or transformed
- deterministic rejection cases
- mapping from OpenAI request to governance routing contract
- focused tests for accepted and rejected wire shapes
- confirmation that compiler/dispatcher receive governance data, not raw OpenAI
  semantics
