#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[1]
DEFAULT_PLATFORM_VERSION = "0.1.0"
DEFAULT_SOURCE_REF = "main"
DEFAULT_ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"
ISSUE_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")
TEXT_SUFFIXES = {
    "",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".ts",
    ".tsx",
    ".js",
    ".cjs",
    ".mjs",
    ".yaml",
    ".yml",
    ".gitignore",
    ".nvmrc",
}
REQUIRED_CHECKS = [
    "ci",
    "spec-gate",
    "risk-classification",
    "security-scan",
    "ai-gate",
    "release-ready",
]
PROTECTED_PATHS = [
    "packages/contracts/**",
    "db/migrations/**",
    "infra/prod/**",
    "auth/**",
    "api/public/**",
]


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platform",
        description="Bootstrap and maintain the reusable AI development platform baseline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser(
        "bootstrap", help="Apply the platform baseline to a new or existing repository."
    )
    bootstrap.add_argument("--target", default=".", help="Repository path to update.")
    bootstrap.add_argument(
        "--adapter",
        choices=["node-ts"],
        default="node-ts",
        help="Stack adapter to apply.",
    )
    bootstrap.add_argument(
        "--service-name",
        default=None,
        help="Service name used in generated files. Defaults to the target directory name.",
    )
    bootstrap.add_argument(
        "--issue-project-key",
        default="PROJ",
        help="Default Jira issue key prefix.",
    )
    bootstrap.add_argument(
        "--confluence-space",
        default="SPACE",
        help="Default Confluence space identifier.",
    )
    bootstrap.add_argument(
        "--deploy-mode",
        choices=["none", "staging-only", "staging-prod"],
        default="staging-prod",
        help="Default deploy mode recorded in the manifest.",
    )
    bootstrap.add_argument(
        "--source-repo",
        default=None,
        help="GitHub owner/repo for reusable workflows and documentation links.",
    )
    bootstrap.add_argument(
        "--version",
        default=DEFAULT_SOURCE_REF,
        help="Pinned version or ref for reusable workflows.",
    )
    bootstrap.add_argument(
        "--force",
        action="store_true",
        help="Overwrite platform-managed files when they already exist.",
    )
    bootstrap.add_argument(
        "--skip-adapter-starter",
        action="store_true",
        help="Do not add adapter starter files when the repo is empty.",
    )
    bootstrap.set_defaults(func=cmd_bootstrap)

    doctor = subparsers.add_parser(
        "doctor", help="Validate a repository against the platform baseline."
    )
    doctor.add_argument("--target", default=".", help="Repository path to inspect.")
    doctor.set_defaults(func=cmd_doctor)

    upgrade = subparsers.add_parser(
        "upgrade", help="Sync platform-managed files and move the repo to a newer platform ref."
    )
    upgrade.add_argument("--target", default=".", help="Repository path to update.")
    upgrade.add_argument(
        "--to",
        default=DEFAULT_SOURCE_REF,
        help="New platform version or workflow ref.",
    )
    upgrade.add_argument(
        "--source-repo",
        default=None,
        help="GitHub owner/repo for reusable workflows. Defaults to the current manifest value.",
    )
    upgrade.set_defaults(func=cmd_upgrade)

    new_spec = subparsers.add_parser(
        "new-spec", help="Render a new issue spec from the shared template."
    )
    new_spec.add_argument("issue_key", help="Issue key, for example PROJ-123.")
    new_spec.add_argument("--target", default=".", help="Repository path to update.")
    new_spec.add_argument("--title", default="TBD", help="Short issue title.")
    new_spec.add_argument("--owner", default="platform-team", help="Team or person name.")
    new_spec.add_argument("--branch", default=None, help="Branch name. Defaults to feat/<issue>-<slug>.")
    new_spec.add_argument("--force", action="store_true", help="Overwrite an existing spec file.")
    new_spec.set_defaults(func=cmd_new_spec)

    return parser


def cmd_bootstrap(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    service_name = args.service_name or target.name
    source_repo = args.source_repo or infer_default_source_repo()
    context = build_context(
        service_name=service_name,
        adapter=args.adapter,
        issue_project_key=args.issue_project_key,
        confluence_space=args.confluence_space,
        source_repo=source_repo,
        source_ref=args.version,
        deploy_mode=args.deploy_mode,
    )
    manifest_path = target / ".platform" / "platform.yaml"
    if manifest_path.exists() and not args.force:
        fail(
            f"{manifest_path} already exists. Re-run with --force or use `platform upgrade`."
        )

    apply_platform_files(
        target=target,
        context=context,
        overwrite=args.force,
        include_adapter_starter=not args.skip_adapter_starter,
        existing_manifest=None,
    )
    print_summary("Bootstrap complete", target, context)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    manifest_path = target / ".platform" / "platform.yaml"
    errors: list[str] = []
    warnings: list[str] = []

    if not manifest_path.exists():
        errors.append("Missing .platform/platform.yaml.")
        report_issues(errors, warnings)
        return 1

    manifest = load_manifest(manifest_path)
    errors.extend(validate_manifest_shape(manifest))
    adapter = manifest["platform"]["adapter"]
    source_repo = manifest["integrations"]["github"]["source_repo"]
    source_ref = manifest["integrations"]["github"]["workflow_ref"]

    required_files = [
        "AGENTS.md",
        ".claude/settings.json",
        ".mcp.json",
        "docs/specs/ISSUE_SPEC_TEMPLATE.md",
        "ops/ai/check-dangerous-command.sh",
        "ops/ai/post-edit-validate.sh",
        "ops/ai/check-stop-gate.sh",
        "ops/platform/checks.py",
    ]
    required_files.extend(f".github/workflows/{name}.yml" for name in REQUIRED_CHECKS)
    required_files.extend(
        [
            ".github/actions/run-logical-command/action.yml",
            ".github/actions/spec-gate/action.yml",
            ".github/actions/risk-classification/action.yml",
            ".github/actions/security-scan/action.yml",
            ".github/actions/release-ready/action.yml",
        ]
    )
    for rel_path in required_files:
        if not (target / rel_path).exists():
            errors.append(f"Missing required file: {rel_path}")

    tools = ["python3", "gh", "claude", "codex"]
    if adapter == "node-ts":
        tools.extend(["node", "pnpm"])
    for tool in tools:
        if shutil.which(tool) is None:
            errors.append(f"Required tool not found on PATH: {tool}")

    warnings.extend(check_local_auth(adapter))

    for check_name in REQUIRED_CHECKS:
        workflow_path = target / ".github" / "workflows" / f"{check_name}.yml"
        if not workflow_path.exists():
            continue
        workflow_text = workflow_path.read_text(encoding="utf-8")
        expected = f"uses: {source_repo}/.github/workflows/{check_name}.yml@{source_ref}"
        if expected not in workflow_text:
            errors.append(
                f"{workflow_path.relative_to(target)} is not pinned to {source_repo}@{source_ref}."
            )

    if "REPLACE_WITH_GITHUB_OWNER" in source_repo:
        warnings.append(
            "Source repo still uses the placeholder owner. Re-run bootstrap/upgrade with --source-repo."
        )

    if adapter == "node-ts":
        package_json_path = target / "package.json"
        if not package_json_path.exists():
            warnings.append("Node adapter selected but package.json is missing.")
        else:
            package_json = json.loads(package_json_path.read_text(encoding="utf-8"))
            scripts = package_json.get("scripts", {})
            expected_scripts = ["lint", "typecheck", "test:unit", "test:integration", "build"]
            for script_name in expected_scripts:
                if script_name not in scripts:
                    warnings.append(f"package.json is missing the `{script_name}` script.")

    spec_dir = target / manifest["paths"]["spec_dir"]
    if not spec_dir.exists():
        errors.append(f"Spec directory does not exist: {spec_dir.relative_to(target)}")

    report_issues(errors, warnings)
    return 1 if errors else 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    manifest_path = target / ".platform" / "platform.yaml"
    if not manifest_path.exists():
        fail("Target repo is missing .platform/platform.yaml. Run `platform bootstrap` first.")

    manifest = load_manifest(manifest_path)
    source_repo = args.source_repo or manifest["integrations"]["github"]["source_repo"]
    issue_project_key = manifest["issue"]["project_key"]
    confluence_space = manifest["integrations"]["atlassian"]["confluence_space"]
    service_name = target.name
    context = build_context(
        service_name=service_name,
        adapter=manifest["platform"]["adapter"],
        issue_project_key=issue_project_key,
        confluence_space=confluence_space,
        source_repo=source_repo,
        source_ref=args.to,
        deploy_mode=manifest["deploy"]["mode"],
    )
    apply_platform_files(
        target=target,
        context=context,
        overwrite=True,
        include_adapter_starter=False,
        existing_manifest=manifest,
    )
    print_summary("Upgrade complete", target, context)
    return 0


def cmd_new_spec(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    manifest = load_manifest(target / ".platform" / "platform.yaml")
    spec_dir = target / manifest["paths"]["spec_dir"]
    template_path = target / manifest["paths"]["spec_template"]
    spec_dir.mkdir(parents=True, exist_ok=True)
    if not template_path.exists():
        fail(f"Spec template not found: {template_path}")

    issue_key = args.issue_key.upper()
    if not ISSUE_RE.fullmatch(issue_key):
        fail(f"Invalid issue key: {issue_key}")

    slug = slugify(args.title)
    branch = args.branch or f"feat/{issue_key}-{slug}"
    destination = spec_dir / f"{issue_key}.md"
    if destination.exists() and not args.force:
        fail(f"{destination} already exists. Re-run with --force to overwrite.")

    replacements = {
        "<ISSUE_KEY>": issue_key,
        "<title>": args.title,
        "<team/person>": args.owner,
        f"feat/{issue_key}-<slug>": branch,
    }
    template_text = template_path.read_text(encoding="utf-8")
    for old, new in replacements.items():
        template_text = template_text.replace(old, new)
    destination.write_text(template_text, encoding="utf-8")
    print(f"Created {destination}")
    return 0


def build_context(
    *,
    service_name: str,
    adapter: str,
    issue_project_key: str,
    confluence_space: str,
    source_repo: str,
    source_ref: str,
    deploy_mode: str,
) -> dict[str, str]:
    return {
        "SERVICE_NAME": service_name,
        "ADAPTER": adapter,
        "ISSUE_PROJECT_KEY": issue_project_key,
        "CONFLUENCE_SPACE": confluence_space,
        "SOURCE_REPO": source_repo,
        "SOURCE_REF": source_ref,
        "DEPLOY_MODE": deploy_mode,
        "INSTALL_COMMAND": "pnpm install",
        "LINT_COMMAND": "pnpm lint",
        "TYPECHECK_COMMAND": "pnpm typecheck",
        "UNIT_TEST_COMMAND": "pnpm test:unit",
        "INTEGRATION_TEST_COMMAND": "pnpm test:integration",
        "BUILD_COMMAND": "pnpm build",
        "RELEASE_TAG": normalize_version(source_ref),
    }


def apply_platform_files(
    *,
    target: Path,
    context: dict[str, str],
    overwrite: bool,
    include_adapter_starter: bool,
    existing_manifest: dict[str, Any] | None,
) -> None:
    manifest = build_manifest(context, existing_manifest)
    write_manifest(target / ".platform" / "platform.yaml", manifest)

    base_scaffold = REPO_ROOT / "scaffolds" / "base"
    copy_tree(base_scaffold, target, context, overwrite=overwrite)

    copy_tree(REPO_ROOT / "ops" / "platform", target / "ops" / "platform", context, overwrite=overwrite)
    copy_tree(
        REPO_ROOT / ".github" / "actions",
        target / ".github" / "actions",
        context,
        overwrite=overwrite,
    )

    if include_adapter_starter:
        package_json_exists = (target / "package.json").exists()
        if not package_json_exists:
            adapter_path = REPO_ROOT / "scaffolds" / "adapters" / context["ADAPTER"]
            copy_tree(adapter_path, target, context, overwrite=overwrite)

    make_executable(
        target / "ops" / "ai" / "check-dangerous-command.sh",
        target / "ops" / "ai" / "post-edit-validate.sh",
        target / "ops" / "ai" / "check-stop-gate.sh",
        target / "ops" / "platform" / "checks.py",
    )


def build_manifest(
    context: dict[str, str], existing_manifest: dict[str, Any] | None
) -> dict[str, Any]:
    manifest = {
        "platform": {
            "version": context["RELEASE_TAG"],
            "adapter": context["ADAPTER"],
        },
        "issue": {
            "project_key": context["ISSUE_PROJECT_KEY"],
        },
        "paths": {
            "spec_dir": "docs/specs",
            "spec_template": "docs/specs/ISSUE_SPEC_TEMPLATE.md",
        },
        "commands": {
            "install": context["INSTALL_COMMAND"],
            "lint": context["LINT_COMMAND"],
            "typecheck": context["TYPECHECK_COMMAND"],
            "test_unit": context["UNIT_TEST_COMMAND"],
            "test_integration": context["INTEGRATION_TEST_COMMAND"],
            "build": context["BUILD_COMMAND"],
        },
        "risk": {
            "protected_paths": PROTECTED_PATHS,
        },
        "checks": {
            "enabled": REQUIRED_CHECKS,
        },
        "deploy": {
            "mode": context["DEPLOY_MODE"],
        },
        "integrations": {
            "atlassian": {
                "mcp_url": DEFAULT_ATLASSIAN_MCP_URL,
                "auth_mode": "oauth2.1",
                "project_scoped": True,
                "api_token_opt_in": False,
                "confluence_space": context["CONFLUENCE_SPACE"],
            },
            "github": {
                "source_repo": context["SOURCE_REPO"],
                "workflow_ref": context["SOURCE_REF"],
                "template_repository": True,
            },
        },
    }
    if existing_manifest:
        manifest["commands"] = existing_manifest.get("commands", manifest["commands"])
        manifest["risk"] = existing_manifest.get("risk", manifest["risk"])
        manifest["checks"] = existing_manifest.get("checks", manifest["checks"])
        manifest["deploy"] = existing_manifest.get("deploy", manifest["deploy"])
        manifest["integrations"]["atlassian"] = {
            **manifest["integrations"]["atlassian"],
            **existing_manifest.get("integrations", {}).get("atlassian", {}),
        }
        manifest["integrations"]["github"] = {
            **manifest["integrations"]["github"],
            **existing_manifest.get("integrations", {}).get("github", {}),
            "source_repo": context["SOURCE_REPO"],
            "workflow_ref": context["SOURCE_REF"],
        }
        manifest["issue"] = {
            **manifest["issue"],
            **existing_manifest.get("issue", {}),
        }
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"Manifest not found: {path}")
    except json.JSONDecodeError as exc:
        fail(f"Manifest must be JSON-compatible YAML: {path} ({exc})")
    raise AssertionError("unreachable")


def check_local_auth(adapter: str) -> list[str]:
    warnings: list[str] = []

    claude_status = run_optional(["claude", "auth", "status"])
    if claude_status and claude_status.returncode == 0:
        try:
            payload = json.loads(claude_status.stdout)
        except json.JSONDecodeError:
            payload = {}
        if not payload.get("loggedIn", False):
            warnings.append("Claude is installed but not logged in. Run `claude auth login --claudeai`.")
    else:
        warnings.append("Claude auth status could not be verified. Run `claude auth status`.")

    codex_status = run_optional(["codex", "login", "status"])
    if codex_status and codex_status.returncode == 0:
        codex_output = f"{codex_status.stdout}\n{codex_status.stderr}".lower()
        if "logged in" not in codex_output or "chatgpt" not in codex_output:
            warnings.append("Codex is not using ChatGPT login. Run `codex logout` then `codex login`.")
    else:
        warnings.append("Codex login status could not be verified. Run `codex login status`.")

    if adapter == "node-ts" and os.environ.get("OPENAI_API_KEY"):
        warnings.append(
            "OPENAI_API_KEY is set in the local shell. This baseline expects login-based Codex usage, not manual API keys."
        )

    return warnings


def run_optional(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False)
    except (FileNotFoundError, PermissionError, OSError):
        return None


def copy_tree(
    source: Path,
    destination: Path,
    context: dict[str, str],
    *,
    overwrite: bool,
) -> None:
    if source.is_file():
        copy_file(source, destination, context, overwrite=overwrite)
        return
    for file_path in sorted(source.rglob("*")):
        if file_path.is_dir():
            continue
        relative_path = file_path.relative_to(source)
        copy_file(file_path, destination / relative_path, context, overwrite=overwrite)


def copy_file(source: Path, destination: Path, context: dict[str, str], *, overwrite: bool) -> None:
    if destination.exists() and not overwrite:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)

    if is_text_file(source):
        text = source.read_text(encoding="utf-8")
        text = render_template(text, context)
        destination.write_text(text, encoding="utf-8")
    else:
        shutil.copy2(source, destination)


def is_text_file(path: Path) -> bool:
    if path.suffix in TEXT_SUFFIXES:
        return True
    if path.name in {"AGENTS.md", ".mcp.json", ".gitignore", ".nvmrc"}:
        return True
    return False


def render_template(text: str, context: dict[str, str]) -> str:
    for key, value in context.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


def normalize_version(value: str) -> str:
    if re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        return value[1:] if value.startswith("v") else value
    if value == "main":
        return DEFAULT_PLATFORM_VERSION
    return value


def infer_default_source_repo() -> str:
    if os.environ.get("PLATFORM_SOURCE_REPO"):
        return os.environ["PLATFORM_SOURCE_REPO"]

    account = None
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        result = None
    if result and result.stdout:
        match = re.search(r"account ([A-Za-z0-9-]+)", result.stdout)
        if match:
            account = match.group(1)
    if not account and result and result.stderr:
        match = re.search(r"account ([A-Za-z0-9-]+)", result.stderr)
        if match:
            account = match.group(1)

    repo_name = REPO_ROOT.name
    if account:
        return f"{account}/{repo_name}"
    return f"REPLACE_WITH_GITHUB_OWNER/{repo_name}"


def make_executable(*paths: Path) -> None:
    for path in paths:
        if not path.exists():
            continue
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "change"


def validate_manifest_shape(manifest: dict[str, Any]) -> list[str]:
    required = [
        ("platform", dict),
        ("issue", dict),
        ("paths", dict),
        ("commands", dict),
        ("risk", dict),
        ("checks", dict),
        ("deploy", dict),
        ("integrations", dict),
    ]
    errors: list[str] = []
    for key, expected_type in required:
        if key not in manifest:
            errors.append(f"Manifest is missing `{key}`.")
        elif not isinstance(manifest[key], expected_type):
            errors.append(f"Manifest key `{key}` must be an object.")
    return errors


def print_summary(title: str, target: Path, context: dict[str, str]) -> None:
    print(title)
    print(f"- target: {target}")
    print(f"- adapter: {context['ADAPTER']}")
    print(f"- source repo: {context['SOURCE_REPO']}")
    print(f"- workflow ref: {context['SOURCE_REF']}")


def report_issues(errors: list[str], warnings: list[str]) -> None:
    if errors:
        print("Errors:")
        for item in errors:
            print(f"- {item}")
    if warnings:
        print("Warnings:")
        for item in warnings:
            print(f"- {item}")
    if not errors and not warnings:
        print("Doctor found no issues.")


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
