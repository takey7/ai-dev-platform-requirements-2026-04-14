#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path.cwd()
ISSUE_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")
REQUIRED_SECTIONS = [
    "Summary",
    "Objective",
    "Acceptance criteria",
    "Scope",
    "Compatibility impact",
    "Migration plan",
    "Rollout strategy",
    "Rollback",
    "Observability",
    "Test plan",
    "Risks",
    "Release notes draft",
]
DEFAULT_BASE_REF = "main"


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shared local and CI checks for the platform baseline.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    command_info = subparsers.add_parser("command-info")
    command_info.add_argument("--manifest", required=True)
    command_info.add_argument("--name", required=True)
    command_info.add_argument("--github-output", default=None)
    command_info.set_defaults(func=cmd_command_info)

    spec_gate = subparsers.add_parser("spec-gate")
    spec_gate.add_argument("--manifest", required=True)
    spec_gate.add_argument("--github-output", default=None)
    spec_gate.set_defaults(func=cmd_spec_gate)

    risk = subparsers.add_parser("risk-classify")
    risk.add_argument("--manifest", required=True)
    risk.add_argument("--base-ref", default=DEFAULT_BASE_REF)
    risk.add_argument("--github-output", default=None)
    risk.set_defaults(func=cmd_risk_classify)

    security = subparsers.add_parser("security-scan")
    security.add_argument("--manifest", required=True)
    security.add_argument("--base-ref", default=DEFAULT_BASE_REF)
    security.add_argument("--github-output", default=None)
    security.set_defaults(func=cmd_security_scan)

    release_ready = subparsers.add_parser("release-ready")
    release_ready.add_argument("--manifest", required=True)
    release_ready.add_argument("--github-output", default=None)
    release_ready.set_defaults(func=cmd_release_ready)

    dangerous = subparsers.add_parser("hook-dangerous")
    dangerous.set_defaults(func=cmd_hook_dangerous)

    post_edit = subparsers.add_parser("hook-post-edit")
    post_edit.add_argument("--manifest", default=".platform/platform.yaml")
    post_edit.set_defaults(func=cmd_hook_post_edit)

    stop = subparsers.add_parser("hook-stop")
    stop.add_argument("--manifest", default=".platform/platform.yaml")
    stop.set_defaults(func=cmd_hook_stop)

    return parser


def cmd_command_info(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    command_map = {
        "install": "install",
        "lint": "lint",
        "typecheck": "typecheck",
        "test-unit": "test_unit",
        "test-integration": "test_integration",
        "build": "build",
    }
    manifest_key = command_map[args.name]
    command = manifest["commands"].get(manifest_key, ":")
    adapter = manifest["platform"]["adapter"]
    outputs = {
        "adapter": adapter,
        "command": command,
        "node_version": "20" if adapter == "node-ts" else "",
        "skip": str(command.strip() in {"", ":"}).lower(),
    }
    write_outputs(outputs, args.github_output)
    print(json.dumps(outputs, ensure_ascii=False))
    return 0


def cmd_spec_gate(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    spec_path, issue_key = locate_relevant_spec(manifest)
    if spec_path is None:
        print("No issue spec could be identified. Create one with `platform new-spec <ISSUE_KEY>`.", file=sys.stderr)
        return 1

    text = spec_path.read_text(encoding="utf-8")
    missing = [section for section in REQUIRED_SECTIONS if section.lower() not in text.lower()]
    outputs = {
        "issue_key": issue_key or "",
        "spec_path": str(spec_path),
        "missing_sections": json.dumps(missing, ensure_ascii=False),
    }
    write_outputs(outputs, args.github_output)
    if missing:
        print(f"Spec is missing required sections in {spec_path}:", file=sys.stderr)
        for section in missing:
            print(f"- {section}", file=sys.stderr)
        return 1
    print(f"Spec gate passed: {spec_path}")
    return 0


def cmd_risk_classify(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    changed_files = get_changed_files(args.base_ref)
    protected = manifest["risk"].get("protected_paths", [])
    high_risk = [path for path in changed_files if matches_any(path, protected)]
    docs_only = changed_files and all(path.startswith("docs/") or path.endswith(".md") for path in changed_files)
    risk_level = "low"
    labels = ["risk:low"]
    if high_risk:
        risk_level = "high"
        labels = ["risk:high"]
    elif changed_files and not docs_only:
        risk_level = "medium"
        labels = ["risk:medium"]

    spec_path, _ = locate_relevant_spec(manifest)
    breaking_change = False
    if spec_path and spec_path.exists():
        spec_text = spec_path.read_text(encoding="utf-8").lower()
        breaking_change = "breaking change: `yes" in spec_text or "breaking change: yes" in spec_text
        if breaking_change and "breaking-change" not in labels:
            labels.append("breaking-change")
    if high_risk:
        labels.extend(high_risk_labels(high_risk))

    outputs = {
        "risk_level": risk_level,
        "labels": json.dumps(sorted(set(labels)), ensure_ascii=False),
        "breaking_change": str(breaking_change).lower(),
        "changed_files": json.dumps(changed_files, ensure_ascii=False),
    }
    write_outputs(outputs, args.github_output)
    print(json.dumps(outputs, ensure_ascii=False))
    return 0


def cmd_security_scan(args: argparse.Namespace) -> int:
    changed_files = get_changed_files(args.base_ref)
    findings: list[str] = []
    patterns = [
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN [A-Z ]+PRIVATE KEY-----",
        r'(?i)(password|secret|api[_-]?key)\s*[:=]\s*["\'][^"\']{8,}["\']',
    ]
    for rel_path in changed_files:
        file_path = ROOT / rel_path
        if not file_path.exists() or file_path.is_dir():
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in patterns:
            if re.search(pattern, content):
                findings.append(f"{rel_path}: matched `{pattern}`")
    outputs = {
        "findings": json.dumps(findings, ensure_ascii=False),
        "passed": str(not findings).lower(),
    }
    write_outputs(outputs, args.github_output)
    if findings:
        print("Security scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("Security scan passed")
    return 0


def cmd_release_ready(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    spec_path, _ = locate_relevant_spec(manifest)
    errors: list[str] = []
    if manifest["deploy"]["mode"] not in {"none", "staging-only", "staging-prod"}:
        errors.append("deploy.mode must be one of none, staging-only, staging-prod.")
    if not spec_path or not spec_path.exists():
        errors.append("No issue spec found for release-ready validation.")
    else:
        spec_text = spec_path.read_text(encoding="utf-8").lower()
        if "rollback trigger:" not in spec_text or "rollback steps:" not in spec_text:
            errors.append("Spec is missing explicit rollback fields.")
        if "staged plan:" not in spec_text:
            errors.append("Spec is missing a staged rollout plan.")
    outputs = {
        "passed": str(not errors).lower(),
        "errors": json.dumps(errors, ensure_ascii=False),
    }
    write_outputs(outputs, args.github_output)
    if errors:
        print("Release-ready check failed:", file=sys.stderr)
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        return 1
    print("Release-ready check passed")
    return 0


def cmd_hook_dangerous(args: argparse.Namespace) -> int:
    payload = sys.stdin.read()
    patterns = [
        r"rm\s+-rf\s+/",
        r"git\s+push\s+--force",
        r"terraform\s+apply.*prod",
        r"kubectl\s+delete.*prod",
        r"(npm|pnpm|yarn)\s+publish",
    ]
    for pattern in patterns:
        if re.search(pattern, payload):
            print(f"[platform] blocked dangerous command matching `{pattern}`", file=sys.stderr)
            return 1
    return 0


def cmd_hook_post_edit(args: argparse.Namespace) -> int:
    manifest_path = ROOT / args.manifest
    if not manifest_path.exists():
        return 0
    marker_path = ROOT / ".platform" / ".last-validation.json"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "timestamp": int(time.time()),
        "mode": "post-edit",
        "status": "recorded",
    }
    marker_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("[platform] post-edit validation marker updated")
    return 0


def cmd_hook_stop(args: argparse.Namespace) -> int:
    manifest_path = ROOT / args.manifest
    if not manifest_path.exists():
        print("[platform] missing .platform/platform.yaml", file=sys.stderr)
        return 1
    marker_path = ROOT / ".platform" / ".last-validation.json"
    if not marker_path.exists():
        print("[platform] local validation has not run yet.", file=sys.stderr)
        return 1
    manifest = load_manifest(manifest_path)
    spec_path, _ = locate_relevant_spec(manifest)
    if not spec_path or not spec_path.exists():
        print("[platform] no issue spec found.", file=sys.stderr)
        return 1
    return 0


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def locate_relevant_spec(manifest: dict[str, Any]) -> tuple[Path | None, str | None]:
    spec_dir = ROOT / manifest["paths"]["spec_dir"]
    if not spec_dir.exists():
        return None, None
    branch_name = os.environ.get("GITHUB_HEAD_REF") or git_current_branch()
    issue_key = None
    if branch_name:
        match = ISSUE_RE.search(branch_name)
        if match:
            issue_key = match.group(0)
    if issue_key:
        direct_path = spec_dir / f"{issue_key}.md"
        if direct_path.exists():
            return direct_path, issue_key
    candidates = sorted(
        [
            path
            for path in spec_dir.glob("*.md")
            if path.name != "ISSUE_SPEC_TEMPLATE.md"
        ]
    )
    if not candidates:
        return None, issue_key
    return candidates[0], issue_key


def get_changed_files(base_ref: str) -> list[str]:
    fetch_base_ref(base_ref)
    commands = [
        ["git", "diff", "--name-only", f"origin/{base_ref}...HEAD"],
        ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
        ["git", "diff", "--name-only"],
    ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return []


def fetch_base_ref(base_ref: str) -> None:
    subprocess.run(
        ["git", "fetch", "--no-tags", "--depth=1", "origin", base_ref],
        capture_output=True,
        text=True,
        check=False,
    )


def git_current_branch() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return None if branch == "HEAD" else branch


def matches_any(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        regex = "^" + re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "$"
        if re.match(regex, path):
            return True
    return False


def high_risk_labels(paths: list[str]) -> list[str]:
    labels = []
    if any(path.startswith("db/migrations/") for path in paths):
        labels.append("db-migration")
    if any(path.startswith("infra/prod/") for path in paths):
        labels.append("infra-prod")
    if any(path.startswith("auth/") for path in paths):
        labels.append("security-sensitive")
    if any(path.startswith("api/public/") or path.startswith("packages/contracts/") for path in paths):
        labels.append("breaking-change")
    labels.extend(["rollback-ready", "needs-canary"])
    return labels


def write_outputs(values: dict[str, str], github_output: str | None) -> None:
    if not github_output:
        return
    output_path = Path(github_output)
    with output_path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}<<EOF\n{value}\nEOF\n")


if __name__ == "__main__":
    raise SystemExit(main())
