from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gpucall.config import load_admin_automation
from gpucall.handoff import _default_quality_feedback_inbox
from gpucall.release import ONBOARDING_MANUAL_URL, ONBOARDING_PROMPT_URL, SDK_WHEEL_URL


HANDOFF_PACKAGE_FILES = (
    "gpucall-handoff.json",
    "CALLER_ENGINEER_README.md",
    "caller-ai-onboarding-prompt.md",
    "acceptance-checklist.json",
    "MANIFEST.json",
)


def build_handoff_contract(config_dir: str | Path, system_name: str, *, require_concrete: bool = False) -> dict[str, Any]:
    automation = load_admin_automation(Path(config_dir))
    gateway_url = automation.api_key_bootstrap_gateway_url or "<GPUCALL_BASE_URL>"
    recipe_inbox = automation.api_key_bootstrap_recipe_inbox or "<GPUCALL_RECIPE_INBOX>"
    contract = {
        "schema_version": 1,
        "phase": "gpucall-caller-handoff",
        "system_name": system_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gateway": {
            "base_url": gateway_url,
            "bootstrap_endpoint": f"{gateway_url.rstrip('/')}/v2/bootstrap/tenant-key" if not _has_placeholder(gateway_url) else "<GPUCALL_BOOTSTRAP_ENDPOINT>",
        },
        "inboxes": {
            "recipe": recipe_inbox,
            "quality_feedback": _default_quality_feedback_inbox(recipe_inbox) if not _has_placeholder(recipe_inbox) else "<GPUCALL_QUALITY_FEEDBACK_INBOX>",
        },
        "assets": {
            "onboarding_prompt_url": automation.onboarding_prompt_url or ONBOARDING_PROMPT_URL,
            "onboarding_manual_url": automation.onboarding_manual_url or ONBOARDING_MANUAL_URL,
            "sdk_wheel_url": automation.caller_sdk_wheel_url or SDK_WHEEL_URL,
        },
        "security": {
            "api_key_handoff_mode": automation.api_key_handoff_mode.value,
            "api_key_is_embedded": False,
            "provider_credentials_included": False,
        },
    }
    if require_concrete:
        blockers = _handoff_contract_placeholders(contract)
        if blockers:
            raise ValueError("handoff package requires concrete values: " + ", ".join(blockers))
    return contract


def caller_ai_onboarding_prompt(contract: dict[str, Any]) -> str:
    system_name = str(contract["system_name"])
    gateway = contract["gateway"]
    inboxes = contract["inboxes"]
    assets = contract["assets"]
    api_key_mode = contract["security"]["api_key_handoff_mode"]
    return f"""# gpucall Caller-Side Onboarding Prompt

You are the coding AI CLI responsible for adapting the caller repository named `{system_name}` to gpucall.

System name: {system_name}

Use only the operator-provided handoff values below. Treat them as authoritative.

## Handoff

- GPUCALL_BASE_URL: `{gateway["base_url"]}`
- GPUCALL_BOOTSTRAP_ENDPOINT: `{gateway["bootstrap_endpoint"]}`
- GPUCALL_RECIPE_INBOX: `{inboxes["recipe"]}`
- GPUCALL_QUALITY_FEEDBACK_INBOX: `{inboxes["quality_feedback"]}`
- GPUCALL_SDK_WHEEL_URL: `{assets["sdk_wheel_url"]}`
- GPUCALL_ONBOARDING_PROMPT_URL: `{assets["onboarding_prompt_url"]}`
- GPUCALL_ONBOARDING_MANUAL_URL: `{assets["onboarding_manual_url"]}`
- GPUCALL_API_KEY_HANDOFF_MODE: `{api_key_mode}`

## Hard Rules

- Do not clone, install, modify, vendor, or import the gpucall gateway repository.
- Work only inside the caller repository, plus explicit operator inboxes and XDG-owned gpucall scratch paths.
- Do not create sibling directories such as `gpucall-c-tooling`, `gpucall-panopticon`, or ad hoc sandboxes next to the caller repository.
- Do not ask for provider credentials, GPU names, endpoint IDs, model IDs, recipes, tuples, or provider choice.
- Do not add direct hosted-AI fallback. Unknown or unsupported work must fail closed and submit sanitized intake.
- Do not send raw confidential payloads to the recipe inbox. Submit sanitized intent, metadata, workload contracts, and quality feedback only.
- Final status must be exactly `Go` or `No-Go`; skipped canary is `No-Go`.

## Required Flow

1. Inspect the caller repository for LLM, Vision, GPU, hosted-AI, local-model, OCR, embedding, and file/image analysis paths.
2. Export the handoff values exactly as provided:

```bash
export GPUCALL_BASE_URL="{gateway["base_url"]}"
export GPUCALL_BOOTSTRAP_ENDPOINT="{gateway["bootstrap_endpoint"]}"
export GPUCALL_RECIPE_INBOX="{inboxes["recipe"]}"
export GPUCALL_QUALITY_FEEDBACK_INBOX="{inboxes["quality_feedback"]}"
export GPUCALL_SDK_WHEEL_URL="{assets["sdk_wheel_url"]}"
```

3. Establish the caller-facing API key without printing it. If `GPUCALL_API_KEY_HANDOFF_MODE` is `trusted_bootstrap`, request the key exactly once from `GPUCALL_BOOTSTRAP_ENDPOINT`, store it in the caller runtime secret environment, and set `GPUCALL_API_KEY` from that secret. If the mode is `manual`, do not call bootstrap; require an operator-issued `GPUCALL_API_KEY`. Never ask for provider credentials.

4. Install only the caller SDK/helper wheel from `GPUCALL_SDK_WHEEL_URL`:

```bash
uv tool install --force "$GPUCALL_SDK_WHEEL_URL"
```

5. Confirm the wheel provides both caller-side commands:

```bash
gpucall-migrate --help
gpucall-recipe-draft --help
```

6. Run deterministic migration assessment and baseline tracing from inside the caller repository.
   First identify the smallest representative baseline command from the caller
   repository. Do not run a broad production job. Record the selected command
   in `.gpucall-migration/baseline-command.txt`, then use that exact command
   in the trace step.

```bash
gpucall-migrate assess . --source {system_name}
mkdir -p .gpucall-migration
printf '%s\n' "$CALLER_BASELINE_COMMAND" > .gpucall-migration/baseline-command.txt
gpucall-migrate trace . --command "$CALLER_BASELINE_COMMAND" --backend baseline
gpucall-migrate profile . --trace .gpucall-migration/workload-trace.json
gpucall-migrate draft-contract . --profile .gpucall-migration/workload-profile.json --write-intake
gpucall-migrate preflight . --source {system_name}
```

7. Verify that deterministic draft artifacts exist, then submit the intake and draft to `GPUCALL_RECIPE_INBOX` using `gpucall-recipe-draft`. The helper auto-generates a draft from recipe intake when `--draft` is omitted, but if `.gpucall-migration/recipe-draft.json` exists you must submit it explicitly:

```bash
test -s .gpucall-migration/recipe-intake.json
test -s .gpucall-migration/recipe-draft.json
gpucall-recipe-draft submit \
  --intake .gpucall-migration/recipe-intake.json \
  --draft .gpucall-migration/recipe-draft.json \
  --remote-inbox "$GPUCALL_RECIPE_INBOX" \
  --source {system_name}
```

If `GPUCALL_RECIPE_INBOX` is an absolute local path, use `--inbox-dir "$GPUCALL_RECIPE_INBOX"` instead of `--remote-inbox`. If you submit preflight-only requests, `gpucall-recipe-draft preflight --remote-inbox "$GPUCALL_RECIPE_INBOX"` must create a submission whose top-level `draft` field is a JSON object, not `null`. Submit low-quality success feedback to `GPUCALL_QUALITY_FEEDBACK_INBOX` with `--remote-quality-inbox` or `--quality-inbox-dir`; quality feedback must not create recipe drafts.
8. Patch caller wrappers so application code sends only task, mode, input data, or DataRefs to `GPUCALL_BASE_URL`.
9. Run caller canaries through the gpucall gateway when the operator handoff says the gateway is ready.
10. Run the caller business validator and write a final onboarding report under `.gpucall-migration/`.

## Required Artifacts

- `.gpucall-migration/assessment.json`
- `.gpucall-migration/workload-trace.json`
- `.gpucall-migration/workload-profile.json`
- `.gpucall-migration/workload-contract.json`
- `.gpucall-migration/recipe-intake.json`
- `.gpucall-migration/recipe-draft.json`
- sanitized recipe submission evidence proving the submitted top-level `draft` field is a JSON object
- gateway canary report or explicit No-Go reason
- caller business validator report
- final `Go` or `No-Go` decision with blockers owned by caller, gpucall-admin, or provider-ops

## Workspace Footprint

Allowed caller-side writes:

- files inside the caller repository
- `.gpucall-migration/` inside the caller repository
- `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, and `XDG_CACHE_HOME`
- explicit operator inboxes listed in this handoff

Any other write location is a product onboarding failure.
"""


def caller_engineer_readme(contract: dict[str, Any]) -> str:
    system_name = str(contract["system_name"])
    gateway = contract["gateway"]
    inboxes = contract["inboxes"]
    assets = contract["assets"]
    api_key_mode = contract["security"]["api_key_handoff_mode"]
    return f"""# gpucall Caller Engineer Handoff

This package tells the `{system_name}` engineering owner how to accept gpucall without learning provider, GPU, model, endpoint, recipe, or tuple operations.

System name: {system_name}

## What This Package Is

The gpucall operator has prepared a router-side environment and is handing off only the caller-side integration contract. The caller-side engineer owns the application repository and its business validator. The gpucall operator owns gateway configuration, provider supply, recipes, tuples, validation evidence, price evidence, and production promotion.

## Handoff Values

- GPUCALL_BASE_URL: `{gateway["base_url"]}`
- GPUCALL_BOOTSTRAP_ENDPOINT: `{gateway["bootstrap_endpoint"]}`
- GPUCALL_RECIPE_INBOX: `{inboxes["recipe"]}`
- GPUCALL_QUALITY_FEEDBACK_INBOX: `{inboxes["quality_feedback"]}`
- GPUCALL_SDK_WHEEL_URL: `{assets["sdk_wheel_url"]}`
- GPUCALL_ONBOARDING_PROMPT_URL: `{assets["onboarding_prompt_url"]}`
- GPUCALL_ONBOARDING_MANUAL_URL: `{assets["onboarding_manual_url"]}`
- API key handoff mode: `{api_key_mode}`

This package does not include API keys, provider credentials, provider account details, GPU names, endpoint IDs, model IDs, recipes, or tuples.

## Responsibility Boundary

Caller engineer responsibilities:

- integrate the caller application with the gpucall SDK/helper wheel
- run caller-side workload inventory, baseline tracing, preflight, canary, and business validation
- submit sanitized workload intent, deterministic recipe draft artifacts, and quality feedback to the listed inboxes
- return a final `Go` or `No-Go` onboarding report

gpucall operator responsibilities:

- operate the gpucall gateway, Provider Panopticon, recipe inboxes, and quality inboxes
- manage provider credentials, provider supply, endpoint provisioning, prices, recipes, tuples, and validation evidence
- decide whether sanitized caller intake becomes a production recipe or tuple

## What You Must Not Do

- Do not clone, install, modify, vendor, or import the gpucall gateway repository.
- Do not choose providers, GPUs, endpoint IDs, model IDs, recipes, tuples, or fallback order in caller code.
- Do not request or store provider credentials in the caller repository.
- Do not add direct hosted-AI fallback for unknown or unsupported workloads.
- Do not send raw confidential payloads to recipe or quality inboxes.
- Do not create sibling directories such as `gpucall-c-tooling`, `gpucall-panopticon`, or ad hoc sandboxes next to the caller repository.

## How To Use The AI CLI Prompt

Give `caller-ai-onboarding-prompt.md` to the coding AI CLI that will edit the caller repository. The AI CLI should work inside the caller repository only. The human engineer should review the generated `.gpucall-migration/` artifacts and run the caller business validator before declaring `Go`.

If the AI CLI cannot complete a step, do not patch around the failure by selecting a provider or weakening the workload. Return `No-Go` with the blocker and the artifact that proves it.

## Expected Caller Artifacts

- `.gpucall-migration/assessment.json`
- `.gpucall-migration/workload-trace.json`
- `.gpucall-migration/workload-profile.json`
- `.gpucall-migration/workload-contract.json`
- `.gpucall-migration/recipe-intake.json`
- `.gpucall-migration/recipe-draft.json`
- sanitized recipe submission evidence proving the submitted top-level `draft` field is a JSON object
- gateway canary report or explicit `No-Go` reason
- caller business validator report
- final onboarding report with exactly `Go` or `No-Go`

## Failure Routing

- unknown workload or missing recipe: return sanitized intake to `GPUCALL_RECIPE_INBOX`; owner is gpucall operator / recipe admin
- validation missing: wait for gpucall operator validation evidence; owner is gpucall operator / recipe admin
- provider missing or supply provisioning required: wait for provider supply repair/provisioning; owner is provider operations
- price unknown: wait for fresh Provider Panopticon price evidence; owner is gpucall operator / recipe admin
- endpoint stale: wait for endpoint repair, recreation, or decommission; owner is provider operations
- caller baseline, workload contract, or business validator failure: fix the caller repository or return caller-owned `No-Go`

## Go / No-Go Rule

Declare `Go` only when the caller repository uses gpucall through the handoff values, required artifacts exist, gateway canary ran when allowed, the caller business validator passed, and no forbidden workspace writes occurred.

Declare `No-Go` for skipped canary, missing baseline, missing workload contract, failed business validator, unknown workload without accepted intake, missing validation, provider shortage, unknown price, stale endpoint, or any write outside the allowed caller/XDG/operator-inbox locations.
"""


def prompt_quality_blockers(prompt: str, contract: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if _handoff_contract_placeholders(contract):
        blockers.append("handoff_contract_contains_placeholders")
    placeholder_tokens = ("<GPUCALL_", "{{", "}}")
    if any(token in prompt for token in placeholder_tokens):
        blockers.append("prompt_contains_placeholder")
    if "<caller baseline command>" in prompt:
        blockers.append("prompt_contains_baseline_command_placeholder")
    if _prompt_contains_unresolved_marker(prompt):
        blockers.append("prompt_contains_unresolved_marker")
    for value in _required_handoff_values(contract):
        if str(value) not in prompt:
            blockers.append(f"prompt_missing_value:{value}")
    required_phrases = [
        "Do not clone, install, modify, vendor, or import the gpucall gateway repository.",
        "gpucall-migrate assess",
        "gpucall-migrate trace",
        "gpucall-migrate draft-contract",
        "gpucall-migrate --help",
        "gpucall-recipe-draft",
        "--remote-inbox",
        ".gpucall-migration/recipe-draft.json",
        "top-level `draft` field is a JSON object",
        "Final status must be exactly `Go` or `No-Go`",
        ".gpucall-migration/workload-contract.json",
        "Any other write location is a product onboarding failure.",
    ]
    for phrase in required_phrases:
        if phrase not in prompt:
            blockers.append(f"prompt_missing_required_phrase:{phrase}")
    return blockers


def human_readme_quality_blockers(readme: str, contract: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if _handoff_contract_placeholders(contract):
        blockers.append("handoff_contract_contains_placeholders")
    placeholder_tokens = ("<GPUCALL_", "{{", "}}")
    if any(token in readme for token in placeholder_tokens):
        blockers.append("readme_contains_placeholder")
    if _prompt_contains_unresolved_marker(readme):
        blockers.append("readme_contains_unresolved_marker")
    for value in _required_handoff_values(contract):
        if str(value) not in readme:
            blockers.append(f"readme_missing_value:{value}")
    required_phrases = [
        "Responsibility Boundary",
        "caller-ai-onboarding-prompt.md",
        "This package does not include API keys, provider credentials",
        "Do not clone, install, modify, vendor, or import the gpucall gateway repository.",
        "Do not choose providers, GPUs, endpoint IDs, model IDs, recipes, tuples, or fallback order in caller code.",
        "Failure Routing",
        "Go / No-Go Rule",
        "Declare `Go` only when",
        "Declare `No-Go`",
    ]
    for phrase in required_phrases:
        if phrase not in readme:
            blockers.append(f"readme_missing_required_phrase:{phrase}")
    return blockers


def build_handoff_package(config_dir: str | Path, system_name: str, *, require_concrete: bool = True) -> dict[str, Any]:
    contract = build_handoff_contract(config_dir, system_name, require_concrete=require_concrete)
    prompt = caller_ai_onboarding_prompt(contract)
    readme = caller_engineer_readme(contract)
    prompt_blockers = prompt_quality_blockers(prompt, contract)
    readme_blockers = human_readme_quality_blockers(readme, contract)
    checklist = {
        "schema_version": 1,
        "phase": "caller-handoff-acceptance-checklist",
        "system_name": system_name,
        "checks": [
            "handoff contains concrete gateway, inbox, SDK, prompt, and manual values",
            "caller AI prompt forbids gateway repository cloning or vendoring",
            "caller engineer README explains responsibility boundary and Go/No-Go rules",
            "caller writes are limited to caller repo, XDG, and explicit operator inboxes",
            "migration assessment, baseline trace, workload contract, preflight, canary, and business validator artifacts are required",
            "final caller status is Go or No-Go",
        ],
        "prompt_quality": {
            "go": not prompt_blockers,
            "blockers": prompt_blockers,
        },
        "human_readme_quality": {
            "go": not readme_blockers,
            "blockers": readme_blockers,
        },
    }
    return {
        "schema_version": 1,
        "phase": "gpucall-caller-handoff-package",
        "system_name": system_name,
        "contract": contract,
        "prompt": prompt,
        "human_readme": readme,
        "checklist": checklist,
    }


def write_handoff_package(config_dir: str | Path, system_name: str, output_dir: str | Path) -> dict[str, Any]:
    package = build_handoff_package(config_dir, system_name, require_concrete=True)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    files = {
        "gpucall-handoff.json": json.dumps(package["contract"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        "CALLER_ENGINEER_README.md": package["human_readme"],
        "caller-ai-onboarding-prompt.md": package["prompt"],
        "acceptance-checklist.json": json.dumps(package["checklist"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    }
    manifest_files: dict[str, dict[str, Any]] = {}
    for name, content in files.items():
        path = destination / name
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)
        manifest_files[name] = {
            "bytes": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    manifest = {
        "schema_version": 1,
        "phase": "gpucall-caller-handoff-package-manifest",
        "system_name": system_name,
        "generated_at": package["contract"]["generated_at"],
        "files": manifest_files,
    }
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    manifest_path = destination / "MANIFEST.json"
    manifest_path.write_text(manifest_text, encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    return {
        "schema_version": 1,
        "phase": "gpucall-caller-handoff-package-write",
        "system_name": system_name,
        "output_dir": str(destination),
        "files": sorted(HANDOFF_PACKAGE_FILES),
        "prompt_quality": package["checklist"]["prompt_quality"],
        "human_readme_quality": package["checklist"]["human_readme_quality"],
        "manifest": manifest,
    }


def _handoff_contract_placeholders(value: Any, *, prefix: str = "") -> list[str]:
    blockers: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            blockers.extend(_handoff_contract_placeholders(item, prefix=f"{prefix}.{key}" if prefix else str(key)))
        return blockers
    if isinstance(value, list):
        for index, item in enumerate(value):
            blockers.extend(_handoff_contract_placeholders(item, prefix=f"{prefix}[{index}]"))
        return blockers
    if isinstance(value, str) and _has_placeholder(value):
        blockers.append(prefix or value)
    return blockers


def _has_placeholder(value: str) -> bool:
    return "<GPUCALL_" in value or value.strip() in {"", "<GPUCALL_BASE_URL>", "<GPUCALL_RECIPE_INBOX>"}


def _required_handoff_values(contract: dict[str, Any]) -> list[Any]:
    return [
        contract["gateway"]["base_url"],
        contract["gateway"]["bootstrap_endpoint"],
        contract["inboxes"]["recipe"],
        contract["inboxes"]["quality_feedback"],
        contract["assets"]["sdk_wheel_url"],
    ]


def _prompt_contains_unresolved_marker(prompt: str) -> bool:
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped in {"TODO", "FIXME"}:
            return True
        if stripped.startswith(("TODO:", "FIXME:", "- TODO", "- FIXME")):
            return True
    return False
