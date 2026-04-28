#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any
from urllib import error, parse, request


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[1]
if str(SCRIPT_PATH.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PATH.parent))
import platform_orchestrator

DEFAULT_PLATFORM_VERSION = "0.1.0"
DEFAULT_SOURCE_REF = "main"
DEFAULT_ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"
DEFAULT_PROJECTS_ROOT = str((Path.home() / "workspaces").expanduser())
DEFAULT_ADAPTER = "node-ts"
DEFAULT_DEPLOY_MODE = "staging-prod"
DEFAULT_LAUNCH_MODE = "tmux"
DEFAULT_CODEX_REVIEW_MODE = "auto_required"
DEFAULT_CODEX_REVIEW_AUTHORS = ("codex", "codex[bot]", "chatgpt-codex-connector")
CODEX_CODE_REVIEW_SETTINGS_URL = "https://chatgpt.com/codex/settings/code-review"
OPENAI_CODEX_GITHUB_DOC_URL = "https://developers.openai.com/codex/integrations/github"
OPENAI_CODEX_CLOUD_DOC_URL = "https://developers.openai.com/codex/cloud"
CONFIG_VERSION = 1
USER_CONFIG_DIRNAME = "ai-dev-platform"
USER_CONFIG_FILENAME = "config.json"
JIRA_PROJECT_TYPE_KEY = "software"
JIRA_KANBAN_TEMPLATE_KEY = "com.pyxis.greenhopper.jira:gh-simplified-agility-kanban"
JIRA_ASSIGNEE_TYPE = "PROJECT_LEAD"
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


class PlatformCommandError(RuntimeError):
    pass


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
        default=DEFAULT_DEPLOY_MODE,
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

    configure = subparsers.add_parser(
        "configure",
        help="Store user-level defaults for create-project without changing repo manifests.",
    )
    configure.add_argument("--github-owner", default=None, help="Default GitHub owner or org.")
    configure.add_argument("--projects-root", default=None, help="Default root directory for new repos.")
    configure.add_argument("--source-repo", default=None, help="Default platform source repo.")
    configure.add_argument("--source-ref", default=None, help="Default platform version or ref.")
    configure.add_argument(
        "--adapter",
        choices=["node-ts"],
        default=None,
        help="Default adapter for create-project.",
    )
    configure.add_argument(
        "--deploy-mode",
        choices=["none", "staging-only", "staging-prod"],
        default=None,
        help="Default deploy mode for bootstrap during create-project.",
    )
    configure.add_argument("--jira-site-url", default=None, help="Atlassian Cloud site URL.")
    configure.add_argument("--jira-admin-email", default=None, help="Admin email used for Jira provisioning.")
    configure.add_argument(
        "--launch-mode",
        choices=["tmux", "none"],
        default=None,
        help="Default post-create launch mode.",
    )
    configure.set_defaults(func=cmd_configure)

    create_project = subparsers.add_parser(
        "create-project",
        help="Create a new GitHub repo, bootstrap it, provision Jira Kanban, and launch local agents.",
    )
    create_project.add_argument("project_name", help="Human-readable project name.")
    create_project.add_argument("--github-owner", default=None, help="Override GitHub owner or org.")
    create_project.add_argument("--root", default=None, help="Override local projects root.")
    create_project.add_argument("--repo-name", default=None, help="Override generated repository name.")
    create_project.add_argument("--jira-key", default=None, help="Override generated Jira project key.")
    create_project.add_argument("--jira-name", default=None, help="Override Jira project display name.")
    create_project.add_argument("--confluence-space", default=None, help="Override Confluence space key.")
    create_project.add_argument("--source-repo", default=None, help="Override platform source repo.")
    create_project.add_argument("--version", default=None, help="Override platform version or ref.")
    create_project.add_argument(
        "--adapter",
        choices=["node-ts"],
        default=None,
        help="Override adapter for the new repo.",
    )
    create_project.add_argument(
        "--launch-mode",
        choices=["tmux", "none"],
        default=None,
        help="Override post-create launch mode.",
    )
    create_project.add_argument(
        "--keep-partials",
        action="store_true",
        help="Keep local and remote assets when a pre-Jira phase fails.",
    )
    create_project.set_defaults(func=cmd_create_project)

    setup_repo = subparsers.add_parser(
        "setup-repo",
        help="Provision GitHub/Jira and bootstrap an existing local repository.",
    )
    setup_repo.add_argument("--target", default=".", help="Existing repository path to set up.")
    setup_repo.add_argument(
        "--project-name",
        default=None,
        help="Human-readable project name. Defaults to the target directory name.",
    )
    setup_repo.add_argument("--github-owner", default=None, help="Override GitHub owner or org.")
    setup_repo.add_argument("--repo-name", default=None, help="Override generated repository name.")
    setup_repo.add_argument("--jira-key", default=None, help="Override generated Jira project key.")
    setup_repo.add_argument("--jira-name", default=None, help="Override Jira project display name.")
    setup_repo.add_argument("--confluence-space", default=None, help="Override Confluence space key.")
    setup_repo.add_argument("--source-repo", default=None, help="Override platform source repo.")
    setup_repo.add_argument("--version", default=None, help="Override platform version or ref.")
    setup_repo.add_argument(
        "--adapter",
        choices=["node-ts"],
        default=None,
        help="Override adapter for this repo.",
    )
    setup_repo.add_argument(
        "--deploy-mode",
        choices=["none", "staging-only", "staging-prod"],
        default=None,
        help="Override deploy mode recorded in the manifest.",
    )
    setup_repo.add_argument(
        "--launch-mode",
        choices=["tmux", "none"],
        default=None,
        help="Override post-setup launch mode.",
    )
    setup_repo.add_argument(
        "--skip-github-create",
        action="store_true",
        help="Do not create or attach a GitHub private repo. Use the existing origin if any.",
    )
    setup_repo.add_argument(
        "--skip-jira-create",
        action="store_true",
        help="Do not create a Jira Kanban project. Requires a usable --jira-key.",
    )
    setup_repo.add_argument(
        "--skip-register",
        action="store_true",
        help="Do not register the repo with the polling orchestrator.",
    )
    setup_repo.add_argument(
        "--no-commit-push",
        action="store_true",
        help="Apply files locally but do not commit or push.",
    )
    setup_repo.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow commit/push even when an existing committed repo had local changes before setup.",
    )
    setup_repo.add_argument(
        "--force",
        action="store_true",
        help="Overwrite platform-managed files when they already exist.",
    )
    setup_repo.set_defaults(func=cmd_setup_repo)

    doctor = subparsers.add_parser(
        "doctor", help="Validate a repository against the platform baseline."
    )
    doctor.add_argument("--target", default=".", help="Repository path to inspect.")
    doctor.set_defaults(func=cmd_doctor)

    codex_review = subparsers.add_parser(
        "codex-review",
        help="Inspect and guide GitHub/Codex PR review setup for a repository.",
    )
    codex_review.add_argument("--target", default=".", help="Repository path to inspect.")
    codex_review.add_argument(
        "--open-settings",
        action="store_true",
        help="Open the Codex code review settings page in the browser.",
    )
    codex_review.add_argument(
        "--request-pr",
        type=int,
        default=None,
        help="Post `@codex review` on the given PR if it has not already been requested.",
    )
    codex_review.add_argument(
        "--force-comment",
        action="store_true",
        help="Post `@codex review` even if a fallback request comment already exists.",
    )
    codex_review.set_defaults(func=cmd_codex_review)

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

    platform_orchestrator.register_commands(subparsers)

    return parser


def cmd_bootstrap(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    manifest_path = target / ".platform" / "platform.yaml"
    if manifest_path.exists() and not args.force:
        fail(
            f"{manifest_path} already exists. Re-run with --force or use `platform upgrade`."
        )
    source_repo = args.source_repo or infer_default_source_repo()
    context = bootstrap_repository(
        target=target,
        adapter=args.adapter,
        service_name=args.service_name or target.name,
        issue_project_key=args.issue_project_key,
        confluence_space=args.confluence_space,
        source_repo=source_repo,
        source_ref=args.version,
        deploy_mode=args.deploy_mode,
        overwrite=args.force,
        include_adapter_starter=not args.skip_adapter_starter,
        existing_manifest=None,
    )
    print_summary("Bootstrap complete", target, context)
    return 0


def cmd_configure(args: argparse.Namespace) -> int:
    config = load_user_config()

    if args.github_owner is not None:
        config["github_owner"] = args.github_owner
    if args.projects_root is not None:
        config["projects_root"] = str(Path(args.projects_root).expanduser().resolve())
    if args.source_repo is not None:
        config["source_repo"] = args.source_repo
        if args.source_ref is None:
            config["source_ref"] = resolve_latest_source_ref(args.source_repo)
    if args.source_ref is not None:
        config["source_ref"] = args.source_ref
    if args.adapter is not None:
        config["adapter"] = args.adapter
    if args.deploy_mode is not None:
        config["deploy_mode"] = args.deploy_mode
    if args.jira_site_url is not None:
        config.setdefault("jira", {})["site_url"] = normalize_site_url(args.jira_site_url)
    if args.jira_admin_email is not None:
        config.setdefault("jira", {})["admin_email"] = args.jira_admin_email
    if args.launch_mode is not None:
        config["launch_mode"] = args.launch_mode

    if not config.get("github_owner"):
        config["github_owner"] = infer_active_github_owner() or ""
    if not config.get("source_repo"):
        config["source_repo"] = infer_default_source_repo()
    if not config.get("source_ref"):
        config["source_ref"] = resolve_latest_source_ref(config["source_repo"])

    save_user_config(config)
    print(f"Saved config: {user_config_path()}")
    print(json.dumps(config, indent=2, ensure_ascii=False))
    return 0


def cmd_create_project(args: argparse.Namespace) -> int:
    config = load_user_config()
    settings = resolve_create_project_settings(args, config)

    remote_created = False
    repo_cloned = False
    jira_created = False
    session_name: str | None = None

    try:
        preflight_create_project(settings)

        settings["jira_lead_account_id"] = jira_current_user_account_id(
            site_url=settings["jira_site_url"],
            admin_email=settings["jira_admin_email"],
            api_token=settings["jira_api_token"],
        )
        settings["jira_key"] = jira_valid_project_key(
            site_url=settings["jira_site_url"],
            admin_email=settings["jira_admin_email"],
            api_token=settings["jira_api_token"],
            requested_key=settings["jira_key"],
        )
        settings["jira_name"] = jira_valid_project_name(
            site_url=settings["jira_site_url"],
            admin_email=settings["jira_admin_email"],
            api_token=settings["jira_api_token"],
            requested_name=settings["jira_name"],
        )
        if not settings.get("confluence_space"):
            settings["confluence_space"] = settings["jira_key"]

        create_github_repository(settings)
        remote_created = True

        clone_github_repository(settings)
        repo_cloned = True

        prepare_local_git_repository(settings["target"])
        context = bootstrap_repository(
            target=settings["target"],
            adapter=settings["adapter"],
            service_name=settings["service_name"],
            issue_project_key=settings["jira_key"],
            confluence_space=settings["confluence_space"],
            source_repo=settings["source_repo"],
            source_ref=settings["source_ref"],
            deploy_mode=settings["deploy_mode"],
            overwrite=False,
            include_adapter_starter=True,
            existing_manifest=None,
        )
        install_adapter_dependencies(settings["target"], settings["adapter"])
        ensure_doctor_passes(settings["target"])
        commit_and_push_initial_repo(settings["target"])

        settings["jira_project"] = create_jira_project(settings)
        jira_created = True

        if settings["launch_mode"] == "tmux":
            session_name = launch_tmux_workspace(settings["target"], settings["repo_name"])

        print_create_project_summary(settings, context, session_name)
        return 0
    except PlatformCommandError as exc:
        if remote_created and not jira_created and not args.keep_partials:
            cleanup_partial_project(settings)
        print(str(exc), file=sys.stderr)
        if remote_created and not jira_created and not args.keep_partials:
            print("Rolled back local directory and GitHub repository created before Jira provisioning.", file=sys.stderr)
        elif jira_created:
            print(
                "GitHub repo and Jira project were created before the failure. "
                "Keeping partial assets for manual follow-up.",
                file=sys.stderr,
            )
        return 1


def cmd_setup_repo(args: argparse.Namespace) -> int:
    config = load_user_config()
    settings = resolve_setup_repo_settings(args, config)
    session_name: str | None = None

    try:
        preflight_setup_repo(settings)
        ensure_git_repository(settings["target"])

        had_commits = git_has_commits(settings["target"])
        dirty_before = git_status_porcelain(settings["target"])
        if not settings["create_github"] and settings["commit_and_push"] and not git_remote_url(settings["target"]):
            raise PlatformCommandError(
                "--skip-github-create requires an existing origin when commit/push is enabled. "
                "Configure origin first or re-run with --no-commit-push."
            )
        if settings["commit_and_push"] and had_commits and dirty_before and not settings["allow_dirty"]:
            raise PlatformCommandError(
                "Target repo has uncommitted changes before setup. Commit/stash them first, "
                "or re-run with --allow-dirty if you intentionally want setup to commit them."
            )

        if settings["create_jira"]:
            settings["jira_lead_account_id"] = jira_current_user_account_id(
                site_url=settings["jira_site_url"],
                admin_email=settings["jira_admin_email"],
                api_token=settings["jira_api_token"],
            )
            settings["jira_key"] = jira_valid_project_key(
                site_url=settings["jira_site_url"],
                admin_email=settings["jira_admin_email"],
                api_token=settings["jira_api_token"],
                requested_key=settings["jira_key"],
            )
            settings["jira_name"] = jira_valid_project_name(
                site_url=settings["jira_site_url"],
                admin_email=settings["jira_admin_email"],
                api_token=settings["jira_api_token"],
                requested_name=settings["jira_name"],
            )
            if not settings.get("confluence_space"):
                settings["confluence_space"] = settings["jira_key"]
            settings["jira_project"] = create_jira_project(settings)
        elif not settings.get("confluence_space"):
            settings["confluence_space"] = settings["jira_key"]

        if settings["create_github"]:
            ensure_github_remote(settings)

        context = bootstrap_repository(
            target=settings["target"],
            adapter=settings["adapter"],
            service_name=settings["service_name"],
            issue_project_key=settings["jira_key"],
            confluence_space=settings["confluence_space"],
            source_repo=settings["source_repo"],
            source_ref=settings["source_ref"],
            deploy_mode=settings["deploy_mode"],
            overwrite=settings["force"],
            include_adapter_starter=True,
            existing_manifest=None,
        )
        ensure_doctor_passes(settings["target"])

        if settings["commit_and_push"]:
            commit_and_push_setup_repo(settings["target"])

        if settings["register_orchestrator"]:
            ensure_orchestrator_project_root(settings["target"].parent)
            register_orchestrator_project(settings["target"])

        if settings["launch_mode"] == "tmux":
            session_name = launch_tmux_workspace(settings["target"], settings["repo_name"])

        print_setup_repo_summary(settings, context, session_name)
        return 0
    except (PlatformCommandError, platform_orchestrator.OrchestratorError) as exc:
        print(str(exc), file=sys.stderr)
        print(
            "Partial assets are kept. Re-run `platform setup-repo --target <repo> --force` "
            "after resolving the issue, or clean up GitHub/Jira manually if needed.",
            file=sys.stderr,
        )
        return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    errors, warnings = inspect_target(target)
    report_issues(errors, warnings)
    return 1 if errors else 0


def cmd_codex_review(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    if not (target / ".git").exists():
        fail(f"{target} is not a Git repository.")

    repo = infer_repo_full_name(target) or "<unknown>"
    if args.open_settings:
        webbrowser.open(CODEX_CODE_REVIEW_SETTINGS_URL)

    if args.request_pr is not None:
        request_codex_review_comment(
            target=target,
            pr_number=args.request_pr,
            force=args.force_comment,
        )

    print(f"Repository: {repo}")
    print("Codex review setup")
    print(f"- settings: {CODEX_CODE_REVIEW_SETTINGS_URL}")
    print(f"- docs: {OPENAI_CODEX_GITHUB_DOC_URL}")
    print("- required UI step: in Codex settings, connect GitHub, enable Code review for this repository, and enable Automatic reviews if desired")
    print("- terminal can verify/request reviews, but it cannot toggle the ChatGPT/Codex repository setting")

    auth_warnings = check_local_auth("node-ts")
    if auth_warnings:
        print("Auth warnings:")
        for warning in auth_warnings:
            print(f"- {warning}")
    else:
        print("Auth: gh/Codex/Claude local login checks passed")

    cloud_status = run_optional(["codex", "cloud", "list", "--json", "--limit", "1"], cwd=target)
    if cloud_status and cloud_status.returncode == 0:
        print("Codex cloud: reachable from CLI")
    else:
        print("Codex cloud: not verified; open Codex web and connect GitHub/repo access")

    result = run_optional(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--limit",
            "5",
            "--json",
            "number,url,reviews,comments,reviewDecision",
        ],
        cwd=target,
    )
    if not result or result.returncode != 0 or not result.stdout.strip():
        print("PR review health: could not read PRs with gh")
        return 0
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("PR review health: could not parse gh PR output")
        return 0

    warnings = codex_review_health_from_prs(prs)
    if warnings:
        print("PR review health warnings:")
        for warning in warnings:
            print(f"- {warning}")
    else:
        print("PR review health: Codex review artifact found in recent PRs")

    if isinstance(prs, list) and prs:
        latest = prs[0]
        print(f"Latest PR: #{latest.get('number')} {latest.get('url')}")
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    manifest_path = target / ".platform" / "platform.yaml"
    if not manifest_path.exists():
        fail("Target repo is missing .platform/platform.yaml. Run `platform bootstrap` first.")

    manifest = load_manifest(manifest_path)
    source_repo = args.source_repo or manifest["integrations"]["github"]["source_repo"]
    context = bootstrap_repository(
        target=target,
        adapter=manifest["platform"]["adapter"],
        service_name=manifest["platform"].get("service_name", target.name),
        issue_project_key=manifest["issue"]["project_key"],
        confluence_space=manifest["integrations"]["atlassian"]["confluence_space"],
        source_repo=source_repo,
        source_ref=args.to,
        deploy_mode=manifest["deploy"]["mode"],
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
    expected_project_key = manifest_issue_project_key(manifest)
    if not issue_key_matches_project(issue_key, expected_project_key):
        fail(
            f"Issue key `{issue_key}` is out of scope for this repo. "
            f"Expected Jira project `{expected_project_key}`."
        )

    slug = slugify(args.title)
    branch = args.branch or f"feat/{issue_key}-{slug}"
    branch_issue_key = extract_issue_key(branch)
    if branch_issue_key and branch_issue_key != issue_key:
        fail(
            f"Branch `{branch}` contains issue key `{branch_issue_key}` "
            f"but the spec is for `{issue_key}`."
        )
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


def bootstrap_repository(
    *,
    target: Path,
    adapter: str,
    service_name: str,
    issue_project_key: str,
    confluence_space: str,
    source_repo: str,
    source_ref: str,
    deploy_mode: str,
    overwrite: bool,
    include_adapter_starter: bool,
    existing_manifest: dict[str, Any] | None,
) -> dict[str, str]:
    target.mkdir(parents=True, exist_ok=True)
    context = build_context(
        service_name=service_name,
        adapter=adapter,
        issue_project_key=issue_project_key,
        confluence_space=confluence_space,
        source_repo=source_repo,
        source_ref=source_ref,
        deploy_mode=deploy_mode,
    )
    apply_platform_files(
        target=target,
        context=context,
        overwrite=overwrite,
        include_adapter_starter=include_adapter_starter,
        existing_manifest=existing_manifest,
    )
    return context


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
        "ISSUE_PROJECT_KEY": issue_project_key.upper(),
        "CONFLUENCE_SPACE": confluence_space.upper(),
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

    copy_tree(REPO_ROOT / "scaffolds" / "base", target, context, overwrite=overwrite)
    copy_tree(REPO_ROOT / "ops" / "platform", target / "ops" / "platform", context, overwrite=overwrite)
    copy_tree(REPO_ROOT / ".github" / "actions", target / ".github" / "actions", context, overwrite=overwrite)

    if include_adapter_starter and not (target / "package.json").exists():
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
            "service_name": context["SERVICE_NAME"],
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
                "codex_review": {
                    "mode": DEFAULT_CODEX_REVIEW_MODE,
                },
            },
        },
    }
    if existing_manifest:
        manifest["platform"] = {
            **manifest["platform"],
            **existing_manifest.get("platform", {}),
            "version": context["RELEASE_TAG"],
            "adapter": context["ADAPTER"],
        }
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
        manifest["integrations"]["github"]["codex_review"] = {
            "mode": DEFAULT_CODEX_REVIEW_MODE,
            **existing_manifest.get("integrations", {}).get("github", {}).get("codex_review", {}),
        }
        manifest["issue"] = {
            **manifest["issue"],
            **existing_manifest.get("issue", {}),
            "project_key": context["ISSUE_PROJECT_KEY"],
        }
    return manifest


def inspect_target(target: Path) -> tuple[list[str], list[str]]:
    manifest_path = target / ".platform" / "platform.yaml"
    errors: list[str] = []
    warnings: list[str] = []

    if not manifest_path.exists():
        return ["Missing .platform/platform.yaml."], warnings

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

    warnings.extend(check_codex_review_health(target, manifest))
    warnings.extend(check_orchestrator_registration(target, manifest))

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
    else:
        expected_project_key = manifest_issue_project_key(manifest)
        for spec_path in sorted(spec_dir.glob("*.md")):
            if spec_path.name == "ISSUE_SPEC_TEMPLATE.md":
                continue
            if not issue_key_matches_project(spec_path.stem, expected_project_key):
                errors.append(
                    f"{spec_path.relative_to(target)} does not match Jira project key `{expected_project_key}`."
                )
    return errors, warnings


def resolve_create_project_settings(
    args: argparse.Namespace, config: dict[str, Any]
) -> dict[str, Any]:
    project_name = normalize_project_name(args.project_name)
    repo_name = args.repo_name or slugify(project_name)
    github_owner = args.github_owner or config.get("github_owner") or infer_active_github_owner()
    if not github_owner:
        raise PlatformCommandError(
            "GitHub owner could not be resolved. Run `platform configure --github-owner <owner>` first."
        )

    root = Path(args.root or config.get("projects_root") or DEFAULT_PROJECTS_ROOT).expanduser().resolve()
    source_repo = args.source_repo or config.get("source_repo") or infer_default_source_repo()
    source_ref = args.version or config.get("source_ref") or resolve_latest_source_ref(source_repo)
    adapter = args.adapter or config.get("adapter") or DEFAULT_ADAPTER
    launch_mode = args.launch_mode or config.get("launch_mode") or DEFAULT_LAUNCH_MODE
    deploy_mode = config.get("deploy_mode") or DEFAULT_DEPLOY_MODE
    jira_config = config.get("jira", {})
    jira_site_url = normalize_site_url(jira_config.get("site_url", ""))
    jira_admin_email = jira_config.get("admin_email", "")
    jira_name = normalize_project_name(args.jira_name or project_name)
    jira_key = (args.jira_key or generate_jira_project_key_candidate(repo_name)).upper()
    confluence_space = args.confluence_space.upper() if args.confluence_space else None
    target = root / repo_name
    return {
        "project_name": project_name,
        "repo_name": repo_name,
        "service_name": repo_name,
        "github_owner": github_owner,
        "root": root,
        "target": target,
        "github_repo": f"{github_owner}/{repo_name}",
        "source_repo": source_repo,
        "source_ref": source_ref,
        "adapter": adapter,
        "launch_mode": launch_mode,
        "deploy_mode": deploy_mode,
        "jira_site_url": jira_site_url,
        "jira_admin_email": jira_admin_email,
        "jira_key": jira_key,
        "jira_name": jira_name,
        "confluence_space": confluence_space,
        "jira_api_token": platform_orchestrator.atlassian_api_token(),
    }


def resolve_setup_repo_settings(
    args: argparse.Namespace, config: dict[str, Any]
) -> dict[str, Any]:
    target = Path(args.target).expanduser().resolve()
    repo_name = args.repo_name or slugify(target.name)
    project_name = normalize_project_name(args.project_name or repo_name.replace("-", " "))
    github_owner = args.github_owner or config.get("github_owner") or infer_active_github_owner()
    if not github_owner and not args.skip_github_create:
        raise PlatformCommandError(
            "GitHub owner could not be resolved. Pass --github-owner or run "
            "`platform configure --github-owner <owner>` first."
        )

    source_repo = args.source_repo or config.get("source_repo") or infer_default_source_repo()
    source_ref = args.version or config.get("source_ref") or resolve_latest_source_ref(source_repo)
    adapter = args.adapter or config.get("adapter") or DEFAULT_ADAPTER
    deploy_mode = args.deploy_mode or config.get("deploy_mode") or DEFAULT_DEPLOY_MODE
    launch_mode = args.launch_mode or config.get("launch_mode") or DEFAULT_LAUNCH_MODE
    jira_config = config.get("jira", {})
    jira_site_url = normalize_site_url(jira_config.get("site_url", ""))
    jira_admin_email = jira_config.get("admin_email", "")
    jira_name = normalize_project_name(args.jira_name or project_name)
    jira_key = (args.jira_key or generate_jira_project_key_candidate(repo_name)).upper()
    confluence_space = args.confluence_space.upper() if args.confluence_space else None

    return {
        "project_name": project_name,
        "repo_name": repo_name,
        "service_name": repo_name,
        "github_owner": github_owner or "",
        "target": target,
        "github_repo": f"{github_owner}/{repo_name}" if github_owner else "",
        "source_repo": source_repo,
        "source_ref": source_ref,
        "adapter": adapter,
        "deploy_mode": deploy_mode,
        "launch_mode": launch_mode,
        "jira_site_url": jira_site_url,
        "jira_admin_email": jira_admin_email,
        "jira_key": jira_key,
        "jira_name": jira_name,
        "confluence_space": confluence_space,
        "jira_api_token": platform_orchestrator.atlassian_api_token(),
        "create_github": not args.skip_github_create,
        "create_jira": not args.skip_jira_create,
        "register_orchestrator": not args.skip_register,
        "commit_and_push": not args.no_commit_push,
        "allow_dirty": args.allow_dirty,
        "force": args.force,
    }


def preflight_create_project(settings: dict[str, Any]) -> None:
    required_tools = ["git", "gh", "python3", "claude", "codex"]
    if settings["adapter"] == "node-ts":
        required_tools.extend(["node", "pnpm"])
    if settings["launch_mode"] == "tmux":
        required_tools.append("tmux")
    missing = [tool for tool in required_tools if shutil.which(tool) is None]
    if missing:
        raise PlatformCommandError(
            "Missing required tools for create-project: " + ", ".join(sorted(missing))
        )

    if not settings["jira_site_url"]:
        raise PlatformCommandError(
            "Jira site URL is missing. Run `platform configure --jira-site-url https://<site>.atlassian.net`."
        )
    if not settings["jira_admin_email"]:
        raise PlatformCommandError(
            "Jira admin email is missing. Run `platform configure --jira-admin-email <email>`."
        )
    if not settings["jira_api_token"]:
        raise PlatformCommandError(
            "ATLASSIAN_API_TOKEN is missing. Export it or store it in macOS Keychain service "
            f"`{platform_orchestrator.ATLASSIAN_TOKEN_KEYCHAIN_SERVICE}` before running "
            "`platform create-project`."
        )

    gh_status = run_optional(["gh", "auth", "status"])
    if not gh_status or gh_status.returncode != 0:
        raise PlatformCommandError("GitHub CLI is not authenticated. Run `gh auth login`.")

    claude_status = run_optional(["claude", "auth", "status"])
    if not claude_status or claude_status.returncode != 0:
        raise PlatformCommandError("Claude auth could not be verified. Run `claude auth login --claudeai`.")
    try:
        claude_payload = json.loads(claude_status.stdout)
    except json.JSONDecodeError:
        claude_payload = {}
    if not claude_payload.get("loggedIn", False):
        raise PlatformCommandError("Claude is not logged in. Run `claude auth login --claudeai`.")

    codex_status = run_optional(["codex", "login", "status"])
    codex_output = ""
    if codex_status:
        codex_output = f"{codex_status.stdout}\n{codex_status.stderr}".lower()
    if not codex_status or codex_status.returncode != 0 or "logged in" not in codex_output or "chatgpt" not in codex_output:
        raise PlatformCommandError("Codex is not using ChatGPT login. Run `codex login`.")

    git_name = run_optional(["git", "config", "--global", "user.name"])
    git_email = run_optional(["git", "config", "--global", "user.email"])
    if not git_name or git_name.returncode != 0 or not git_name.stdout.strip():
        raise PlatformCommandError("Git user.name is missing. Configure it before create-project.")
    if not git_email or git_email.returncode != 0 or not git_email.stdout.strip():
        raise PlatformCommandError("Git user.email is missing. Configure it before create-project.")

    settings["root"].mkdir(parents=True, exist_ok=True)
    if settings["target"].exists():
        raise PlatformCommandError(f"Target directory already exists: {settings['target']}")

    if github_repo_exists(settings["github_repo"]):
        raise PlatformCommandError(f"GitHub repository already exists: {settings['github_repo']}")

    if settings["launch_mode"] == "tmux" and tmux_session_exists(settings["repo_name"]):
        raise PlatformCommandError(f"tmux session already exists: {settings['repo_name']}")


def preflight_setup_repo(settings: dict[str, Any]) -> None:
    required_tools = ["git", "gh", "python3", "claude", "codex"]
    if settings["adapter"] == "node-ts":
        required_tools.extend(["node", "pnpm"])
    if settings["launch_mode"] == "tmux":
        required_tools.append("tmux")
    missing = [tool for tool in required_tools if shutil.which(tool) is None]
    if missing:
        raise PlatformCommandError(
            "Missing required tools for setup-repo: " + ", ".join(sorted(missing))
        )

    target = settings["target"]
    if not target.exists() or not target.is_dir():
        raise PlatformCommandError(f"Target directory does not exist: {target}")
    manifest_path = target / ".platform" / "platform.yaml"
    if manifest_path.exists() and not settings["force"]:
        raise PlatformCommandError(
            f"{manifest_path} already exists. Re-run with --force or use `platform upgrade`."
        )

    if settings["create_jira"]:
        if not settings["jira_site_url"]:
            raise PlatformCommandError(
                "Jira site URL is missing. Run `platform configure --jira-site-url https://<site>.atlassian.net`."
            )
        if not settings["jira_admin_email"]:
            raise PlatformCommandError(
                "Jira admin email is missing. Run `platform configure --jira-admin-email <email>`."
            )
        if not settings["jira_api_token"]:
            raise PlatformCommandError(
                "ATLASSIAN_API_TOKEN is missing. Export it or store it in macOS Keychain service "
                f"`{platform_orchestrator.ATLASSIAN_TOKEN_KEYCHAIN_SERVICE}` before running "
                "`platform setup-repo`."
            )

    gh_status = run_optional(["gh", "auth", "status"])
    if not gh_status or gh_status.returncode != 0:
        raise PlatformCommandError("GitHub CLI is not authenticated. Run `gh auth login`.")

    claude_status = run_optional(["claude", "auth", "status"])
    if not claude_status or claude_status.returncode != 0:
        raise PlatformCommandError("Claude auth could not be verified. Run `claude auth login --claudeai`.")
    try:
        claude_payload = json.loads(claude_status.stdout)
    except json.JSONDecodeError:
        claude_payload = {}
    if not claude_payload.get("loggedIn", False):
        raise PlatformCommandError("Claude is not logged in. Run `claude auth login --claudeai`.")

    codex_status = run_optional(["codex", "login", "status"])
    codex_output = ""
    if codex_status:
        codex_output = f"{codex_status.stdout}\n{codex_status.stderr}".lower()
    if not codex_status or codex_status.returncode != 0 or "logged in" not in codex_output or "chatgpt" not in codex_output:
        raise PlatformCommandError("Codex is not using ChatGPT login. Run `codex login`.")

    if settings["commit_and_push"]:
        git_name = run_optional(["git", "config", "--global", "user.name"])
        git_email = run_optional(["git", "config", "--global", "user.email"])
        if not git_name or git_name.returncode != 0 or not git_name.stdout.strip():
            raise PlatformCommandError("Git user.name is missing. Configure it before setup-repo.")
        if not git_email or git_email.returncode != 0 or not git_email.stdout.strip():
            raise PlatformCommandError("Git user.email is missing. Configure it before setup-repo.")

    if settings["launch_mode"] == "tmux" and tmux_session_exists(settings["repo_name"]):
        raise PlatformCommandError(f"tmux session already exists: {settings['repo_name']}")


def create_github_repository(settings: dict[str, Any]) -> None:
    args = [
        "gh",
        "repo",
        "create",
        settings["github_repo"],
        "--private",
        "--disable-issues",
        "--disable-wiki",
        "--description",
        f"{settings['project_name']} bootstrapped from the shared AI development platform.",
    ]
    run_command(args, cwd=settings["root"])


def clone_github_repository(settings: dict[str, Any]) -> None:
    run_command(
        ["gh", "repo", "clone", settings["github_repo"], str(settings["target"])],
        cwd=settings["root"],
    )


def prepare_local_git_repository(target: Path) -> None:
    run_command(["git", "checkout", "-B", "main"], cwd=target)


def ensure_git_repository(target: Path) -> None:
    if not (target / ".git").exists():
        run_command(["git", "init"], cwd=target)
        run_command(["git", "checkout", "-B", "main"], cwd=target)
        return
    if not git_has_commits(target):
        run_command(["git", "checkout", "-B", "main"], cwd=target)


def ensure_github_remote(settings: dict[str, Any]) -> None:
    target = settings["target"]
    desired_repo = settings["github_repo"]
    origin_url = git_remote_url(target)
    if origin_url:
        existing_repo = repo_full_name_from_remote_url(origin_url)
        if existing_repo and existing_repo.lower() != desired_repo.lower():
            raise PlatformCommandError(
                f"origin already points to `{existing_repo}`, not `{desired_repo}`. "
                "Use --skip-github-create to keep the existing remote, or choose matching --github-owner/--repo-name."
            )
        print(f"GitHub repo: using existing origin {origin_url}")
        return
    if github_repo_exists(desired_repo):
        raise PlatformCommandError(
            f"GitHub repository already exists: {desired_repo}. "
            "Clone that repo first or use --skip-github-create with the correct origin."
        )
    create_github_repository({**settings, "root": target.parent})
    run_command(["git", "remote", "add", "origin", f"https://github.com/{desired_repo}.git"], cwd=target)


def git_has_commits(target: Path) -> bool:
    result = run_optional(["git", "rev-parse", "--verify", "HEAD"], cwd=target)
    return bool(result and result.returncode == 0)


def git_status_porcelain(target: Path) -> str:
    result = run_optional(["git", "status", "--porcelain"], cwd=target)
    if not result or result.returncode != 0:
        raise PlatformCommandError(f"Could not inspect git status for {target}.")
    return result.stdout.strip()


def git_current_branch(target: Path) -> str:
    result = run_optional(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=target)
    if result and result.returncode == 0:
        branch = result.stdout.strip()
        if branch and branch != "HEAD":
            return branch
    return "main"


def git_remote_url(target: Path) -> str:
    result = run_optional(["git", "remote", "get-url", "origin"], cwd=target)
    if result and result.returncode == 0:
        return result.stdout.strip()
    return ""


def repo_full_name_from_remote_url(value: str) -> str | None:
    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$", value)
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def install_adapter_dependencies(target: Path, adapter: str) -> None:
    if adapter == "node-ts" and (target / "package.json").exists():
        run_command(["pnpm", "install"], cwd=target)


def ensure_doctor_passes(target: Path) -> None:
    errors, warnings = inspect_target(target)
    report_issues(errors, warnings)
    if errors:
        raise PlatformCommandError(f"`platform doctor` failed for {target}")


def commit_and_push_initial_repo(target: Path) -> None:
    run_command(["git", "add", "."], cwd=target)
    run_command(["git", "commit", "-m", "Bootstrap platform baseline"], cwd=target)
    run_command(["git", "push", "-u", "origin", "main"], cwd=target)


def commit_and_push_setup_repo(target: Path) -> None:
    if not git_remote_url(target):
        raise PlatformCommandError("Cannot push setup commit because origin is not configured.")
    run_command(["git", "add", "."], cwd=target)
    diff = run_optional(["git", "diff", "--cached", "--quiet"], cwd=target)
    if diff and diff.returncode == 0:
        print("No setup changes to commit.")
    else:
        run_command(["git", "commit", "-m", "Bootstrap platform baseline"], cwd=target)
    branch = git_current_branch(target)
    run_command(["git", "push", "-u", "origin", branch], cwd=target)


def ensure_orchestrator_project_root(root: Path) -> None:
    config_path = platform_orchestrator.default_config_path()
    config = platform_orchestrator.load_orchestrator_config(config_path)
    roots = [str(Path(item).expanduser().resolve()) for item in config.get("projects_roots", [])]
    resolved = str(root.expanduser().resolve())
    if resolved not in roots:
        roots.append(resolved)
        config["projects_roots"] = roots
        platform_orchestrator.save_orchestrator_config(config, config_path)


def register_orchestrator_project(target: Path) -> None:
    args = argparse.Namespace(
        target=str(target),
        config=None,
        bind_host=None,
        bind_port=None,
        event_mode="polling",
        webhook=False,
        public_base_url=None,
        webhook_secret=None,
        listen_url=None,
        shared_secret=None,
    )
    platform_orchestrator.cmd_register(args)


def create_jira_project(settings: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "key": settings["jira_key"],
        "name": settings["jira_name"],
        "projectTypeKey": JIRA_PROJECT_TYPE_KEY,
        "projectTemplateKey": JIRA_KANBAN_TEMPLATE_KEY,
        "assigneeType": JIRA_ASSIGNEE_TYPE,
        "leadAccountId": settings["jira_lead_account_id"],
        "description": (
            "Provisioned by the AI development platform for "
            f"{settings['github_repo']}."
        ),
    }
    response = jira_request(
        site_url=settings["jira_site_url"],
        admin_email=settings["jira_admin_email"],
        api_token=settings["jira_api_token"],
        method="POST",
        path="/rest/api/3/project",
        payload=payload,
    )
    if not isinstance(response, dict):
        raise PlatformCommandError("Jira project creation returned an unexpected response.")
    return response


def launch_tmux_workspace(target: Path, session_name: str) -> str:
    if tmux_session_exists(session_name):
        raise PlatformCommandError(f"tmux session already exists: {session_name}")
    run_command(["tmux", "new-session", "-d", "-s", session_name, "-n", "dev", "-c", str(target)])
    run_command(["tmux", "new-window", "-t", session_name, "-n", "claude", "-c", str(target), "claude"])
    run_command(["tmux", "new-window", "-t", session_name, "-n", "codex", "-c", str(target), "codex"])
    if sys.stdout.isatty() and not os.environ.get("TMUX"):
        subprocess.run(["tmux", "attach-session", "-t", session_name], check=False)
    return session_name


def cleanup_partial_project(settings: dict[str, Any]) -> None:
    target = settings["target"]
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    subprocess.run(
        ["gh", "repo", "delete", settings["github_repo"], "--yes"],
        cwd=settings["root"],
        check=False,
        capture_output=True,
        text=True,
    )


def print_create_project_summary(
    settings: dict[str, Any], context: dict[str, str], session_name: str | None
) -> None:
    print("Create-project complete")
    print(f"- repo: {settings['github_repo']}")
    print(f"- local path: {settings['target']}")
    print(f"- Jira project: {settings['jira_key']} ({settings['jira_name']})")
    print(f"- Confluence space: {settings['confluence_space']}")
    print(f"- adapter: {context['ADAPTER']}")
    print(f"- workflow ref: {context['SOURCE_REF']}")
    if session_name:
        print(f"- tmux session: {session_name}")


def print_setup_repo_summary(
    settings: dict[str, Any], context: dict[str, str], session_name: str | None
) -> None:
    print("Setup-repo complete")
    repo_label = (
        settings["github_repo"]
        if settings["create_github"]
        else infer_repo_full_name(settings["target"]) or "existing local repo (GitHub creation skipped)"
    )
    print(f"- repo: {repo_label}")
    print(f"- local path: {settings['target']}")
    print(f"- Jira project: {settings['jira_key']} ({settings['jira_name']})")
    print(f"- Confluence space: {settings['confluence_space']}")
    print(f"- adapter: {context['ADAPTER']}")
    print(f"- workflow ref: {context['SOURCE_REF']}")
    print(f"- committed/pushed: {'yes' if settings['commit_and_push'] else 'no'}")
    print(f"- orchestrator registered: {'yes' if settings['register_orchestrator'] else 'no'}")
    if session_name:
        print(f"- tmux session: {session_name}")


def user_config_path() -> Path:
    base_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
    return base_dir / USER_CONFIG_DIRNAME / USER_CONFIG_FILENAME


def default_user_config() -> dict[str, Any]:
    source_repo = infer_default_source_repo()
    return {
        "version": CONFIG_VERSION,
        "github_owner": infer_active_github_owner() or "",
        "projects_root": str(Path(DEFAULT_PROJECTS_ROOT).expanduser().resolve()),
        "source_repo": source_repo,
        "source_ref": resolve_latest_source_ref(source_repo),
        "adapter": DEFAULT_ADAPTER,
        "deploy_mode": DEFAULT_DEPLOY_MODE,
        "launch_mode": DEFAULT_LAUNCH_MODE,
        "jira": {
            "site_url": "",
            "admin_email": "",
        },
    }


def load_user_config() -> dict[str, Any]:
    config = default_user_config()
    path = user_config_path()
    if not path.exists():
        return config
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return config
    if isinstance(loaded, dict):
        config.update({key: value for key, value in loaded.items() if key != "jira"})
        if isinstance(loaded.get("jira"), dict):
            config["jira"] = {
                **config.get("jira", {}),
                **loaded["jira"],
            }
    config["projects_root"] = str(Path(config["projects_root"]).expanduser().resolve())
    config["version"] = CONFIG_VERSION
    config.setdefault("jira", {})
    config["jira"]["site_url"] = normalize_site_url(config["jira"].get("site_url", ""))
    return config


def save_user_config(config: dict[str, Any]) -> None:
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def normalize_site_url(value: str) -> str:
    return value.strip().rstrip("/")


def normalize_project_name(value: str) -> str:
    text = " ".join(value.split()).strip()
    return text or "Untitled Project"


def generate_jira_project_key_candidate(repo_name: str) -> str:
    segments = [segment for segment in repo_name.split("-") if segment]
    acronym = "".join(segment[0].upper() for segment in segments if segment and segment[0].isalpha())
    collapsed = re.sub(r"[^A-Za-z0-9]", "", repo_name).upper()
    letters_only = "".join(char for char in collapsed if char.isalpha())
    candidate = acronym[:4]
    if len(candidate) < 2:
        candidate = letters_only[:4] or collapsed[:4]
    if len(candidate) < 2:
        candidate = "PRJ"
    if not candidate[0].isalpha():
        candidate = f"P{candidate}"
    return candidate[:10]


def jira_current_user_account_id(site_url: str, admin_email: str, api_token: str) -> str:
    payload = jira_request(
        site_url=site_url,
        admin_email=admin_email,
        api_token=api_token,
        method="GET",
        path="/rest/api/3/myself",
    )
    account_id = payload.get("accountId") if isinstance(payload, dict) else None
    if not account_id:
        raise PlatformCommandError("Jira admin accountId could not be resolved from /rest/api/3/myself.")
    return account_id


def jira_valid_project_key(site_url: str, admin_email: str, api_token: str, requested_key: str) -> str:
    path = "/rest/api/3/projectvalidate/validProjectKey?" + parse.urlencode({"key": requested_key})
    payload = jira_request(
        site_url=site_url,
        admin_email=admin_email,
        api_token=api_token,
        method="GET",
        path=path,
    )
    if isinstance(payload, str) and payload:
        return payload.upper()
    raise PlatformCommandError("Jira did not return a valid project key suggestion.")


def jira_valid_project_name(site_url: str, admin_email: str, api_token: str, requested_name: str) -> str:
    path = "/rest/api/3/projectvalidate/validProjectName?" + parse.urlencode({"name": requested_name})
    payload = jira_request(
        site_url=site_url,
        admin_email=admin_email,
        api_token=api_token,
        method="GET",
        path=path,
    )
    if isinstance(payload, str) and payload:
        return payload
    raise PlatformCommandError("Jira did not return a valid project name suggestion.")


def jira_request(
    *,
    site_url: str,
    admin_email: str,
    api_token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    if not site_url.startswith("https://"):
        raise PlatformCommandError(f"Jira site URL must start with https://: {site_url}")
    url = site_url + path
    data = None
    headers = {
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    token = base64.b64encode(f"{admin_email}:{api_token}".encode("utf-8")).decode("utf-8")
    headers["Authorization"] = f"Basic {token}"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise PlatformCommandError(
            f"Jira API request failed: {method} {path} -> {exc.code} {exc.reason}. {body}"
        ) from exc
    except error.URLError as exc:
        raise PlatformCommandError(f"Jira API request failed: {method} {path} -> {exc.reason}") from exc

    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body.strip()


def github_repo_exists(repo: str) -> bool:
    result = subprocess.run(
        ["gh", "repo", "view", repo],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def infer_active_github_owner() -> str | None:
    result = run_optional(["gh", "api", "user", "--jq", ".login"])
    if result and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def resolve_latest_source_ref(source_repo: str) -> str:
    result = run_optional(["gh", "api", f"repos/{source_repo}/releases/latest", "--jq", ".tag_name"])
    if result and result.returncode == 0:
        tag = result.stdout.strip()
        if tag:
            return tag
    return DEFAULT_SOURCE_REF


def tmux_session_exists(session_name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


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


def run_optional(
    argv: list[str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except (FileNotFoundError, PermissionError, OSError):
        return None


def run_command(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        message = [f"Command failed: {' '.join(argv)}"]
        if result.stdout.strip():
            message.append(result.stdout.strip())
        if result.stderr.strip():
            message.append(result.stderr.strip())
        raise PlatformCommandError("\n".join(message))
    return result


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
        if "__pycache__" in file_path.parts or file_path.suffix == ".pyc":
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
    account = infer_active_github_owner()
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


def manifest_issue_project_key(manifest: dict[str, Any]) -> str:
    return str(manifest["issue"]["project_key"]).upper()


def issue_key_matches_project(issue_key: str, project_key: str) -> bool:
    return issue_key.upper().startswith(f"{project_key.upper()}-")


def extract_issue_key(value: str | None) -> str | None:
    if not value:
        return None
    match = ISSUE_RE.search(value.upper())
    return match.group(0) if match else None


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


def check_codex_review_health(target: Path, manifest: dict[str, Any]) -> list[str]:
    mode = str(
        manifest.get("integrations", {})
        .get("github", {})
        .get("codex_review", {})
        .get("mode", DEFAULT_CODEX_REVIEW_MODE)
    )
    if mode != DEFAULT_CODEX_REVIEW_MODE or shutil.which("gh") is None:
        return []
    result = run_optional(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--limit",
            "10",
            "--json",
            "number,url,reviews,comments,reviewDecision",
        ],
        cwd=target,
    )
    if not result or result.returncode != 0 or not result.stdout.strip():
        return ["Could not verify recent Codex review activity on this repository."]
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ["Could not parse recent PR data for Codex review health."]
    return codex_review_health_from_prs(prs)


def codex_review_health_from_prs(prs: Any) -> list[str]:
    if not isinstance(prs, list) or not prs:
        return ["No PR history found yet to validate automatic Codex reviews."]
    authors = {author.lower() for author in DEFAULT_CODEX_REVIEW_AUTHORS}
    fallback_requested = False
    for pr in prs:
        reviews = pr.get("reviews", []) if isinstance(pr, dict) else []
        for review in reviews:
            login = str(review.get("author", {}).get("login", "")).lower()
            if login in authors:
                return []
        comments = pr.get("comments", []) if isinstance(pr, dict) else []
        for comment in comments:
            if is_codex_review_comment(comment, authors):
                return []
            body = str(comment.get("body", ""))
            if "@codex review" in body.lower():
                fallback_requested = True
    if fallback_requested:
        return [
            "Recent PRs show `@codex review` fallback requests, but no Codex review artifact. "
            f"Enable repo-level Code review in Codex settings ({CODEX_CODE_REVIEW_SETTINGS_URL})."
        ]
    return [
        "Automatic Codex review is not verified: recent PRs do not show a Codex review artifact. "
        f"Run `platform codex-review --target <repo> --open-settings` and enable the repo in Codex settings ({CODEX_CODE_REVIEW_SETTINGS_URL})."
    ]


def is_codex_review_comment(comment: dict[str, Any], authors: set[str]) -> bool:
    login = str(comment.get("author", {}).get("login", "")).lower()
    body = str(comment.get("body", "")).lower()
    return login in authors and "codex review:" in body


def infer_repo_full_name(target: Path) -> str | None:
    result = run_optional(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        cwd=target,
    )
    if result and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    remote = run_optional(["git", "remote", "get-url", "origin"], cwd=target)
    if not remote or remote.returncode != 0:
        return None
    value = remote.stdout.strip()
    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$", value)
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def request_codex_review_comment(*, target: Path, pr_number: int, force: bool) -> None:
    existing = run_optional(
        ["gh", "pr", "view", str(pr_number), "--json", "comments", "--jq", ".comments[].body"],
        cwd=target,
    )
    if existing and existing.returncode == 0 and "@codex review" in existing.stdout.lower() and not force:
        print(f"Skipped PR #{pr_number}: `@codex review` was already requested. Use --force-comment to post again.")
        return
    run_command(["gh", "pr", "comment", str(pr_number), "--body", "@codex review"], cwd=target)
    print(f"Requested Codex review on PR #{pr_number}.")


def check_orchestrator_registration(target: Path, manifest: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    project_key = manifest_issue_project_key(manifest)
    try:
        settings = platform_orchestrator.load_worker_settings()
    except Exception as exc:
        return [f"Could not inspect orchestrator registration: {exc}"]
    if not settings.db_path.exists():
        return [
            "Orchestrator state DB was not found. "
            "Run `platform orchestrator register --target <repo>` before Jira-driven automation."
        ]
    store = platform_orchestrator.OrchestratorStore(settings.db_path)

    project = store.project(project_key)
    if not project:
        return [
            "Orchestrator registration not found for this repo. "
            "Run `platform orchestrator register --target <repo>` before Jira-driven automation."
        ]

    registered_repo = Path(str(project.get("repo_path", ""))).expanduser().resolve()
    if registered_repo != target.resolve():
        warnings.append(
            f"Orchestrator project `{project_key}` is registered to `{registered_repo}`, not this target."
        )
    event_mode = str(getattr(settings, "event_mode", "polling"))
    if event_mode == "webhook" and not settings.public_base_url:
        warnings.append(
            "Orchestrator public_base_url is not configured. Jira Automation callbacks need a fixed public URL. "
            "Run `platform orchestrator configure --public-base-url https://orchestrator.<domain>` on the worker host, then re-run `platform orchestrator register --target <repo>`."
        )
    if event_mode == "webhook" and not project.get("webhook_secret"):
        warnings.append(
            "Orchestrator registration is missing a project webhook secret. Re-run `platform orchestrator register --target <repo>`."
        )
    if not project.get("control_issue_key"):
        warnings.append(
            "Orchestrator registration has no Jira control issue. Re-run `platform orchestrator register --target <repo>` to enable project-level control commands."
        )
    if event_mode == "webhook" and (
        not project.get("lifecycle_rule_uuid") or not project.get("comment_rule_uuid")
    ):
        warnings.append(
            "Orchestrator Automation rule IDs are missing. Confirm Jira Automation rules were created or import the exported blueprints."
        )
    return warnings


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


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        fail(f"Manifest not found: {path}")
        raise exc
    except json.JSONDecodeError as exc:
        fail(f"Manifest must be JSON-compatible YAML: {path} ({exc})")
        raise exc


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
