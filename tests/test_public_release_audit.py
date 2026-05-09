from __future__ import annotations

import re
import subprocess
from pathlib import Path


def test_tracked_files_do_not_contain_private_operator_artifacts() -> None:
    root = Path(__file__).resolve().parents[1]
    files = subprocess.check_output(["git", "ls-files"], cwd=root, text=True).splitlines()
    ignored = {
        "docs/PUBLIC_RELEASE_CHECKLIST.md",
        "scripts/public_release_audit.sh",
        "tests/test_public_release_audit.py",
    }
    patterns = [
        r"100" + r"\.91" + r"\.94" + r"\.11",
        r"152" + r"\.53" + r"\.228" + r"\.117",
        r"vllm-[a-z0-9]{12,}",
        "RUNPOD_ENDPOINT_ID_PLACEHOLDER",
        "RUNPOD_ENDPOINT_ID_PLACEHOLDER",
        "root@" + "gpucall.example.internal",
        r"\broot@",
        "news-" + "system",
        "/Users/" + "tamotsu",
        "PRIVATE KEY",
        r"sk-[A-Za-z0-9]",
        r"AKIA[0-9A-Z]{16}",
    ]
    compiled = re.compile("|".join(patterns))

    findings: list[str] = []
    for relative in files:
        if relative in ignored:
            continue
        path = root / relative
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if compiled.search(line):
                findings.append(f"{relative}:{lineno}:{line}")

    assert findings == []
