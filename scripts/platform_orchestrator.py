#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import plistlib
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, parse, request


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[1]
DEFAULT_READY_STATUSES = ("To Do", "Selected for Development")
DEFAULT_POLL_INTERVALS = {
    "reconcile_seconds": 300,
    "github_seconds": 30,
    "loop_seconds": 5,
}
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8787
DEFAULT_EVENT_MODE = "polling"
DEFAULT_GITHUB_MODE = "polling"
DEFAULT_HEADER_NAME = "X-Platform-Orchestrator-Secret"
LEGACY_HEADER_NAME = "X-Platform-Shared-Secret"
DEFAULT_CODEX_REVIEW_AUTHORS = ("codex", "codex[bot]", "chatgpt-codex-connector")
DEFAULT_AUTO_REVIEW_GRACE_SECONDS = 180
DEFAULT_FALLBACK_REVIEW_GRACE_SECONDS = 300
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 600
DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS = 900
DEFAULT_LOCAL_REVIEW_TIMEOUT_SECONDS = 180
DEFAULT_CODEX_MODEL = ""
DEFAULT_CODEX_BINARY = "auto"
DEFAULT_CODEX_IGNORE_USER_CONFIG = True
DEFAULT_CLAUDE_MODEL = "default"
DEFAULT_CLAUDE_EFFORT = ""
DEFAULT_MAX_PARALLEL_PER_REPO = 3
DEFAULT_MAX_PARALLEL_PER_PROJECT = 5
DEFAULT_CONTRACT_HANDSHAKE = "required"
DEFAULT_MAX_BATON_ROUNDS = 2
DEFAULT_FAILURE_MAX_ATTEMPTS = 2
DEFAULT_FAILURE_BACKLOG_STATUSES = ("To Do", "Backlog")
DEFAULT_GITHUB_MERGE_POLICY = "merge_queue"
DEFAULT_TRANSITION_POLICY_MODE = "kanban_minimal"
DEFAULT_ACTIVE_STATUS_ALIASES = ("In Progress", "進行中", "作業中")
DEFAULT_DONE_STATUS_ALIASES = ("Done", "完了")
ATLASSIAN_TOKEN_KEYCHAIN_SERVICE = "ai-dev-platform.atlassian-api-token"
IGNORED_WORKTREE_PREFIXES = (".tmp/",)
IGNORED_WORKTREE_PATHS = {
    ".platform/.last-validation.json",
}
ORCHESTRATOR_CONFIG_FILENAME = "orchestrator.json"
PLATFORM_CONFIG_FILENAME = "config.json"
TOOLCHAIN_CONFIG_FILENAME = "toolchain.json"
CONFIG_DIRNAME = "ai-dev-platform"
DB_FILENAME = "orchestrator.db"
SUMMARY_MARKER = "<!-- platform-orchestrator:summary -->"
START_LABEL = "ai:auto"
CONTROL_LABEL = "ai:control"
WORKTREE_ROOTNAME = "worktrees"
ISSUE_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")
CONTROL_COMMAND_RE = re.compile(
    r"^/ai\s+(pause|resume|cancel|retry|status|unblock|pause-project|resume-project|drain-project)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
EVENT_PATH_RE = re.compile(r"^/jira/events/(?P<project_key>[A-Za-z][A-Za-z0-9]+)$")
RUNNABLE_STATES = {"queued", "planning", "coding", "reviewing", "pr_open", "paused", "blocked", "failed"}
GATE_STATES = {"gate_waiting_human", "gate_failed"}
DEPENDENCY_WAIT_STATES = {"waiting_dependency"}
DEPENDENCY_BLOCKING_STATES = {
    "gate_failed",
    "gate_waiting_human",
    "blocked",
    "failed",
    "backlog",
    "cancelled",
}
WAITING_STATES = {"waiting_checks", "waiting_review", "ready_for_merge", *GATE_STATES, *DEPENDENCY_WAIT_STATES}
GITHUB_POLL_STATES = {"waiting_checks", "waiting_review", "ready_for_merge", *GATE_STATES}
TERMINAL_STATES = {"ready_for_merge", "done", "cancelled", "backlog"}
ACTIVE_JIRA_TRANSITION_STATES = {
    "queued",
    "planning",
    "coding",
    "reviewing",
    "pr_open",
    "waiting_checks",
    "waiting_review",
    "ready_for_merge",
}
LAUNCH_AGENT_LABEL = "com.ai-dev-platform.orchestrator"
MIN_CODEX_VERSION = (0, 125, 0)
REQUIRED_CODEX_EXEC_FLAGS = (
    "--ignore-user-config",
    "--output-schema",
    "--output-last-message",
    "--cd",
    "--json",
)
CODEX_CLI_INCOMPATIBLE_PATTERNS = (
    "unexpected argument '--ignore-user-config'",
    "unknown option",
    "requires a newer version of codex",
    "no such file or directory",
)
TRANSIENT_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
DEFAULT_API_RETRY_ATTEMPTS = 3
PLATFORM_RELEASE_VERSION = "v0.2.1"


class OrchestratorError(RuntimeError):
    pass


class JiraRequestError(OrchestratorError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RepoProject:
    project_key: str
    repo_path: Path
    repo_name: str
    confluence_space: str
    codex_review_mode: str
    transition_policy: dict[str, Any]
    manifest_path: Path
    source_repo: str
    workflow_ref: str


@dataclass(frozen=True)
class WorkerSettings:
    config_path: Path
    db_path: Path
    bind_host: str
    bind_port: int
    event_mode: str
    public_base_url: str
    projects_roots: tuple[Path, ...]
    poll_intervals: dict[str, int]
    github_mode: str
    codex_review_authors: tuple[str, ...]
    auto_review_grace_seconds: int
    fallback_review_grace_seconds: int
    max_parallel_per_repo: int
    max_parallel_per_project: int
    contract_handshake: str
    max_baton_rounds: int
    failure_max_attempts: int
    failure_backlog_statuses: tuple[str, ...]
    github_merge_policy: str
    claude_timeout_seconds: int
    codex_exec_timeout_seconds: int
    codex_review_timeout_seconds: int
    codex_model: str
    codex_binary: str
    codex_resolved_binary: str
    codex_resolved_version: str
    codex_toolchain_compatible: bool
    codex_ignore_user_config: bool
    claude_model: str
    claude_effort: str
    jira_site_url: str
    jira_admin_email: str


def keychain_atlassian_api_token() -> str:
    if sys.platform != "darwin" or shutil.which("security") is None:
        return ""
    command = ["security", "find-generic-password"]
    account = os.environ.get("USER", "").strip()
    if account:
        command.extend(["-a", account])
    command.extend(["-s", ATLASSIAN_TOKEN_KEYCHAIN_SERVICE, "-w"])
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def atlassian_api_token() -> str:
    env_token = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
    if env_token:
        return env_token
    return keychain_atlassian_api_token()


def register_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    orchestrator = subparsers.add_parser(
        "orchestrator",
        help="Run and control the resident Jira-driven orchestrator worker.",
    )
    orchestrator_subparsers = orchestrator.add_subparsers(
        dest="orchestrator_command", required=True
    )

    configure = orchestrator_subparsers.add_parser(
        "configure",
        help="Update resident worker settings such as polling roots or optional webhook URL.",
    )
    configure.add_argument("--config", default=None, help="Override the worker config path.")
    configure.add_argument("--bind-host", default=None, help="HTTP bind host for the worker.")
    configure.add_argument("--bind-port", type=int, default=None, help="HTTP bind port for the worker.")
    configure.add_argument(
        "--event-mode",
        choices=["polling", "webhook"],
        default=None,
        help="Jira intake mode. Default is polling; webhook requires a fixed public URL.",
    )
    configure.add_argument(
        "--public-base-url",
        default=None,
        help="Fixed public HTTPS base URL used by Jira Automation callbacks.",
    )
    configure.add_argument(
        "--clear-public-base-url",
        action="store_true",
        help="Clear the public callback URL so live Jira callbacks are not configured.",
    )
    configure.add_argument(
        "--project-root",
        action="append",
        default=None,
        help="Add a projects root scanned by the worker.",
    )
    configure.add_argument("--jira-site-url", default=None, help="Atlassian Cloud site URL.")
    configure.add_argument("--jira-admin-email", default=None, help="Jira admin email.")
    configure.add_argument(
        "--codex-model",
        default=None,
        help="Codex CLI model for worker coding/review stages. Use empty string to defer to Codex defaults.",
    )
    configure.add_argument(
        "--codex-binary",
        default=None,
        help="Codex CLI binary for worker coding/review stages. Use `auto` to resolve from the toolchain contract.",
    )
    configure.add_argument(
        "--codex-use-user-config",
        action="store_true",
        help="Allow worker Codex subprocesses to load ~/.codex/config.toml. Default is isolated.",
    )
    configure.add_argument(
        "--codex-ignore-user-config",
        action="store_true",
        help="Force worker Codex subprocesses to ignore ~/.codex/config.toml.",
    )
    configure.add_argument(
        "--claude-model",
        default=None,
        help="Claude Code model alias or full name for worker planning/integration stages.",
    )
    configure.add_argument(
        "--claude-effort",
        choices=["", "low", "medium", "high", "xhigh", "max"],
        default=None,
        help="Claude Code effort level for worker planning/integration. Empty string defers to Claude defaults.",
    )
    configure.add_argument("--max-parallel-per-repo", type=int, default=None, help="Maximum concurrent jobs per repo.")
    configure.add_argument("--max-parallel-per-project", type=int, default=None, help="Maximum concurrent jobs per Jira project.")
    configure.add_argument("--max-baton-rounds", type=int, default=None, help="Maximum Claude/Codex contract clarification rounds.")
    configure.add_argument("--codex-exec-timeout", type=int, default=None, help="Codex implementation timeout in seconds.")
    configure.add_argument("--codex-review-timeout", type=int, default=None, help="Codex local review timeout in seconds.")
    configure.add_argument("--claude-timeout", type=int, default=None, help="Claude planning/integration timeout in seconds.")
    configure.add_argument("--failure-max-attempts", type=int, default=None, help="Maximum attempts before failing and returning to backlog.")
    configure.add_argument(
        "--github-merge-policy",
        choices=["merge_queue", "manual"],
        default=None,
        help="Merge policy after ready_for_merge. Default enables GitHub auto-merge/merge queue.",
    )
    configure.set_defaults(func=cmd_configure)

    run = orchestrator_subparsers.add_parser(
        "run",
        help="Run the resident worker that polls Jira/GitHub and drives Claude/Codex.",
    )
    run.add_argument("--config", default=None, help="Override the worker config path.")
    run.add_argument("--bind-host", default=None, help="Override the HTTP bind host.")
    run.add_argument("--bind-port", type=int, default=None, help="Override the HTTP bind port.")
    run.add_argument(
        "--event-mode",
        choices=["polling", "webhook"],
        default=None,
        help="Override Jira intake mode for this run.",
    )
    run.add_argument(
        "--poll-only",
        action="store_true",
        help="Run in polling mode and do not start the webhook HTTP listener.",
    )
    run.add_argument(
        "--public-base-url",
        default=None,
        help="Override the public HTTPS base URL used by Jira Automation callbacks.",
    )
    run.add_argument(
        "--listen-url",
        default=None,
        help=argparse.SUPPRESS,
    )
    run.add_argument(
        "--project-root",
        action="append",
        default=None,
        help="Add a projects root for this invocation only.",
    )
    run.add_argument("--jira-site-url", default=None, help="Override Jira site URL.")
    run.add_argument("--jira-admin-email", default=None, help="Override Jira admin email.")
    run.add_argument(
        "--once",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    run.add_argument(
        "--no-http",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    run.set_defaults(func=cmd_run)

    register = orchestrator_subparsers.add_parser(
        "register",
        help="Register a repo for orchestration and create or reuse its Jira control issue.",
    )
    register.add_argument("--target", required=True, help="Repository path to register.")
    register.add_argument("--config", default=None, help="Override the worker config path.")
    register.add_argument("--bind-host", default=None, help="Override the HTTP bind host.")
    register.add_argument("--bind-port", type=int, default=None, help="Override the HTTP bind port.")
    register.add_argument(
        "--event-mode",
        choices=["polling", "webhook"],
        default=None,
        help="Override Jira intake mode for registration.",
    )
    register.add_argument(
        "--webhook",
        action="store_true",
        help="Opt in to Jira Automation webhook rule creation for this repo.",
    )
    register.add_argument(
        "--public-base-url",
        default=None,
        help="Override the public HTTPS base URL used by Jira Automation callbacks.",
    )
    register.add_argument(
        "--webhook-secret",
        default=None,
        help="Override the per-project Jira Automation webhook secret.",
    )
    register.add_argument(
        "--listen-url",
        default=None,
        help=argparse.SUPPRESS,
    )
    register.add_argument(
        "--shared-secret",
        default=None,
        help=argparse.SUPPRESS,
    )
    register.set_defaults(func=cmd_register)

    reconcile = orchestrator_subparsers.add_parser(
        "reconcile",
        help="Poll Jira for ai:auto issues and repair missed events.",
    )
    reconcile.add_argument("--config", default=None, help="Override the worker config path.")
    reconcile.add_argument("--project", default=None, help="Only reconcile a single Jira project key.")
    reconcile.set_defaults(func=cmd_reconcile)

    poll = orchestrator_subparsers.add_parser(
        "poll",
        help="Refresh GitHub check/review state for waiting jobs without running the resident loop.",
    )
    poll.add_argument("--config", default=None, help="Override the worker config path.")
    poll.add_argument("--issue", default=None, help="Only refresh a single Jira issue key.")
    poll.add_argument("--project", default=None, help="Only refresh a single Jira project key.")
    poll.set_defaults(func=cmd_poll)

    status = orchestrator_subparsers.add_parser(
        "status",
        help="Show orchestrator state by issue, project, or all tracked jobs.",
    )
    status.add_argument("--config", default=None, help="Override the worker config path.")
    status.add_argument("--issue", default=None, help="Filter by Jira issue key.")
    status.add_argument("--project", default=None, help="Filter by Jira project key.")
    status.add_argument(
        "--refresh",
        action="store_true",
        help="Poll GitHub and refresh Jira sticky comments before printing status.",
    )
    status.set_defaults(func=cmd_status)

    pause = orchestrator_subparsers.add_parser(
        "pause", help="Pause one issue, one project, or the entire worker."
    )
    pause.add_argument("--config", default=None, help="Override the worker config path.")
    pause.add_argument("--issue", default=None, help="Pause a single issue.")
    pause.add_argument("--project", default=None, help="Pause all issues in a Jira project.")
    pause.add_argument("--global", dest="global_pause", action="store_true", help="Pause the whole worker.")
    pause.set_defaults(func=cmd_pause)

    resume = orchestrator_subparsers.add_parser(
        "resume", help="Resume one issue, one project, or the entire worker."
    )
    resume.add_argument("--config", default=None, help="Override the worker config path.")
    resume.add_argument("--issue", default=None, help="Resume a single issue.")
    resume.add_argument("--project", default=None, help="Resume all issues in a Jira project.")
    resume.add_argument("--global", dest="global_resume", action="store_true", help="Resume the whole worker.")
    resume.set_defaults(func=cmd_resume)

    cancel = orchestrator_subparsers.add_parser(
        "cancel", help="Cancel a single in-flight issue and keep its branch/PR/worktree."
    )
    cancel.add_argument("--config", default=None, help="Override the worker config path.")
    cancel.add_argument("--issue", required=True, help="Issue key to cancel.")
    cancel.set_defaults(func=cmd_cancel)

    retry = orchestrator_subparsers.add_parser(
        "retry",
        help="Requeue a failed or blocked issue from the last successful checkpoint.",
    )
    retry.add_argument("--config", default=None, help="Override the worker config path.")
    retry.add_argument("--issue", required=True, help="Issue key to retry.")
    retry.set_defaults(func=cmd_retry)

    fail = orchestrator_subparsers.add_parser(
        "fail",
        help="Mark an issue failed and optionally return it to the Jira backlog.",
    )
    fail.add_argument("--config", default=None, help="Override the worker config path.")
    fail.add_argument("--issue", required=True, help="Issue key to mark failed.")
    fail.add_argument("--reason", default="operator marked failed", help="Failure reason.")
    fail.add_argument("--backlog", action="store_true", help="Best-effort transition back to To Do / Backlog.")
    fail.set_defaults(func=cmd_fail)

    gate = orchestrator_subparsers.add_parser(
        "gate",
        help="Inspect and unblock issue-level quality gates without stopping the whole batch.",
    )
    gate_subparsers = gate.add_subparsers(dest="gate_command", required=True)
    gate_status = gate_subparsers.add_parser("status", help="Show gate/quarantine state.")
    gate_status.add_argument("--config", default=None, help="Override the worker config path.")
    gate_status.add_argument("--issue", default=None, help="Filter by Jira issue key.")
    gate_status.add_argument("--batch", default=None, help="Filter by batch id.")
    gate_status.add_argument("--project", default=None, help="Filter by Jira project key.")
    gate_status.set_defaults(func=cmd_gate_status)
    gate_unblock = gate_subparsers.add_parser("unblock", help="Requeue an issue after a human gate is resolved.")
    gate_unblock.add_argument("--config", default=None, help="Override the worker config path.")
    gate_unblock.add_argument("--issue", required=True, help="Issue key to unblock.")
    gate_unblock.add_argument("--reason", required=True, help="Human-readable unblock reason.")
    gate_unblock.set_defaults(func=cmd_gate_unblock)

    batch = orchestrator_subparsers.add_parser(
        "batch",
        help="Create and control multi-issue mediated development batches.",
    )
    batch_subparsers = batch.add_subparsers(dest="batch_command", required=True)
    batch_create = batch_subparsers.add_parser("create", help="Create a batch from a Jira JQL query.")
    batch_create.add_argument("--config", default=None, help="Override the worker config path.")
    batch_create.add_argument("--project", required=True, help="Jira project key.")
    batch_create.add_argument("--jql", required=True, help="JQL selecting issues for the batch.")
    batch_create.add_argument("--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL_PER_REPO)
    batch_create.set_defaults(func=cmd_batch_create)
    batch_status = batch_subparsers.add_parser("status", help="Show batch status.")
    batch_status.add_argument("--config", default=None, help="Override the worker config path.")
    batch_status.add_argument("--batch", default=None, help="Batch id.")
    batch_status.set_defaults(func=cmd_batch_status)
    for name, help_text in (
        ("pause", "Pause a batch."),
        ("resume", "Resume a batch."),
        ("cancel", "Cancel a batch."),
    ):
        parser = batch_subparsers.add_parser(name, help=help_text)
        parser.add_argument("--config", default=None, help="Override the worker config path.")
        parser.add_argument("--batch", required=True, help="Batch id.")
        parser.set_defaults(func=cmd_batch_control)

    install_agent = orchestrator_subparsers.add_parser(
        "install-agent",
        help="Install a macOS LaunchAgent that restarts the polling worker at login.",
    )
    install_agent.add_argument("--config", default=None, help="Worker config path to pass to the agent.")
    install_agent.add_argument("--label", default=LAUNCH_AGENT_LABEL, help="LaunchAgent label.")
    install_agent.add_argument("--platform-root", default=str(REPO_ROOT), help="Platform source repo path.")
    install_agent.add_argument("--dry-run", action="store_true", help="Print the plist without writing or loading it.")
    install_agent.set_defaults(func=cmd_install_agent)

    uninstall_agent = orchestrator_subparsers.add_parser(
        "uninstall-agent",
        help="Unload and remove the macOS LaunchAgent.",
    )
    uninstall_agent.add_argument("--label", default=LAUNCH_AGENT_LABEL, help="LaunchAgent label.")
    uninstall_agent.add_argument("--dry-run", action="store_true", help="Print the target plist path without removing it.")
    uninstall_agent.set_defaults(func=cmd_uninstall_agent)

    agent_status = orchestrator_subparsers.add_parser(
        "agent-status",
        help="Show macOS LaunchAgent status for the polling worker.",
    )
    agent_status.add_argument("--label", default=LAUNCH_AGENT_LABEL, help="LaunchAgent label.")
    agent_status.set_defaults(func=cmd_agent_status)


def cmd_configure(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    config = load_orchestrator_config(config_path)
    if args.bind_host is not None:
        config["bind_host"] = args.bind_host
    if args.bind_port is not None:
        config["bind_port"] = args.bind_port
    if args.event_mode is not None:
        config["event_mode"] = args.event_mode
    if args.clear_public_base_url:
        config["public_base_url"] = ""
    if args.public_base_url is not None:
        config["public_base_url"] = normalize_public_base_url(args.public_base_url)
    if args.project_root:
        roots = [str(Path(item).expanduser().resolve()) for item in config.get("projects_roots", [])]
        for root in args.project_root:
            resolved = str(Path(root).expanduser().resolve())
            if resolved not in roots:
                roots.append(resolved)
        config["projects_roots"] = roots
    if args.jira_site_url is not None:
        config["jira_site_url"] = normalize_public_base_url(args.jira_site_url)
    if args.jira_admin_email is not None:
        config["jira_admin_email"] = args.jira_admin_email
    if (
        args.codex_model is not None
        or args.codex_binary is not None
        or args.codex_use_user_config
        or args.codex_ignore_user_config
        or args.claude_model is not None
        or args.claude_effort is not None
    ):
        ai_config = dict(config.get("ai", {}))
        if args.codex_model is not None:
            ai_config["codex_model"] = args.codex_model.strip()
        if args.codex_binary is not None:
            ai_config["codex_binary"] = args.codex_binary.strip() or DEFAULT_CODEX_BINARY
        if args.codex_use_user_config:
            ai_config["codex_ignore_user_config"] = False
        if args.codex_ignore_user_config:
            ai_config["codex_ignore_user_config"] = True
        if args.claude_model is not None:
            ai_config["claude_model"] = args.claude_model.strip()
        if args.claude_effort is not None:
            ai_config["claude_effort"] = args.claude_effort.strip()
        config["ai"] = ai_config
    if (
        getattr(args, "max_parallel_per_repo", None) is not None
        or getattr(args, "max_parallel_per_project", None) is not None
        or getattr(args, "max_baton_rounds", None) is not None
    ):
        scheduler_config = dict(config.get("scheduler", {}))
        if getattr(args, "max_parallel_per_repo", None) is not None:
            scheduler_config["max_parallel_per_repo"] = max(1, int(args.max_parallel_per_repo))
        if getattr(args, "max_parallel_per_project", None) is not None:
            scheduler_config["max_parallel_per_project"] = max(1, int(args.max_parallel_per_project))
        if getattr(args, "max_baton_rounds", None) is not None:
            scheduler_config["max_baton_rounds"] = max(0, int(args.max_baton_rounds))
        scheduler_config.setdefault("contract_handshake", DEFAULT_CONTRACT_HANDSHAKE)
        config["scheduler"] = scheduler_config
    if (
        getattr(args, "codex_exec_timeout", None) is not None
        or getattr(args, "codex_review_timeout", None) is not None
        or getattr(args, "claude_timeout", None) is not None
    ):
        timeout_config = dict(config.get("timeouts", {}))
        if getattr(args, "codex_exec_timeout", None) is not None:
            timeout_config["codex_exec_seconds"] = max(1, int(args.codex_exec_timeout))
        if getattr(args, "codex_review_timeout", None) is not None:
            timeout_config["codex_review_seconds"] = max(1, int(args.codex_review_timeout))
        if getattr(args, "claude_timeout", None) is not None:
            timeout_config["claude_seconds"] = max(1, int(args.claude_timeout))
        config["timeouts"] = timeout_config
    if getattr(args, "failure_max_attempts", None) is not None:
        failure_config = dict(config.get("failure", {}))
        failure_config["max_attempts"] = max(1, int(args.failure_max_attempts))
        config["failure"] = failure_config
    if getattr(args, "github_merge_policy", None) is not None:
        github_config = dict(config.get("github", {}))
        github_config["merge_policy"] = args.github_merge_policy
        config["github"] = github_config
    save_orchestrator_config(config, config_path)
    print(f"Saved orchestrator config: {config_path}")
    print(json.dumps(config, indent=2, ensure_ascii=False))
    if config.get("event_mode") == "webhook" and config.get("public_base_url"):
        print("Re-run `platform orchestrator register --target <repo>` for each project to update Jira Automation callbacks.")
    else:
        print("Polling mode is active. No public URL or Jira Automation callback is required.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_worker_settings(
        config_override=args.config,
        bind_host=args.bind_host,
        bind_port=args.bind_port,
        event_mode="polling" if args.poll_only else args.event_mode,
        public_base_url=args.public_base_url,
        listen_url=args.listen_url,
        project_roots=args.project_root,
        jira_site_url=args.jira_site_url,
        jira_admin_email=args.jira_admin_email,
    )
    require_runtime_credentials(settings)
    store = OrchestratorStore(settings.db_path)
    store.record_worker_event(
        "toolchain",
        {
            "codex_binary": settings.codex_resolved_binary,
            "codex_version": settings.codex_resolved_version,
            "compatible": settings.codex_toolchain_compatible,
            "source_repo": str(REPO_ROOT),
        },
    )
    print(
        "Codex toolchain: "
        f"{settings.codex_resolved_binary or 'unresolved'} "
        f"({settings.codex_resolved_version or 'unknown'})"
    )
    service = OrchestratorService(settings=settings, store=store)
    service.run(once=args.once, enable_http=(not args.no_http and settings.event_mode == "webhook"))
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser().resolve()
    project = load_repo_project(target)
    settings = load_worker_settings(
        config_override=args.config,
        bind_host=args.bind_host,
        bind_port=args.bind_port,
        event_mode="webhook" if args.webhook else args.event_mode,
        public_base_url=args.public_base_url,
        listen_url=args.listen_url,
        project_roots=[str(project.repo_path.parent)],
    )
    store = OrchestratorStore(settings.db_path)
    existing = store.project(project.project_key)
    if existing and Path(existing["repo_path"]).expanduser().resolve() != project.repo_path:
        raise OrchestratorError(
            f"Jira project key `{project.project_key}` is already registered to "
            f"{existing['repo_path']}."
        )
    store.sync_projects([project])
    current_registration = store.project(project.project_key) or {}
    project_id = str(current_registration.get("jira_project_id", ""))
    control_issue_key = str(current_registration.get("control_issue_key", ""))
    lifecycle_rule_uuid = str(current_registration.get("lifecycle_rule_uuid", ""))
    comment_rule_uuid = str(current_registration.get("comment_rule_uuid", ""))
    previous_rule_uuids = [rule for rule in (lifecycle_rule_uuid, comment_rule_uuid) if rule]
    webhook_secret = str(current_registration.get("webhook_secret", ""))
    provided_secret = args.webhook_secret or args.shared_secret
    disabled_legacy_rules: list[str] = []
    if settings.event_mode == "webhook":
        webhook_secret = store.ensure_project_webhook_secret(
            project.project_key,
            provided_secret,
        )
    else:
        webhook_secret = ""
        lifecycle_rule_uuid = ""
        comment_rule_uuid = ""
    api_token = atlassian_api_token()
    if settings.jira_site_url and settings.jira_admin_email and api_token:
        if settings.event_mode != "webhook":
            disabled_legacy_rules = disable_automation_rules(settings, previous_rule_uuids)
        project_details = jira_get_project(
            site_url=settings.jira_site_url,
            admin_email=settings.jira_admin_email,
            api_token=api_token,
            project_key=project.project_key,
        )
        project_id = str(project_details.get("id", ""))
        control_issue_key = ensure_control_issue(settings, project)
        if settings.event_mode == "webhook":
            if settings.public_base_url:
                rule_result = ensure_automation_rules(settings, project, project_id, webhook_secret)
                lifecycle_rule_uuid = rule_result.get("lifecycle_rule_uuid", "")
                comment_rule_uuid = rule_result.get("comment_rule_uuid", "")
            else:
                export_automation_rule_blueprints(settings, project)
    elif settings.event_mode == "webhook":
        export_automation_rule_blueprints(settings, project)

    store.update_project_registration(
        project_key=project.project_key,
        jira_project_id=project_id,
        control_issue_key=control_issue_key,
        lifecycle_rule_uuid=lifecycle_rule_uuid,
        comment_rule_uuid=comment_rule_uuid,
        webhook_secret=webhook_secret,
    )
    print("Orchestrator registration complete")
    print(f"- repo: {project.repo_path}")
    print(f"- project key: {project.project_key}")
    print(f"- config: {settings.config_path}")
    print(f"- db: {settings.db_path}")
    print(f"- event mode: {settings.event_mode}")
    if settings.event_mode == "webhook" and settings.public_base_url:
        print(f"- endpoint: {settings.public_base_url}/jira/events/{project.project_key}")
    else:
        print("- endpoint: polling mode uses outbound Jira REST; no callback URL required")
    if control_issue_key:
        print(f"- control issue: {control_issue_key}")
    if lifecycle_rule_uuid or comment_rule_uuid:
        print(f"- lifecycle rule: {lifecycle_rule_uuid or 'skipped'}")
        print(f"- comment rule: {comment_rule_uuid or 'skipped'}")
    elif settings.event_mode == "webhook":
        print("- automation rules: exported as blueprints (live API setup skipped)")
    else:
        print("- automation rules: skipped (polling mode)")
        if disabled_legacy_rules:
            print(f"- disabled legacy automation rules: {len(disabled_legacy_rules)}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    settings = load_worker_settings(config_override=args.config)
    require_runtime_credentials(settings)
    store = OrchestratorStore(settings.db_path)
    service = OrchestratorService(settings=settings, store=store)
    service.sync_projects()
    count = service.reconcile_projects(project_key=args.project.upper() if args.project else None)
    print(f"Reconcile complete: enqueued {count} issue(s)")
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    settings = load_worker_settings(config_override=args.config)
    require_runtime_credentials(settings)
    store = OrchestratorStore(settings.db_path)
    service = OrchestratorService(settings=settings, store=store)
    count = service.poll_github_jobs(
        issue_key=args.issue.upper() if args.issue else None,
        project_key=args.project.upper() if args.project else None,
    )
    print(f"GitHub poll complete: refreshed {count} waiting job(s)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_worker_settings(config_override=args.config)
    store = OrchestratorStore(settings.db_path)
    if args.refresh:
        require_runtime_credentials(settings)
        service = OrchestratorService(settings=settings, store=store)
        service.poll_github_jobs(
            issue_key=args.issue.upper() if args.issue else None,
            project_key=args.project.upper() if args.project else None,
        )
    rows = store.list_jobs(
        issue_key=args.issue.upper() if args.issue else None,
        project_key=args.project.upper() if args.project else None,
    )
    flags = store.list_control_flags()
    worker_event = store.latest_worker_event("toolchain")
    payload = {
        "jobs": rows,
        "control_flags": flags,
        "worker": worker_event,
        "hints": status_hints(rows, refreshed=args.refresh, settings=settings, worker_event=worker_event),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    return update_pause_state(
        config_override=args.config,
        issue_key=args.issue.upper() if args.issue else None,
        project_key=args.project.upper() if args.project else None,
        global_scope=args.global_pause,
        value="paused",
        label="Pause",
    )


def cmd_resume(args: argparse.Namespace) -> int:
    return update_pause_state(
        config_override=args.config,
        issue_key=args.issue.upper() if args.issue else None,
        project_key=args.project.upper() if args.project else None,
        global_scope=args.global_resume,
        value="running",
        label="Resume",
    )


def cmd_cancel(args: argparse.Namespace) -> int:
    settings = load_worker_settings(config_override=args.config)
    store = OrchestratorStore(settings.db_path)
    issue_key = args.issue.upper()
    store.set_requested_action(issue_key, "cancel")
    signal_active_process(store.get_job(issue_key).get("active_pid"))
    print(f"Cancel requested for {issue_key}")
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    settings = load_worker_settings(config_override=args.config)
    store = OrchestratorStore(settings.db_path)
    issue_key = args.issue.upper()
    retry_issue_job(store, issue_key, reason="operator retry")
    print(f"Retry queued for {issue_key}")
    return 0


def cmd_fail(args: argparse.Namespace) -> int:
    settings = load_worker_settings(config_override=args.config)
    store = OrchestratorStore(settings.db_path)
    issue_key = args.issue.upper()
    fail_issue_job(
        store,
        settings,
        issue_key,
        reason=args.reason,
        backlog=bool(args.backlog),
    )
    OrchestratorService(settings=settings, store=store).refresh_issue_report(issue_key)
    print(f"Marked failed: {issue_key}")
    return 0


def cmd_gate_status(args: argparse.Namespace) -> int:
    settings = load_worker_settings(config_override=args.config)
    store = OrchestratorStore(settings.db_path)
    if args.batch:
        rows = store.list_jobs_for_batch(args.batch)
    else:
        rows = store.list_jobs(
            issue_key=args.issue.upper() if args.issue else None,
            project_key=args.project.upper() if args.project else None,
        )
    payload = {
        "gates": [gate_status_row(row) for row in rows],
        "batches": store.list_batches(batch_id=args.batch) if args.batch else [],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_gate_unblock(args: argparse.Namespace) -> int:
    settings = load_worker_settings(config_override=args.config)
    store = OrchestratorStore(settings.db_path)
    issue_key = args.issue.upper()
    unblock_gate_issue(store, issue_key, reason=args.reason)
    OrchestratorService(settings=settings, store=store).refresh_issue_report(
        issue_key,
        extra_notice=f"Gate unblocked: {args.reason}",
    )
    print(f"Gate unblocked and queued for {issue_key}")
    return 0


def cmd_batch_create(args: argparse.Namespace) -> int:
    settings = load_worker_settings(config_override=args.config)
    require_runtime_credentials(settings)
    store = OrchestratorStore(settings.db_path)
    service = OrchestratorService(settings=settings, store=store)
    service.sync_projects()
    batch = create_batch_from_jql(
        store=store,
        settings=settings,
        project_key=args.project.upper(),
        jql=args.jql,
        max_parallel=max(1, int(args.max_parallel)),
    )
    print(json.dumps(batch, indent=2, ensure_ascii=False))
    return 0


def cmd_batch_status(args: argparse.Namespace) -> int:
    settings = load_worker_settings(config_override=args.config)
    store = OrchestratorStore(settings.db_path)
    print(json.dumps(store.list_batches(batch_id=args.batch), indent=2, ensure_ascii=False))
    return 0


def cmd_batch_control(args: argparse.Namespace) -> int:
    settings = load_worker_settings(config_override=args.config)
    store = OrchestratorStore(settings.db_path)
    command = str(args.batch_command)
    state = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[command]
    store.update_batch_state(args.batch, state)
    if command == "cancel":
        for job in store.list_jobs_for_batch(args.batch):
            store.set_requested_action(str(job["issue_key"]), "cancel")
            signal_active_process(job.get("active_pid"))
            if str(job.get("state", "")) == "queued":
                store.update_job(str(job["issue_key"]), state="cancelled", requested_action="")
    print(f"Batch {args.batch} {command} requested")
    return 0


def cmd_install_agent(args: argparse.Namespace) -> int:
    platform_root = Path(args.platform_root).expanduser().resolve()
    plist_path = launch_agent_plist_path(args.label)
    log_dir = launch_agent_log_dir()
    config_path = Path(args.config).expanduser().resolve() if args.config else None
    plist = build_launch_agent_plist(
        label=args.label,
        platform_root=platform_root,
        config_path=config_path,
        log_dir=log_dir,
    )
    if args.dry_run:
        print(plistlib.dumps(plist, sort_keys=False).decode("utf-8"))
        return 0
    if sys.platform != "darwin":
        raise OrchestratorError("LaunchAgent install is only supported on macOS. Use systemd on Linux.")
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps(plist, sort_keys=False))
    unload_launch_agent(args.label, plist_path, ignore_errors=True)
    run_command(["launchctl", "bootstrap", launchctl_domain(), str(plist_path)])
    run_command(["launchctl", "kickstart", "-k", f"{launchctl_domain()}/{args.label}"])
    print(f"Installed LaunchAgent: {plist_path}")
    return 0


def cmd_uninstall_agent(args: argparse.Namespace) -> int:
    plist_path = launch_agent_plist_path(args.label)
    if args.dry_run:
        print(f"Would unload {args.label} and remove {plist_path}")
        return 0
    if sys.platform != "darwin":
        raise OrchestratorError("LaunchAgent uninstall is only supported on macOS.")
    unload_launch_agent(args.label, plist_path, ignore_errors=True)
    if plist_path.exists():
        plist_path.unlink()
    print(f"Uninstalled LaunchAgent: {plist_path}")
    return 0


def cmd_agent_status(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        raise OrchestratorError("LaunchAgent status is only supported on macOS.")
    result = run_optional(["launchctl", "print", f"{launchctl_domain()}/{args.label}"])
    if result and result.returncode == 0:
        print(result.stdout.rstrip())
        return 0
    print(f"LaunchAgent is not loaded: {args.label}")
    return 1


def update_pause_state(
    *,
    config_override: str | None,
    issue_key: str | None,
    project_key: str | None,
    global_scope: bool,
    value: str,
    label: str,
) -> int:
    if sum(bool(item) for item in (issue_key, project_key, global_scope)) != 1:
        raise SystemExit("Exactly one of --issue, --project, or --global is required.")
    settings = load_worker_settings(config_override=config_override)
    store = OrchestratorStore(settings.db_path)
    if issue_key:
        if value == "paused":
            store.set_requested_action(issue_key, "pause")
        else:
            store.clear_requested_action(issue_key)
            job = store.get_job(issue_key)
            if job and job["state"] == "paused":
                store.update_job(
                    issue_key,
                    state="queued",
                    requested_action="",
                )
        print(f"{label} requested for {issue_key}")
        return 0
    scope_type = "global" if global_scope else "project"
    scope_key = "*" if global_scope else project_key or ""
    store.set_control_flag(scope_type, scope_key, "pause", value)
    print(f"{label} applied to {scope_type}:{scope_key}")
    return 0


def retry_issue_job(store: "OrchestratorStore", issue_key: str, *, reason: str) -> None:
    job = store.get_job(issue_key)
    if not job:
        raise OrchestratorError(f"Job not found: {issue_key}")
    retryable_states = {"failed", "blocked", "gate_failed", "gate_waiting_human", "waiting_dependency", "backlog"}
    if str(job.get("state", "")) not in retryable_states:
        raise OrchestratorError(
            f"Job {issue_key} is `{job.get('state')}`; retry is only allowed for {', '.join(sorted(retryable_states))} jobs."
        )
    signal_active_process(job.get("active_pid"))
    store.release_lease(str(job.get("repo_path", "")), issue_key)
    store.update_job(
        issue_key,
        state="queued",
        latest_error="",
        requested_action="",
        active_pid=None,
        gate_state="",
        gate_reason="",
        blocked_dependencies_json="[]",
    )
    store.record_step(
        issue_key,
        "retry",
        "success",
        {
            "summary": f"Retry queued: {reason}",
            "reason": reason,
            "previous_state": str(job.get("state", "")),
        },
    )


def fail_issue_job(
    store: "OrchestratorStore",
    settings: WorkerSettings,
    issue_key: str,
    *,
    reason: str,
    backlog: bool,
) -> None:
    job = store.get_job(issue_key)
    if not job:
        raise OrchestratorError(f"Job not found: {issue_key}")
    signal_active_process(job.get("active_pid"))
    store.release_lease(str(job.get("repo_path", "")), issue_key)
    attempt_count = int(job.get("attempt_count") or 0) + 1
    final_state = "backlog" if backlog else "failed"
    store.update_job(
        issue_key,
        state=final_state,
        latest_error=reason,
        active_pid=None,
        requested_action="",
        attempt_count=attempt_count,
        gate_state="",
        gate_reason="",
        blocked_dependencies_json="[]",
    )
    payload = {
        "summary": reason,
        "backlog_requested": backlog,
        "attempt": attempt_count,
        "retry_command": f"platform orchestrator retry --issue {issue_key}",
    }
    if backlog:
        transition = transition_jira_issue_to_aliases(
            settings,
            issue_key,
            getattr(settings, "failure_backlog_statuses", DEFAULT_FAILURE_BACKLOG_STATUSES),
        )
        payload["backlog_transition"] = transition
        if transition.get("status_name"):
            store.update_job(issue_key, status_name=str(transition["status_name"]))
    store.record_step(issue_key, final_state, "failed", payload)
    store.record_attempt(
        issue_key,
        batch_id=str(job.get("batch_id", "")),
        attempt=attempt_count,
        step_name=final_state,
        status="failed",
        classification=classify_failure_reason(reason),
        payload=payload,
    )


def retry_transient_or_fail_issue_job(
    store: "OrchestratorStore",
    settings: WorkerSettings,
    issue_key: str,
    *,
    reason: str,
    backlog: bool,
) -> str:
    job = store.get_job(issue_key)
    if not job:
        raise OrchestratorError(f"Job not found: {issue_key}")
    classification = classify_failure_reason(reason)
    next_attempt = int(job.get("attempt_count") or 0) + 1
    max_attempts = int(getattr(settings, "failure_max_attempts", DEFAULT_FAILURE_MAX_ATTEMPTS))
    if classification == "transient_network_or_timeout" and next_attempt < max_attempts:
        signal_active_process(job.get("active_pid"))
        store.release_lease(str(job.get("repo_path", "")), issue_key)
        payload = {
            "summary": reason,
            "classification": classification,
            "attempt": next_attempt,
            "max_attempts": max_attempts,
            "next_state": "queued",
        }
        store.update_job(
            issue_key,
            state="queued",
            latest_error=f"Transient failure; retrying attempt {next_attempt + 1}/{max_attempts}: {reason}",
            active_pid=None,
            requested_action="",
            attempt_count=next_attempt,
        )
        store.record_step(issue_key, "transient_retry", "warning", payload)
        store.record_attempt(
            issue_key,
            batch_id=str(job.get("batch_id", "")),
            attempt=next_attempt,
            step_name="transient_retry",
            status="retrying",
            classification=classification,
            payload=payload,
        )
        return "queued"
    fail_issue_job(store, settings, issue_key, reason=reason, backlog=backlog)
    return "backlog" if backlog else "failed"


def classify_failure_reason(reason: str) -> str:
    text = reason.lower()
    if any(fragment in text for fragment in ("nodename nor servname", "temporary failure", "timed out", "timeout")):
        return "transient_network_or_timeout"
    if "codex" in text and ("version" in text or "argument" in text or "option" in text):
        return "codex_cli_incompatible"
    if "validation" in text or "test" in text:
        return "validation_failure"
    return "failed"


def dependencies_satisfied(store: "OrchestratorStore", job: dict[str, Any]) -> bool:
    return dependency_status(store, job)["satisfied"]


def dependency_status(store: "OrchestratorStore", job: dict[str, Any]) -> dict[str, Any]:
    dependencies = parse_dependencies(job.get("dependencies_json"))
    blocked: list[dict[str, str]] = []
    waiting: list[dict[str, str]] = []
    for dependency in dependencies:
        dep_job = store.get_job(dependency)
        state = str(dep_job.get("state", "")) if dep_job else "missing"
        if state == "done":
            continue
        item = {"issue_key": dependency, "state": state}
        if state in DEPENDENCY_BLOCKING_STATES:
            blocked.append(item)
        else:
            waiting.append(item)
    return {
        "dependencies": dependencies,
        "satisfied": not blocked and not waiting,
        "blocked": blocked,
        "waiting": waiting,
    }


def batch_effective_state(batch: dict[str, Any], jobs: list[dict[str, Any]]) -> str:
    explicit = str(batch.get("state", ""))
    if explicit in {"paused", "cancelled"}:
        return explicit
    if not jobs:
        return explicit or "running"
    states = {str(job.get("state", "")) for job in jobs}
    if states and states <= {"done"}:
        return "done"
    degraded_states = {
        "gate_failed",
        "gate_waiting_human",
        "waiting_dependency",
        "blocked",
        "failed",
        "backlog",
        "cancelled",
    }
    if states & degraded_states:
        return "degraded"
    return explicit or "running"


def gate_status_row(job: dict[str, Any]) -> dict[str, Any]:
    blocked_dependencies = parse_blocked_dependencies(job.get("blocked_dependencies_json"))
    return {
        "issue_key": job.get("issue_key", ""),
        "project_key": job.get("project_key", ""),
        "batch_id": job.get("batch_id", ""),
        "state": job.get("state", ""),
        "gate_state": job.get("gate_state", "") or inferred_gate_state(job),
        "gate_reason": job.get("gate_reason", "") or job.get("latest_error", ""),
        "blocked_dependencies": blocked_dependencies,
        "pr_url": job.get("pr_url", ""),
        "next_operator_action": next_operator_action(job, blocked_dependencies),
    }


def inferred_gate_state(job: dict[str, Any]) -> str:
    state = str(job.get("state", ""))
    if state in GATE_STATES or state in DEPENDENCY_WAIT_STATES:
        return state
    return ""


def parse_blocked_dependencies(value: Any) -> list[dict[str, str]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not value:
        return []
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    return [item for item in decoded if isinstance(item, dict)] if isinstance(decoded, list) else []


def next_operator_action(job: dict[str, Any], blocked_dependencies: list[dict[str, str]] | None = None) -> str:
    state = str(job.get("state", ""))
    issue_key = str(job.get("issue_key", ""))
    if state == "gate_waiting_human":
        return f"Resolve the human gate, then run `platform orchestrator gate unblock --issue {issue_key} --reason <reason>` or comment `/ai unblock`."
    if state == "gate_failed":
        return f"Fix the failing gate, then run `platform orchestrator gate unblock --issue {issue_key} --reason <reason>` or `platform orchestrator fail --issue {issue_key} --backlog`."
    if state == "waiting_dependency":
        deps = ", ".join(str(item.get("issue_key", "")) for item in (blocked_dependencies or []) if item.get("issue_key"))
        return f"Waiting for dependency resolution: {deps or 'dependency not done'}."
    if state in {"failed", "blocked", "backlog"}:
        return f"Run `platform orchestrator retry --issue {issue_key}` after fixing the blocker."
    return "none"


def unblock_gate_issue(store: "OrchestratorStore", issue_key: str, *, reason: str) -> None:
    job = store.get_job(issue_key)
    if not job:
        raise OrchestratorError(f"Job not found: {issue_key}")
    state = str(job.get("state", ""))
    allowed = {*GATE_STATES, *DEPENDENCY_WAIT_STATES, "blocked", "failed"}
    if state not in allowed:
        raise OrchestratorError(f"Job {issue_key} is `{state}`; gate unblock is only allowed for quarantined jobs.")
    signal_active_process(job.get("active_pid"))
    store.release_lease(str(job.get("repo_path", "")), issue_key)
    store.update_job(
        issue_key,
        state="queued",
        latest_error="",
        requested_action="",
        active_pid=None,
        gate_state="",
        gate_reason="",
        blocked_dependencies_json="[]",
    )
    store.record_step(
        issue_key,
        "gate_unblock",
        "success",
        {
            "summary": f"Gate unblocked: {reason}",
            "reason": reason,
            "previous_state": state,
        },
    )


def parse_dependencies(value: Any) -> list[str]:
    if isinstance(value, list):
        return [normalize_issue_key(item) for item in value if normalize_issue_key(item)]
    if not value:
        return []
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [normalize_issue_key(item) for item in decoded if normalize_issue_key(item)]


def status_hints(
    rows: list[dict[str, Any]],
    *,
    refreshed: bool,
    settings: WorkerSettings | None = None,
    worker_event: dict[str, Any] | None = None,
) -> list[str]:
    hints: list[str] = []
    if refreshed:
        return hints
    if any(str(row.get("state", "")) in WAITING_STATES for row in rows):
        hints.append(
            "Waiting job state may be stale if the worker is not running. "
            "Run `platform orchestrator poll` or `platform orchestrator status --refresh` to refresh GitHub checks/reviews and Jira reporting."
        )
    if settings and not settings.codex_toolchain_compatible:
        hints.append("Codex toolchain is not compatible. Run `platform toolchain doctor`.")
    worker_payload = (worker_event or {}).get("payload", {})
    worker_source_repo = str(worker_payload.get("source_repo", "")) if isinstance(worker_payload, dict) else ""
    if worker_source_repo and worker_source_repo != str(REPO_ROOT):
        hints.append(
            "Last recorded worker source repo differs from this CLI source. "
            f"Worker: {worker_source_repo}. CLI: {REPO_ROOT}. Restart the worker from the current source repo."
        )
    if worker_source_repo.endswith("-v0.1.11"):
        hints.append(
            f"Last recorded worker source repo looks stale: {worker_source_repo}. "
            f"Restart from the {PLATFORM_RELEASE_VERSION} source repo."
        )
    if str(REPO_ROOT).endswith("-v0.1.11"):
        hints.append(
            f"Worker source repo looks stale: {REPO_ROOT}. "
            f"Restart from the {PLATFORM_RELEASE_VERSION} source repo."
        )
    return hints


class OrchestratorService:
    def __init__(self, *, settings: WorkerSettings, store: "OrchestratorStore") -> None:
        self.settings = settings
        self.store = store
        self.stop_event = threading.Event()
        self.thread_lock = threading.Lock()
        self.active_threads: dict[str, threading.Thread] = {}
        self.next_reconcile_at = 0.0
        self.next_github_poll_at = 0.0

    def run(self, *, once: bool, enable_http: bool) -> None:
        self.sync_projects()
        self.store.clear_all_leases()
        self.store.recover_inflight_jobs()
        server = self.start_http_server() if enable_http else None
        try:
            while not self.stop_event.is_set():
                self.sync_projects()
                now = time.monotonic()
                if now >= self.next_reconcile_at:
                    self.reconcile_projects()
                    self.next_reconcile_at = now + self.settings.poll_intervals["reconcile_seconds"]
                if now >= self.next_github_poll_at:
                    self.poll_github_jobs()
                    self.next_github_poll_at = now + self.settings.poll_intervals["github_seconds"]
                self.dispatch_jobs()
                self.collect_finished_threads()
                if once:
                    return
                time.sleep(max(1, self.settings.poll_intervals["loop_seconds"]))
        finally:
            if server:
                server.shutdown()
                server.server_close()

    def start_http_server(self) -> ThreadingHTTPServer:
        host, port = self.settings.bind_host, self.settings.bind_port
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                service.handle_http_get(self)

            def do_POST(self) -> None:  # noqa: N802
                service.handle_http_post(self)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        server = ThreadingHTTPServer((host, port), Handler)
        thread = threading.Thread(target=server.serve_forever, name="platform-orchestrator-http", daemon=True)
        thread.start()
        return server

    def handle_http_get(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.path == "/healthz":
            self.write_health_response(handler)
            return
        handler.send_error(HTTPStatus.NOT_FOUND)

    def handle_http_post(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.path == "/healthz":
            self.write_health_response(handler)
            return
        match = EVENT_PATH_RE.fullmatch(handler.path)
        if not match:
            handler.send_error(HTTPStatus.NOT_FOUND)
            return
        project_key = str(match.group("project_key")).upper()
        project = self.store.project(project_key)
        if not project:
            handler.send_error(HTTPStatus.NOT_FOUND, "unknown Jira project key")
            return
        header_value = handler.headers.get(DEFAULT_HEADER_NAME) or handler.headers.get(LEGACY_HEADER_NAME)
        expected_secret = str(project.get("webhook_secret", ""))
        if expected_secret and header_value != expected_secret:
            handler.send_error(HTTPStatus.FORBIDDEN, "webhook secret mismatch")
            return
        length = int(handler.headers.get("Content-Length", "0"))
        raw_payload = handler.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw_payload or "{}")
        except json.JSONDecodeError:
            handler.send_error(HTTPStatus.BAD_REQUEST, "invalid json")
            return
        payload["project_key"] = project_key
        self.handle_event(payload)
        handler.send_response(HTTPStatus.ACCEPTED)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(b'{"accepted":true}')

    def write_health_response(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        payload = {"status": "ok", "time": now_iso()}
        handler.wfile.write(json.dumps(payload).encode("utf-8"))

    def sync_projects(self) -> list[RepoProject]:
        projects = discover_projects(self.settings.projects_roots)
        self.store.sync_projects(projects)
        return projects

    def handle_event(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("event_type", "")).strip()
        issue_key = normalize_issue_key(payload.get("issue_key"))
        project_key = str(payload.get("project_key", "")).upper()
        if not project_key or not issue_key:
            return
        self.store.append_event(project_key, issue_key, event_type or "unknown", payload)
        if event_type == "comment_control":
            self.handle_comment_control(project_key, issue_key, payload)
            return
        self.maybe_enqueue_issue(project_key, issue_key)

    def handle_comment_control(self, project_key: str, issue_key: str, payload: dict[str, Any]) -> None:
        command = parse_control_command(str(payload.get("command", "")))
        if not command:
            return
        control_issue_key = self.store.project_control_issue(project_key)
        if issue_key == control_issue_key:
            if command == "pause-project":
                self.store.set_control_flag("project", project_key, "pause", "paused")
            elif command == "resume-project":
                self.store.set_control_flag("project", project_key, "pause", "running")
            elif command == "drain-project":
                self.store.set_control_flag("project", project_key, "drain", "enabled")
            return
        if command == "pause":
            self.store.set_requested_action(issue_key, "pause")
        elif command == "resume":
            self.store.clear_requested_action(issue_key)
            job = self.store.get_job(issue_key)
            if job and job["state"] == "paused":
                self.store.update_job(issue_key, state="queued", requested_action="")
        elif command == "cancel":
            self.store.set_requested_action(issue_key, "cancel")
            signal_active_process(self.store.get_job(issue_key).get("active_pid"))
        elif command == "retry":
            job = self.store.get_job(issue_key)
            if job and job["state"] in {"failed", "blocked", "gate_failed", "gate_waiting_human", "waiting_dependency", "backlog"}:
                retry_issue_job(self.store, issue_key, reason="Jira /ai retry")
        elif command == "unblock":
            job = self.store.get_job(issue_key)
            if job and job["state"] in {"gate_failed", "gate_waiting_human", "waiting_dependency", "blocked"}:
                unblock_gate_issue(self.store, issue_key, reason="Jira /ai unblock")
                self.refresh_issue_report(issue_key, extra_notice="Gate unblocked from Jira comment.")
        elif command == "status":
            self.refresh_issue_report(issue_key)

    def reconcile_projects(self, project_key: str | None = None) -> int:
        if self.store.control_flag_value("global", "*", "pause") == "paused":
            return 0
        count = 0
        projects = {item.project_key: item for item in discover_projects(self.settings.projects_roots)}
        for current_key, project in projects.items():
            if project_key and current_key != project_key:
                continue
            count += self.poll_jira_control_comments(current_key)
            if self.store.control_flag_value("project", current_key, "pause") == "paused":
                continue
            for issue in jira_search_auto_issues(self.settings, current_key):
                count += self.poll_jira_control_comments(current_key, issue_keys=[str(issue["key"])])
                if self.maybe_enqueue_issue(current_key, issue["key"]):
                    count += 1
        return count

    def poll_jira_control_comments(self, project_key: str, issue_keys: list[str] | None = None) -> int:
        project = self.store.project(project_key)
        if not project:
            return 0
        candidates = {normalize_issue_key(key) for key in (issue_keys or [])}
        control_issue_key = self.store.project_control_issue(project_key)
        if control_issue_key:
            candidates.add(control_issue_key)
        for job in self.store.list_jobs(project_key=project_key):
            candidates.add(str(job["issue_key"]))

        processed = 0
        for issue_key in sorted(key for key in candidates if key):
            for comment in jira_issue_comments(self.settings, issue_key):
                comment_id = str(comment.get("id", ""))
                if not comment_id or self.store.has_processed_comment(comment_id):
                    continue
                body = extract_jira_text(comment.get("body"))
                command = parse_control_command(body)
                if command:
                    self.store.append_event(
                        project_key,
                        issue_key,
                        "comment_control",
                        {
                            "event_type": "comment_control",
                            "project_key": project_key,
                            "issue_key": issue_key,
                            "comment_id": comment_id,
                            "command": body,
                        },
                    )
                    self.handle_comment_control(project_key, issue_key, {"command": body})
                    processed += 1
                self.store.mark_processed_comment(
                    comment_id=comment_id,
                    project_key=project_key,
                    issue_key=issue_key,
                    command=command,
                )
        return processed

    def maybe_enqueue_issue(self, project_key: str, issue_key: str) -> bool:
        project = self.store.project(project_key)
        if not project:
            return False
        issue = jira_get_issue(self.settings, issue_key)
        if not issue_is_auto_ready(issue):
            return False
        existing_job = self.store.get_job(issue_key)
        self.store.enqueue_issue(
            project_key=project_key,
            repo_path=project["repo_path"],
            issue_key=issue_key,
            status=str(issue["fields"]["status"]["name"]),
            summary=str(issue["fields"].get("summary", "")),
        )
        if not existing_job:
            self.sync_jira_transition(issue_key)
        return not existing_job

    def dispatch_jobs(self) -> None:
        if self.store.control_flag_value("global", "*", "pause") == "paused":
            return
        self.collect_finished_threads()
        for job in self.store.list_runnable_jobs():
            issue_key = job["issue_key"]
            project_key = job["project_key"]
            repo_path = job["repo_path"]
            if self.store.control_flag_value("project", project_key, "pause") == "paused":
                continue
            if self.store.control_flag_value("project", project_key, "drain") == "enabled":
                continue
            batch_id = str(job.get("batch_id", ""))
            if batch_id:
                batch = self.store.batch(batch_id)
                if str(batch.get("state", "")) in {"paused", "cancelled"}:
                    continue
                dep_status = dependency_status(self.store, job)
                if not dep_status["satisfied"]:
                    blocked_dependencies = dep_status["blocked"] or dep_status["waiting"]
                    if str(job.get("state", "")) != "waiting_dependency" or parse_blocked_dependencies(job.get("blocked_dependencies_json")) != blocked_dependencies:
                        self.store.update_job(
                            issue_key,
                            state="waiting_dependency",
                            gate_state="waiting_dependency",
                            gate_reason="Waiting for dependency issue(s) before this work can start.",
                            blocked_dependencies_json=json.dumps(blocked_dependencies, ensure_ascii=False),
                        )
                        with contextlib.suppress(Exception):
                            self.refresh_issue_report(issue_key)
                    continue
                if str(job.get("state", "")) == "waiting_dependency":
                    self.store.update_job(
                        issue_key,
                        state="queued",
                        gate_state="",
                        gate_reason="",
                        blocked_dependencies_json="[]",
                    )
                    job = self.store.get_job(issue_key)
                if str(job.get("state", "")) != "queued":
                    continue
            if str(job.get("state", "")) != "queued":
                continue
            with self.thread_lock:
                if issue_key in self.active_threads:
                    continue
            if not self.store.acquire_lease(
                repo_path,
                issue_key,
                project_key=project_key,
                conflict_group=str(job.get("conflict_group") or issue_key),
                max_parallel_per_repo=int(getattr(self.settings, "max_parallel_per_repo", DEFAULT_MAX_PARALLEL_PER_REPO)),
                max_parallel_per_project=int(
                    getattr(self.settings, "max_parallel_per_project", DEFAULT_MAX_PARALLEL_PER_PROJECT)
                ),
            ):
                continue
            thread = threading.Thread(
                target=self.process_job,
                name=f"orchestrator-{issue_key}",
                daemon=True,
                args=(issue_key,),
            )
            with self.thread_lock:
                self.active_threads[issue_key] = thread
            thread.start()

    def collect_finished_threads(self) -> None:
        with self.thread_lock:
            finished = [issue_key for issue_key, thread in self.active_threads.items() if not thread.is_alive()]
            for issue_key in finished:
                self.active_threads.pop(issue_key, None)

    def process_job(self, issue_key: str) -> None:
        try:
            self._process_job(issue_key)
        except Exception as exc:  # pragma: no cover - defensive logging path
            outcome = retry_transient_or_fail_issue_job(
                self.store,
                self.settings,
                issue_key,
                reason=str(exc),
                backlog=True,
            )
            notice = "Retrying transient failure" if outcome == "queued" else "Failed"
            self.refresh_issue_report(issue_key, extra_notice=f"{notice}: {exc}")
        finally:
            job = self.store.get_job(issue_key)
            if job:
                self.store.release_lease(job["repo_path"], issue_key)
                self.store.update_job(issue_key, active_pid=None)

    def _process_job(self, issue_key: str) -> None:
        job = self.store.get_job(issue_key)
        if not job:
            return
        project_key = job["project_key"]
        repo_path = Path(job["repo_path"])
        issue = jira_get_issue(self.settings, issue_key)
        branch, worktree_path = ensure_issue_worktree(
            repo_path,
            project_key,
            issue_key,
            issue["fields"].get("summary", ""),
        )
        self.store.update_job(
            issue_key,
            branch=branch,
            worktree_path=str(worktree_path),
        )

        if job["state"] in {"queued", "planning", "paused"}:
            self.store.update_job(issue_key, state="planning")
            self.refresh_issue_report(issue_key)
            ensure_issue_spec(issue_key, worktree_path, str(issue["fields"].get("summary", issue_key)))
            plan = run_claude_planning(
                self.store,
                self.settings,
                worktree_path,
                issue_key,
                project_key,
                issue_summary=str(issue["fields"].get("summary", issue_key)),
                issue_description=extract_jira_text(issue["fields"].get("description")),
            )
            contract = normalize_task_contract(plan, issue_key=issue_key)
            plan["task_contract"] = contract
            self.store.record_contract(
                issue_key,
                batch_id=str(job.get("batch_id", "")),
                round_number=0,
                contract=contract,
                created_by="claude",
            )
            self.store.record_step(issue_key, "planning", "success", plan)
            if self._maybe_pause_or_cancel(issue_key):
                return

        job = self.store.get_job(issue_key)
        if not job:
            return
        if job["state"] in {"planning", "coding", "queued"}:
            self.store.update_job(issue_key, state="coding")
            self.refresh_issue_report(issue_key)
            plan_step = self.store.latest_step(issue_key, "planning")
            plan_payload = {}
            if plan_step:
                with contextlib.suppress(json.JSONDecodeError):
                    plan_payload = json.loads(plan_step["payload_json"])
            contract = normalize_task_contract(
                plan_payload if isinstance(plan_payload, dict) else {},
                issue_key=issue_key,
            )
            if getattr(self.settings, "contract_handshake", DEFAULT_CONTRACT_HANDSHAKE) == "required":
                baton = run_contract_handshake(
                    self.store,
                    self.settings,
                    Path(job["worktree_path"]),
                    issue_key,
                    project_key,
                    str(job.get("batch_id", "")),
                    contract,
                )
                self.store.record_step(issue_key, "contract_handshake", baton["status"], baton)
                if baton["status"] != "approved":
                    self.store.update_job(issue_key, state="blocked", latest_error=baton.get("summary", "Contract handshake blocked."))
                    self.refresh_issue_report(issue_key)
                    return
                contract = baton["task_contract"]
                if isinstance(plan_payload, dict):
                    plan_payload["task_contract"] = contract
            exec_result = run_codex_exec(
                self.store,
                self.settings,
                Path(job["worktree_path"]),
                project_key,
                issue_key,
                job["branch"],
                plan_payload if isinstance(plan_payload, dict) else {},
            )
            self.store.record_step(issue_key, "coding", exec_result["status"], exec_result)
            self.store.update_job(issue_key, latest_commit=git_head(Path(job["worktree_path"])))
            if exec_result["status"] not in {"success", "fallback"}:
                if exec_result["status"] == "blocked":
                    self.store.update_job(
                        issue_key,
                        state="blocked",
                        latest_error=exec_result.get("summary", "Codex exec blocked"),
                    )
                else:
                    retry_transient_or_fail_issue_job(
                        self.store,
                        self.settings,
                        issue_key,
                        reason=exec_result.get("summary", "Codex exec failed"),
                        backlog=True,
                    )
                self.refresh_issue_report(issue_key)
                return
            if exec_result.get("toolchain_recovery"):
                self.refresh_issue_report(issue_key)
            if not exec_result.get("changed_files"):
                retry_transient_or_fail_issue_job(
                    self.store,
                    self.settings,
                    issue_key,
                    reason="Codex finished without producing a diff.",
                    backlog=True,
                )
                self.refresh_issue_report(issue_key)
                return
            if self._maybe_pause_or_cancel(issue_key):
                return

        job = self.store.get_job(issue_key)
        if not job:
            return
        if job["state"] in {"coding", "reviewing"}:
            self.store.update_job(issue_key, state="reviewing")
            self.refresh_issue_report(issue_key)
            review_result = run_codex_review(
                self.store,
                self.settings,
                issue_key,
                Path(job["worktree_path"]),
            )
            self.store.record_step(issue_key, "reviewing", review_result["status"], review_result)
            if review_result["status"] == "blocked":
                self.store.update_job(
                    issue_key,
                    state="blocked",
                    latest_error=review_result.get("summary", "Codex review failed"),
                )
                self.refresh_issue_report(issue_key)
                return
            if review_result.get("toolchain_recovery"):
                self.refresh_issue_report(issue_key)
            integrate = run_claude_integrate(
                self.store,
                self.settings,
                Path(job["worktree_path"]),
                issue_key,
                project_key,
                review_result,
            )
            self.store.record_step(issue_key, "integrating", "success", integrate)
            if integrate["next_action"] == "retry_coding":
                self.store.update_job(issue_key, state="queued", latest_error="", requested_action="")
                self.refresh_issue_report(issue_key, extra_notice="Claude requested another coding pass.")
                return
            if integrate["next_action"] == "blocked":
                self.store.update_job(
                    issue_key,
                    state="blocked",
                    latest_error=integrate.get("blocker", "Blocked during Claude integration"),
                )
                self.refresh_issue_report(issue_key)
                return
            if self._maybe_pause_or_cancel(issue_key):
                return

        job = self.store.get_job(issue_key)
        if not job:
            return
        if job["state"] in {"reviewing", "pr_open"}:
            self.store.update_job(issue_key, state="pr_open")
            self.refresh_issue_report(issue_key)
            pr = ensure_pull_request(
                settings=self.settings,
                worktree_path=Path(job["worktree_path"]),
                issue_key=issue_key,
                branch=job["branch"],
                summary=str(issue["fields"].get("summary", issue_key)),
                project_key=project_key,
            )
            self.store.update_job(
                issue_key,
                state="waiting_checks",
                pr_url=pr["url"],
                pr_number=str(pr["number"]),
                latest_commit=git_head(Path(job["worktree_path"])),
            )
            self.refresh_issue_report(issue_key)

    def _maybe_pause_or_cancel(self, issue_key: str) -> bool:
        job = self.store.get_job(issue_key)
        if not job:
            return True
        action = job.get("requested_action", "")
        if action == "cancel":
            self.store.update_job(issue_key, state="cancelled", requested_action="", active_pid=None)
            self.refresh_issue_report(issue_key, extra_notice="Cancelled by operator.")
            return True
        if action == "pause":
            self.store.update_job(issue_key, state="paused", requested_action="")
            self.refresh_issue_report(issue_key, extra_notice="Paused after the current step.")
            return True
        return False

    def poll_github_jobs(self, *, issue_key: str | None = None, project_key: str | None = None) -> int:
        refreshed = 0
        for job in self.store.list_waiting_jobs(issue_key=issue_key, project_key=project_key):
            worktree_path = Path(job["worktree_path"]) if job.get("worktree_path") else None
            if not worktree_path or not worktree_path.exists():
                continue
            pr = github_pull_request_status(worktree_path, job["branch"], job.get("pr_number", ""))
            if not pr:
                continue
            project = self.store.project(job["project_key"])
            codex_review_mode = str(project.get("codex_review_mode", "auto_required")) if project else "auto_required"
            updates = {
                "pr_url": pr["url"],
                "pr_number": str(pr["number"]),
                "latest_commit": pr.get("headRefOid", job.get("latest_commit", "")),
            }
            state = job["state"]
            review_summary = summarize_reviews(
                pr.get("reviews", []),
                self.settings.codex_review_authors,
                pr.get("comments", []),
            )
            check_summary = summarize_checks(pr.get("statusCheckRollup", []))
            if pr.get("state") == "MERGED":
                state = "done"
                updates["latest_error"] = ""
                updates["gate_state"] = ""
                updates["gate_reason"] = ""
                updates["blocked_dependencies_json"] = "[]"
            elif check_summary["failed"]:
                gate = classify_gate_result(
                    check_summary=check_summary,
                    review_summary=review_summary,
                    pr=pr,
                )
                state = gate["state"]
                updates["latest_error"] = gate["reason"]
                updates["gate_state"] = gate["classification"]
                updates["gate_reason"] = gate["reason"]
                updates["blocked_dependencies_json"] = "[]"
            elif (
                state in {"waiting_checks", "gate_failed", "gate_waiting_human"}
                and check_summary["passed"]
                and str(job.get("gate_state", "")) != "review_changes_requested"
            ):
                state = "waiting_review"
                updates["review_requested_at"] = str(job.get("review_requested_at") or now_iso())
                updates["latest_error"] = ""
                updates["gate_state"] = ""
                updates["gate_reason"] = ""
                updates["blocked_dependencies_json"] = "[]"
                if codex_review_mode == "comment_fallback":
                    updates["review_fallback_requested_at"] = request_codex_review(
                        worktree_path,
                        pr["url"],
                        existing_timestamp=str(job.get("review_fallback_requested_at", "")),
                    )
            if state in {"waiting_review", "gate_waiting_human"}:
                job_snapshot = {**job, **updates}
                state, extra_updates = self.resolve_review_state(
                    job=job_snapshot,
                    worktree_path=worktree_path,
                    pr=pr,
                    review_summary=review_summary,
                    codex_review_mode=codex_review_mode,
                )
                updates.update(extra_updates)
                if state == "gate_waiting_human" and "Codex review did not arrive" in str(updates.get("latest_error", "")):
                    fresh_pr = github_pull_request_status(worktree_path, job["branch"], str(updates.get("pr_number", "")))
                    if fresh_pr:
                        fresh_review_summary = summarize_reviews(
                            fresh_pr.get("reviews", []),
                            self.settings.codex_review_authors,
                            fresh_pr.get("comments", []),
                        )
                        if fresh_review_summary["reviewed"]:
                            pr = fresh_pr
                            review_summary = fresh_review_summary
                            state = "ready_for_merge"
                            updates.pop("latest_error", None)
                            updates["latest_error"] = ""
                            updates["gate_state"] = ""
                            updates["gate_reason"] = ""
                            updates["blocked_dependencies_json"] = "[]"
                            updates["pr_url"] = pr["url"]
                            updates["pr_number"] = str(pr["number"])
                            updates["latest_commit"] = pr.get("headRefOid", updates.get("latest_commit", ""))
            if state == "ready_for_merge":
                updates["latest_error"] = ""
                updates["gate_state"] = ""
                updates["gate_reason"] = ""
                updates["blocked_dependencies_json"] = "[]"
            if state == "ready_for_merge" and getattr(self.settings, "github_merge_policy", DEFAULT_GITHUB_MERGE_POLICY) == "merge_queue":
                merge_result = ensure_github_auto_merge(worktree_path, pr.get("url", ""))
                if merge_result.get("warning"):
                    self.store.record_step(job["issue_key"], "merge_queue", "warning", merge_result)
                elif merge_result.get("enabled"):
                    self.store.record_step(job["issue_key"], "merge_queue", "success", merge_result)
            updates["state"] = state
            self.store.update_job(job["issue_key"], **updates)
            self.refresh_issue_report(
                job["issue_key"],
                checks_summary=check_summary["summary"],
                review_summary=review_summary["summary"],
            )
            refreshed += 1
        return refreshed

    def resolve_review_state(
        self,
        *,
        job: dict[str, Any],
        worktree_path: Path,
        pr: dict[str, Any],
        review_summary: dict[str, Any],
        codex_review_mode: str,
    ) -> tuple[str, dict[str, Any]]:
        updates: dict[str, Any] = {}
        if review_summary["changes_requested"]:
            gate = classify_gate_result(review_summary=review_summary)
            return (
                gate["state"],
                {
                    "latest_error": gate["reason"],
                    "gate_state": gate["classification"],
                    "gate_reason": gate["reason"],
                },
            )
        if review_summary["reviewed"]:
            return (
                "ready_for_merge",
                {
                    **updates,
                    "latest_error": "",
                    "gate_state": "",
                    "gate_reason": "",
                    "blocked_dependencies_json": "[]",
                },
            )

        now = now_iso()
        review_requested_at = str(job.get("review_requested_at") or now)
        updates.setdefault("review_requested_at", review_requested_at)
        fallback_requested_at = str(job.get("review_fallback_requested_at") or "")

        if codex_review_mode == "comment_fallback":
            fallback_requested_at = request_codex_review(
                worktree_path,
                pr["url"],
                existing_timestamp=fallback_requested_at,
            )
            if fallback_requested_at:
                updates["review_fallback_requested_at"] = fallback_requested_at
            if fallback_requested_at and seconds_since(fallback_requested_at) >= self.settings.fallback_review_grace_seconds:
                return (
                    "gate_waiting_human",
                    {
                        **updates,
                        "latest_error": "Codex review did not arrive after the fallback review request.",
                        "gate_state": "intentional_gate",
                        "gate_reason": "Codex review did not arrive after the fallback review request.",
                    },
                )
            return ("waiting_review", updates)

        if not fallback_requested_at and seconds_since(review_requested_at) >= self.settings.auto_review_grace_seconds:
            fallback_requested_at = request_codex_review(
                worktree_path,
                pr["url"],
                existing_timestamp="",
            )
            if fallback_requested_at:
                updates["review_fallback_requested_at"] = fallback_requested_at
        if fallback_requested_at and seconds_since(fallback_requested_at) >= self.settings.fallback_review_grace_seconds:
            return (
                "gate_waiting_human",
                {
                    **updates,
                    "latest_error": "Codex review did not arrive after the fallback review request.",
                    "gate_state": "intentional_gate",
                    "gate_reason": "Codex review did not arrive after the fallback review request.",
                },
            )
        return ("waiting_review", updates)

    def refresh_issue_report(
        self,
        issue_key: str,
        *,
        extra_notice: str = "",
        checks_summary: str = "",
        review_summary: str = "",
    ) -> None:
        job = self.store.get_job(issue_key)
        if not job:
            return
        transition_notice = self.sync_jira_transition(issue_key, job)
        if transition_notice:
            extra_notice = "\n".join(item for item in (extra_notice, transition_notice) if item)
            job = self.store.get_job(issue_key) or job
        recovery_notice = latest_step_summary(self.store, issue_key, "toolchain_recovery")
        if recovery_notice:
            extra_notice = "\n".join(item for item in (extra_notice, recovery_notice) if item)
        comment_body = build_summary_comment(
            job=job,
            checks_summary=checks_summary or latest_step_summary(self.store, issue_key, "waiting_checks"),
            review_summary=review_summary or latest_step_summary(self.store, issue_key, "reviewing"),
            fallback_summary=latest_fallback_summary(self.store, issue_key),
            extra_notice=extra_notice,
        )
        comment_id = upsert_summary_comment(self.settings, issue_key, comment_body, self.store.report_comment_id(issue_key))
        self.store.upsert_report(issue_key, comment_id, comment_body)

    def sync_jira_transition(self, issue_key: str, job: dict[str, Any] | None = None) -> str:
        current_job = job or self.store.get_job(issue_key)
        if not current_job:
            return ""
        policy = transition_policy_from_project(self.store.project(str(current_job.get("project_key", ""))))
        result = transition_jira_issue_for_job(self.settings, current_job, policy)
        if result.get("status_name"):
            self.store.update_job(issue_key, status_name=str(result["status_name"]))
        if result.get("warning"):
            self.store.record_step(issue_key, "jira_transition", "warning", result)
            return str(result["warning"])
        if result.get("transitioned"):
            self.store.record_step(issue_key, "jira_transition", "success", result)
        return ""


class OrchestratorStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def _connection(self) -> Any:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_key TEXT PRIMARY KEY,
                    repo_path TEXT NOT NULL,
                    repo_name TEXT NOT NULL,
                    confluence_space TEXT NOT NULL,
                    codex_review_mode TEXT NOT NULL DEFAULT 'auto_required',
                    transition_policy_json TEXT DEFAULT '{}',
                    manifest_path TEXT NOT NULL,
                    source_repo TEXT NOT NULL,
                    workflow_ref TEXT NOT NULL,
                    jira_project_id TEXT DEFAULT '',
                    control_issue_key TEXT DEFAULT '',
                    lifecycle_rule_uuid TEXT DEFAULT '',
                    comment_rule_uuid TEXT DEFAULT '',
                    webhook_secret TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS issue_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_key TEXT NOT NULL,
                    issue_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    issue_key TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    state TEXT NOT NULL,
                    batch_id TEXT DEFAULT '',
                    conflict_group TEXT DEFAULT '',
                    dependencies_json TEXT DEFAULT '[]',
                    attempt_count INTEGER DEFAULT 0,
                    contract_rounds INTEGER DEFAULT 0,
                    status_name TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    branch TEXT DEFAULT '',
                    worktree_path TEXT DEFAULT '',
                    pr_url TEXT DEFAULT '',
                    pr_number TEXT DEFAULT '',
                    latest_commit TEXT DEFAULT '',
                    latest_error TEXT DEFAULT '',
                    gate_state TEXT DEFAULT '',
                    gate_reason TEXT DEFAULT '',
                    blocked_dependencies_json TEXT DEFAULT '[]',
                    active_pid INTEGER,
                    requested_action TEXT DEFAULT '',
                    review_requested_at TEXT DEFAULT '',
                    review_fallback_requested_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases (
                    repo_path TEXT PRIMARY KEY,
                    issue_key TEXT NOT NULL,
                    acquired_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduler_leases (
                    repo_path TEXT NOT NULL,
                    issue_key TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL,
                    conflict_group TEXT NOT NULL,
                    acquired_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduler_leases_conflict
                    ON scheduler_leases(repo_path, conflict_group);
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL,
                    jql TEXT NOT NULL,
                    state TEXT NOT NULL,
                    max_parallel INTEGER NOT NULL,
                    summary TEXT DEFAULT '',
                    design_memo TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_units (
                    issue_key TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    conflict_group TEXT NOT NULL,
                    dependencies_json TEXT DEFAULT '[]',
                    contract_json TEXT DEFAULT '{}',
                    state TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contracts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_key TEXT NOT NULL,
                    batch_id TEXT DEFAULT '',
                    round INTEGER NOT NULL,
                    contract_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_key TEXT NOT NULL,
                    batch_id TEXT DEFAULT '',
                    sender TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_key TEXT NOT NULL,
                    batch_id TEXT DEFAULT '',
                    round INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_key TEXT NOT NULL,
                    batch_id TEXT DEFAULT '',
                    attempt INTEGER NOT NULL,
                    step_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    classification TEXT DEFAULT '',
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_key TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    exit_code INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reports (
                    issue_key TEXT PRIMARY KEY,
                    comment_id TEXT DEFAULT '',
                    body TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control_flags (
                    scope_type TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    flag TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (scope_type, scope_key, flag)
                );
                CREATE TABLE IF NOT EXISTS processed_comments (
                    comment_id TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL,
                    issue_key TEXT NOT NULL,
                    command TEXT DEFAULT '',
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(
                connection,
                "projects",
                "codex_review_mode",
                "TEXT NOT NULL DEFAULT 'auto_required'",
            )
            self._ensure_column(
                connection,
                "projects",
                "transition_policy_json",
                "TEXT DEFAULT '{}'",
            )
            self._ensure_column(
                connection,
                "projects",
                "webhook_secret",
                "TEXT DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "jobs",
                "review_requested_at",
                "TEXT DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "jobs",
                "review_fallback_requested_at",
                "TEXT DEFAULT ''",
            )
            for name, definition in (
                ("batch_id", "TEXT DEFAULT ''"),
                ("conflict_group", "TEXT DEFAULT ''"),
                ("dependencies_json", "TEXT DEFAULT '[]'"),
                ("attempt_count", "INTEGER DEFAULT 0"),
                ("contract_rounds", "INTEGER DEFAULT 0"),
                ("gate_state", "TEXT DEFAULT ''"),
                ("gate_reason", "TEXT DEFAULT ''"),
                ("blocked_dependencies_json", "TEXT DEFAULT '[]'"),
            ):
                self._ensure_column(connection, "jobs", name, definition)

    def _ensure_column(self, connection: sqlite3.Connection, table: str, name: str, definition: str) -> None:
        existing = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def sync_projects(self, projects: list[RepoProject]) -> None:
        with self._connection() as connection:
            for project in projects:
                connection.execute(
                    """
                    INSERT INTO projects (
                        project_key, repo_path, repo_name, confluence_space, codex_review_mode,
                        transition_policy_json, manifest_path, source_repo, workflow_ref, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_key) DO UPDATE SET
                        repo_path=excluded.repo_path,
                        repo_name=excluded.repo_name,
                        confluence_space=excluded.confluence_space,
                        codex_review_mode=excluded.codex_review_mode,
                        transition_policy_json=excluded.transition_policy_json,
                        manifest_path=excluded.manifest_path,
                        source_repo=excluded.source_repo,
                        workflow_ref=excluded.workflow_ref,
                        updated_at=excluded.updated_at
                    """,
                    (
                        project.project_key,
                        str(project.repo_path),
                        project.repo_name,
                        project.confluence_space,
                        project.codex_review_mode,
                        json.dumps(project.transition_policy, ensure_ascii=False),
                        str(project.manifest_path),
                        project.source_repo,
                        project.workflow_ref,
                        now_iso(),
                    ),
                )

    def update_project_registration(
        self,
        *,
        project_key: str,
        jira_project_id: str,
        control_issue_key: str,
        lifecycle_rule_uuid: str,
        comment_rule_uuid: str,
        webhook_secret: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE projects
                SET jira_project_id=?, control_issue_key=?, lifecycle_rule_uuid=?,
                    comment_rule_uuid=?, webhook_secret=?, updated_at=?
                WHERE project_key=?
                """,
                (
                    jira_project_id,
                    control_issue_key,
                    lifecycle_rule_uuid,
                    comment_rule_uuid,
                    webhook_secret,
                    now_iso(),
                    project_key,
                ),
            )

    def project(self, project_key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_key = ?",
                (project_key,),
            ).fetchone()
            return dict(row) if row else None

    def project_control_issue(self, project_key: str) -> str:
        project = self.project(project_key)
        return str(project.get("control_issue_key", "")) if project else ""

    def ensure_project_webhook_secret(self, project_key: str, provided_secret: str | None = None) -> str:
        project = self.project(project_key)
        existing = str(project.get("webhook_secret", "")) if project else ""
        secret = provided_secret or existing or secrets.token_hex(24)
        if existing == secret:
            return secret
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE projects
                SET webhook_secret=?, updated_at=?
                WHERE project_key=?
                """,
                (secret, now_iso(), project_key),
            )
        return secret

    def append_event(self, project_key: str, issue_key: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO issue_events (project_key, issue_key, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (project_key, issue_key, event_type, json.dumps(payload, ensure_ascii=False), now_iso()),
            )

    def record_worker_event(self, event_type: str, payload: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO worker_events (event_type, payload_json, created_at)
                VALUES (?, ?, ?)
                """,
                (event_type, json.dumps(payload, ensure_ascii=False), now_iso()),
            )

    def latest_worker_event(self, event_type: str = "") -> dict[str, Any]:
        query = "SELECT * FROM worker_events"
        params: tuple[Any, ...] = ()
        if event_type:
            query += " WHERE event_type = ?"
            params = (event_type,)
        query += " ORDER BY created_at DESC, id DESC LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(query, params).fetchone()
        if not row:
            return {}
        payload: dict[str, Any]
        try:
            decoded = json.loads(str(row["payload_json"]))
            payload = decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            payload = {}
        return {
            "event_type": str(row["event_type"]),
            "payload": payload,
            "created_at": str(row["created_at"]),
        }

    def create_batch(
        self,
        *,
        batch_id: str,
        project_key: str,
        jql: str,
        max_parallel: int,
        summary: str,
        design_memo: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO batches (
                    batch_id, project_key, jql, state, max_parallel, summary, design_memo, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    state=excluded.state,
                    max_parallel=excluded.max_parallel,
                    summary=excluded.summary,
                    design_memo=excluded.design_memo,
                    updated_at=excluded.updated_at
                """,
                (batch_id, project_key, jql, max_parallel, summary, design_memo, now_iso(), now_iso()),
            )

    def update_batch_state(self, batch_id: str, state: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE batches SET state = ?, updated_at = ? WHERE batch_id = ?",
                (state, now_iso(), batch_id),
            )

    def batch(self, batch_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            return dict(row) if row else {}

    def list_batches(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        query = "SELECT * FROM batches"
        if batch_id:
            query += " WHERE batch_id = ?"
            params.append(batch_id)
        query += " ORDER BY updated_at DESC"
        with self._connection() as connection:
            batches = [dict(row) for row in connection.execute(query, params).fetchall()]
        for batch in batches:
            batch["jobs"] = self.list_jobs_for_batch(str(batch["batch_id"]))
            batch["effective_state"] = batch_effective_state(batch, batch["jobs"])
        return batches

    def upsert_work_unit(
        self,
        *,
        issue_key: str,
        batch_id: str,
        project_key: str,
        repo_path: str,
        conflict_group: str,
        dependencies: list[str],
        contract: dict[str, Any],
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO work_units (
                    issue_key, batch_id, project_key, repo_path, conflict_group,
                    dependencies_json, contract_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                ON CONFLICT(issue_key) DO UPDATE SET
                    batch_id=excluded.batch_id,
                    project_key=excluded.project_key,
                    repo_path=excluded.repo_path,
                    conflict_group=excluded.conflict_group,
                    dependencies_json=excluded.dependencies_json,
                    contract_json=excluded.contract_json,
                    updated_at=excluded.updated_at
                """,
                (
                    issue_key,
                    batch_id,
                    project_key,
                    repo_path,
                    conflict_group,
                    json.dumps(dependencies, ensure_ascii=False),
                    json.dumps(contract, ensure_ascii=False),
                    now_iso(),
                    now_iso(),
                ),
            )
            connection.execute(
                """
                UPDATE jobs
                SET batch_id=?, conflict_group=?, dependencies_json=?, updated_at=?
                WHERE issue_key=?
                """,
                (batch_id, conflict_group, json.dumps(dependencies, ensure_ascii=False), now_iso(), issue_key),
            )

    def list_jobs_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at ASC",
                (batch_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def record_contract(
        self,
        issue_key: str,
        *,
        batch_id: str,
        round_number: int,
        contract: dict[str, Any],
        created_by: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO contracts (issue_key, batch_id, round, contract_json, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (issue_key, batch_id, round_number, json.dumps(contract, ensure_ascii=False), created_by, now_iso()),
            )

    def record_message(
        self,
        issue_key: str,
        *,
        batch_id: str,
        sender: str,
        message_type: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO messages (issue_key, batch_id, sender, message_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (issue_key, batch_id, sender, message_type, json.dumps(payload, ensure_ascii=False), now_iso()),
            )

    def record_decision(
        self,
        issue_key: str,
        *,
        batch_id: str,
        round_number: int,
        decision: dict[str, Any],
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO decisions (issue_key, batch_id, round, decision, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    issue_key,
                    batch_id,
                    round_number,
                    str(decision.get("decision", "")),
                    json.dumps(decision, ensure_ascii=False),
                    now_iso(),
                ),
            )

    def record_attempt(
        self,
        issue_key: str,
        *,
        batch_id: str,
        attempt: int,
        step_name: str,
        status: str,
        classification: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO attempts (
                    issue_key, batch_id, attempt, step_name, status, classification, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issue_key,
                    batch_id,
                    attempt,
                    step_name,
                    status,
                    classification,
                    json.dumps(payload, ensure_ascii=False),
                    now_iso(),
                ),
            )

    def enqueue_issue(
        self,
        *,
        project_key: str,
        repo_path: str,
        issue_key: str,
        status: str,
        summary: str,
    ) -> None:
        existing = self.get_job(issue_key)
        if existing:
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE jobs
                    SET project_key=?, repo_path=?, status_name=?, summary=?, updated_at=?
                    WHERE issue_key=?
                    """,
                    (project_key, repo_path, status, summary, now_iso(), issue_key),
                )
            return
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    issue_key, project_key, repo_path, state, status_name, summary, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (issue_key, project_key, repo_path, status, summary, now_iso(), now_iso()),
            )

    def list_jobs(
        self, *, issue_key: str | None = None, project_key: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if issue_key:
            clauses.append("issue_key = ?")
            params.append(issue_key)
        if project_key:
            clauses.append("project_key = ?")
            params.append(project_key)
        query = "SELECT * FROM jobs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC"
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def get_job(self, issue_key: str | None) -> dict[str, Any]:
        if not issue_key:
            return {}
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE issue_key = ?",
                (issue_key,),
            ).fetchone()
            return dict(row) if row else {}

    def list_runnable_jobs(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state IN ('queued', 'waiting_dependency')
                ORDER BY created_at ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def list_waiting_jobs(
        self, *, issue_key: str | None = None, project_key: str | None = None
    ) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in GITHUB_POLL_STATES)
        clauses = [f"state IN ({placeholders})"]
        params: list[Any] = sorted(GITHUB_POLL_STATES)
        if issue_key:
            clauses.append("issue_key = ?")
            params.append(issue_key)
        if project_key:
            clauses.append("project_key = ?")
            params.append(project_key)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at ASC
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def acquire_lease(
        self,
        repo_path: str,
        issue_key: str,
        *,
        project_key: str = "",
        conflict_group: str = "",
        max_parallel_per_repo: int = 1,
        max_parallel_per_project: int = 1,
    ) -> bool:
        if max_parallel_per_repo <= 1 and max_parallel_per_project <= 1 and not conflict_group:
            return self._acquire_legacy_lease(repo_path, issue_key)
        group = conflict_group or issue_key
        project = project_key or str((self.get_job(issue_key) or {}).get("project_key", ""))
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT issue_key FROM scheduler_leases WHERE issue_key = ?",
                (issue_key,),
            ).fetchone()
            if existing:
                return True
            repo_count = connection.execute(
                "SELECT COUNT(*) AS count FROM scheduler_leases WHERE repo_path = ?",
                (repo_path,),
            ).fetchone()["count"]
            if int(repo_count) >= max_parallel_per_repo:
                return False
            project_count = connection.execute(
                "SELECT COUNT(*) AS count FROM scheduler_leases WHERE project_key = ?",
                (project,),
            ).fetchone()["count"]
            if int(project_count) >= max_parallel_per_project:
                return False
            conflict = connection.execute(
                """
                SELECT issue_key FROM scheduler_leases
                WHERE repo_path = ? AND conflict_group = ? AND issue_key != ?
                """,
                (repo_path, group, issue_key),
            ).fetchone()
            if conflict:
                return False
            connection.execute(
                """
                INSERT INTO scheduler_leases (repo_path, issue_key, project_key, conflict_group, acquired_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (repo_path, issue_key, project, group, now_iso()),
            )
        return True

    def _acquire_legacy_lease(self, repo_path: str, issue_key: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT issue_key FROM leases WHERE repo_path = ?",
                (repo_path,),
            ).fetchone()
            if row and row["issue_key"] != issue_key:
                return False
            connection.execute(
                """
                INSERT INTO leases (repo_path, issue_key, acquired_at)
                VALUES (?, ?, ?)
                ON CONFLICT(repo_path) DO UPDATE SET
                    issue_key=excluded.issue_key,
                    acquired_at=excluded.acquired_at
                """,
                (repo_path, issue_key, now_iso()),
            )
        return True

    def release_lease(self, repo_path: str | None, issue_key: str) -> None:
        if not repo_path:
            return
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM leases WHERE repo_path = ? AND issue_key = ?",
                (repo_path, issue_key),
            )
            connection.execute(
                "DELETE FROM scheduler_leases WHERE repo_path = ? AND issue_key = ?",
                (repo_path, issue_key),
            )

    def clear_all_leases(self) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM leases")
            connection.execute("DELETE FROM scheduler_leases")

    def recover_inflight_jobs(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state='queued',
                    active_pid=NULL,
                    requested_action='',
                    updated_at=?
                WHERE state IN ('planning', 'coding', 'reviewing', 'pr_open')
                """,
                (now_iso(),),
            )

    def update_job(self, issue_key: str, **fields: Any) -> None:
        if not fields:
            return
        safe_fields = dict(fields)
        safe_fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in safe_fields)
        params = list(safe_fields.values()) + [issue_key]
        with self._connection() as connection:
            connection.execute(f"UPDATE jobs SET {assignments} WHERE issue_key = ?", params)

    def record_step(
        self, issue_key: str, step_name: str, status: str, payload: dict[str, Any], exit_code: int | None = None
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO steps (issue_key, step_name, status, payload_json, exit_code, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    issue_key,
                    step_name,
                    status,
                    json.dumps(payload, ensure_ascii=False),
                    exit_code,
                    now_iso(),
                ),
            )

    def latest_step(self, issue_key: str, step_name: str | None = None) -> dict[str, Any]:
        query = "SELECT * FROM steps WHERE issue_key = ?"
        params: list[Any] = [issue_key]
        if step_name:
            query += " AND step_name = ?"
            params.append(step_name)
        query += " ORDER BY id DESC LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(query, params).fetchone()
            return dict(row) if row else {}

    def upsert_report(self, issue_key: str, comment_id: str, body: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO reports (issue_key, comment_id, body, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(issue_key) DO UPDATE SET
                    comment_id=excluded.comment_id,
                    body=excluded.body,
                    updated_at=excluded.updated_at
                """,
                (issue_key, comment_id, body, now_iso()),
            )

    def report_comment_id(self, issue_key: str) -> str:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT comment_id FROM reports WHERE issue_key = ?",
                (issue_key,),
            ).fetchone()
            return str(row["comment_id"]) if row else ""

    def set_control_flag(self, scope_type: str, scope_key: str, flag: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO control_flags (scope_type, scope_key, flag, value, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope_type, scope_key, flag) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (scope_type, scope_key, flag, value, now_iso()),
            )

    def control_flag_value(self, scope_type: str, scope_key: str, flag: str) -> str:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT value FROM control_flags
                WHERE scope_type = ? AND scope_key = ? AND flag = ?
                """,
                (scope_type, scope_key, flag),
            ).fetchone()
            return str(row["value"]) if row else ""

    def list_control_flags(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM control_flags ORDER BY scope_type, scope_key, flag"
            ).fetchall()
            return [dict(row) for row in rows]

    def set_requested_action(self, issue_key: str, action: str) -> None:
        self.update_job(issue_key, requested_action=action)

    def clear_requested_action(self, issue_key: str) -> None:
        self.update_job(issue_key, requested_action="")

    def has_processed_comment(self, comment_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT comment_id FROM processed_comments WHERE comment_id = ?",
                (comment_id,),
            ).fetchone()
            return bool(row)

    def mark_processed_comment(
        self,
        *,
        comment_id: str,
        project_key: str,
        issue_key: str,
        command: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO processed_comments (comment_id, project_key, issue_key, command, processed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(comment_id) DO NOTHING
                """,
                (comment_id, project_key, issue_key, command, now_iso()),
            )


def load_worker_settings(
    *,
    config_override: str | None = None,
    bind_host: str | None = None,
    bind_port: int | None = None,
    event_mode: str | None = None,
    public_base_url: str | None = None,
    listen_url: str | None = None,
    project_roots: list[str] | None = None,
    jira_site_url: str | None = None,
    jira_admin_email: str | None = None,
) -> WorkerSettings:
    config_path = (
        Path(config_override).expanduser().resolve()
        if config_override
        else default_config_path()
    )
    config = load_orchestrator_config(config_path)
    if bind_host:
        config["bind_host"] = bind_host
    if bind_port is not None:
        config["bind_port"] = int(bind_port)
    if event_mode is not None:
        config["event_mode"] = event_mode
    if public_base_url is not None:
        config["public_base_url"] = normalize_public_base_url(public_base_url)
    if listen_url:
        apply_legacy_listen_url(config, listen_url)
    if project_roots:
        merged_roots = config.get("projects_roots", [])
        for item in project_roots:
            normalized = str(Path(item).expanduser().resolve())
            if normalized not in merged_roots:
                merged_roots.append(normalized)
        config["projects_roots"] = merged_roots
    if jira_site_url:
        config["jira_site_url"] = jira_site_url.strip().rstrip("/")
    if jira_admin_email:
        config["jira_admin_email"] = jira_admin_email
    save_orchestrator_config(config, config_path)

    codex_binary = str(config.get("ai", {}).get("codex_binary", DEFAULT_CODEX_BINARY)).strip() or DEFAULT_CODEX_BINARY
    codex_toolchain = resolve_codex_toolchain(configured_binary=codex_binary, write=True, require=False)

    state_dir = default_state_dir()
    configured_codex_review_authors = tuple(
        str(item).strip().lower()
        for item in config.get("github", {}).get("codex_review_authors", DEFAULT_CODEX_REVIEW_AUTHORS)
        if str(item).strip()
    )
    codex_review_authors = tuple(
        dict.fromkeys([*configured_codex_review_authors, *DEFAULT_CODEX_REVIEW_AUTHORS])
    )
    scheduler_config = config.get("scheduler", {})
    timeout_config = config.get("timeouts", {})
    failure_config = config.get("failure", {})
    backlog_statuses = tuple(
        str(item).strip()
        for item in failure_config.get("backlog_statuses", DEFAULT_FAILURE_BACKLOG_STATUSES)
        if str(item).strip()
    )
    return WorkerSettings(
        config_path=config_path,
        db_path=state_dir / DB_FILENAME,
        bind_host=str(config["bind_host"]),
        bind_port=int(config["bind_port"]),
        event_mode=str(config.get("event_mode", DEFAULT_EVENT_MODE)),
        public_base_url=str(config.get("public_base_url", "")).rstrip("/"),
        projects_roots=tuple(Path(item).expanduser().resolve() for item in config["projects_roots"]),
        poll_intervals={key: int(value) for key, value in config["poll_intervals"].items()},
        github_mode=str(config["github_mode"]),
        codex_review_authors=codex_review_authors or DEFAULT_CODEX_REVIEW_AUTHORS,
        auto_review_grace_seconds=int(
            config.get("github", {}).get("auto_review_grace_seconds", DEFAULT_AUTO_REVIEW_GRACE_SECONDS)
        ),
        fallback_review_grace_seconds=int(
            config.get("github", {}).get(
                "fallback_review_grace_seconds",
                DEFAULT_FALLBACK_REVIEW_GRACE_SECONDS,
            )
        ),
        max_parallel_per_repo=max(1, int(scheduler_config.get("max_parallel_per_repo", DEFAULT_MAX_PARALLEL_PER_REPO))),
        max_parallel_per_project=max(
            1,
            int(scheduler_config.get("max_parallel_per_project", DEFAULT_MAX_PARALLEL_PER_PROJECT)),
        ),
        contract_handshake=str(scheduler_config.get("contract_handshake", DEFAULT_CONTRACT_HANDSHAKE)),
        max_baton_rounds=max(0, int(scheduler_config.get("max_baton_rounds", DEFAULT_MAX_BATON_ROUNDS))),
        failure_max_attempts=max(1, int(failure_config.get("max_attempts", DEFAULT_FAILURE_MAX_ATTEMPTS))),
        failure_backlog_statuses=backlog_statuses or DEFAULT_FAILURE_BACKLOG_STATUSES,
        github_merge_policy=str(config.get("github", {}).get("merge_policy", DEFAULT_GITHUB_MERGE_POLICY)),
        claude_timeout_seconds=max(1, int(timeout_config.get("claude_seconds", DEFAULT_CLAUDE_TIMEOUT_SECONDS))),
        codex_exec_timeout_seconds=max(1, int(timeout_config.get("codex_exec_seconds", DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS))),
        codex_review_timeout_seconds=max(
            1,
            int(timeout_config.get("codex_review_seconds", DEFAULT_LOCAL_REVIEW_TIMEOUT_SECONDS)),
        ),
        codex_model=str(config.get("ai", {}).get("codex_model", DEFAULT_CODEX_MODEL)).strip(),
        codex_binary=codex_binary,
        codex_resolved_binary=str(codex_toolchain.get("binary", "")),
        codex_resolved_version=str(codex_toolchain.get("version", "")),
        codex_toolchain_compatible=bool(codex_toolchain.get("compatible")),
        codex_ignore_user_config=bool(
            config.get("ai", {}).get("codex_ignore_user_config", DEFAULT_CODEX_IGNORE_USER_CONFIG)
        ),
        claude_model=str(config.get("ai", {}).get("claude_model", DEFAULT_CLAUDE_MODEL)).strip(),
        claude_effort=str(config.get("ai", {}).get("claude_effort", DEFAULT_CLAUDE_EFFORT)).strip(),
        jira_site_url=str(config["jira_site_url"]).rstrip("/"),
        jira_admin_email=str(config["jira_admin_email"]),
    )


def load_orchestrator_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or default_config_path()
    config = default_orchestrator_config()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            loaded = {}
        if isinstance(loaded, dict):
            config.update(
                {
                    key: value
                    for key, value in loaded.items()
                    if key
                    not in {
                        "poll_intervals",
                        "github",
                        "ai",
                        "scheduler",
                        "timeouts",
                        "failure",
                        "listen_url",
                        "shared_secret",
                    }
                }
            )
            if isinstance(loaded.get("poll_intervals"), dict):
                config["poll_intervals"] = {
                    **config["poll_intervals"],
                    **loaded["poll_intervals"],
                }
            if isinstance(loaded.get("github"), dict):
                config["github"] = {
                    **config["github"],
                    **loaded["github"],
                }
            if isinstance(loaded.get("ai"), dict):
                config["ai"] = {
                    **config["ai"],
                    **loaded["ai"],
                }
            if isinstance(loaded.get("scheduler"), dict):
                config["scheduler"] = {
                    **config["scheduler"],
                    **loaded["scheduler"],
                }
            if isinstance(loaded.get("timeouts"), dict):
                config["timeouts"] = {
                    **config["timeouts"],
                    **loaded["timeouts"],
                }
            if isinstance(loaded.get("failure"), dict):
                config["failure"] = {
                    **config["failure"],
                    **loaded["failure"],
                }
            if loaded.get("listen_url"):
                apply_legacy_listen_url(config, str(loaded["listen_url"]))
    config.pop("listen_url", None)
    config.pop("shared_secret", None)
    config["bind_host"] = str(config.get("bind_host") or DEFAULT_BIND_HOST)
    config["bind_port"] = int(config.get("bind_port") or DEFAULT_BIND_PORT)
    config["event_mode"] = str(config.get("event_mode") or DEFAULT_EVENT_MODE)
    if config["event_mode"] not in {"polling", "webhook"}:
        config["event_mode"] = DEFAULT_EVENT_MODE
    config["public_base_url"] = normalize_public_base_url(str(config.get("public_base_url", "")))
    config["projects_roots"] = [
        str(Path(item).expanduser().resolve())
        for item in config.get("projects_roots", [])
    ]
    if not config["projects_roots"]:
        config["projects_roots"] = [default_projects_root()]
    return config


def save_orchestrator_config(config: dict[str, Any], config_path: Path | None = None) -> None:
    path = config_path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def default_orchestrator_config() -> dict[str, Any]:
    platform_config = load_platform_user_config()
    jira_config = platform_config.get("jira", {})
    return {
        "version": 2,
        "bind_host": DEFAULT_BIND_HOST,
        "bind_port": DEFAULT_BIND_PORT,
        "event_mode": DEFAULT_EVENT_MODE,
        "public_base_url": "",
        "projects_roots": [default_projects_root()],
        "poll_intervals": dict(DEFAULT_POLL_INTERVALS),
        "github_mode": DEFAULT_GITHUB_MODE,
        "github": {
            "codex_review_authors": list(DEFAULT_CODEX_REVIEW_AUTHORS),
            "auto_review_grace_seconds": DEFAULT_AUTO_REVIEW_GRACE_SECONDS,
            "fallback_review_grace_seconds": DEFAULT_FALLBACK_REVIEW_GRACE_SECONDS,
            "merge_policy": DEFAULT_GITHUB_MERGE_POLICY,
        },
        "scheduler": {
            "max_parallel_per_repo": DEFAULT_MAX_PARALLEL_PER_REPO,
            "max_parallel_per_project": DEFAULT_MAX_PARALLEL_PER_PROJECT,
            "contract_handshake": DEFAULT_CONTRACT_HANDSHAKE,
            "max_baton_rounds": DEFAULT_MAX_BATON_ROUNDS,
        },
        "timeouts": {
            "claude_seconds": DEFAULT_CLAUDE_TIMEOUT_SECONDS,
            "codex_exec_seconds": DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS,
            "codex_review_seconds": DEFAULT_LOCAL_REVIEW_TIMEOUT_SECONDS,
        },
        "failure": {
            "max_attempts": DEFAULT_FAILURE_MAX_ATTEMPTS,
            "backlog_statuses": list(DEFAULT_FAILURE_BACKLOG_STATUSES),
        },
        "ai": {
            "codex_model": DEFAULT_CODEX_MODEL,
            "codex_binary": DEFAULT_CODEX_BINARY,
            "codex_ignore_user_config": DEFAULT_CODEX_IGNORE_USER_CONFIG,
            "claude_model": DEFAULT_CLAUDE_MODEL,
            "claude_effort": DEFAULT_CLAUDE_EFFORT,
        },
        "jira_site_url": jira_config.get("site_url", ""),
        "jira_admin_email": jira_config.get("admin_email", ""),
    }


def load_platform_user_config() -> dict[str, Any]:
    path = default_config_path().with_name(PLATFORM_CONFIG_FILENAME)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def default_config_dir() -> Path:
    base_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
    return base_dir / CONFIG_DIRNAME


def default_config_path() -> Path:
    return default_config_dir() / ORCHESTRATOR_CONFIG_FILENAME


def default_toolchain_path() -> Path:
    return default_config_dir() / TOOLCHAIN_CONFIG_FILENAME


def load_toolchain_config(path: Path | None = None) -> dict[str, Any]:
    toolchain_path = path or default_toolchain_path()
    if not toolchain_path.exists():
        return {}
    try:
        payload = json.loads(toolchain_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_toolchain_config(payload: dict[str, Any], path: Path | None = None) -> None:
    toolchain_path = path or default_toolchain_path()
    toolchain_path.parent.mkdir(parents=True, exist_ok=True)
    toolchain_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def pin_codex_toolchain(binary: str) -> dict[str, Any]:
    result = inspect_codex_binary(str(Path(binary).expanduser()))
    if not result.get("compatible"):
        raise OrchestratorError(f"Codex binary is not compatible: {binary} ({result.get('reason')})")
    result["pinned"] = True
    result["source"] = "pin"
    payload = {"version": 1, "codex": result}
    save_toolchain_config(payload)
    return result


def resolve_codex_toolchain(
    *,
    configured_binary: str = DEFAULT_CODEX_BINARY,
    write: bool = False,
    require: bool = True,
    exclude_binaries: tuple[str, ...] = (),
) -> dict[str, Any]:
    excluded = {str(Path(item).expanduser().resolve()) for item in exclude_binaries if item}
    last_result: dict[str, Any] = {}
    for candidate, source in codex_candidate_paths(configured_binary):
        result = inspect_codex_binary(candidate)
        result["source"] = source
        if result.get("binary"):
            binary_key = str(Path(str(result["binary"])).expanduser().resolve())
            if binary_key in excluded:
                result["compatible"] = False
                result["reason"] = "excluded after failed attempt"
        last_result = result
        if result.get("compatible"):
            result["resolved_at"] = now_iso()
            if write:
                save_toolchain_config({"version": 1, "codex": result})
            return result
    if require:
        reason = last_result.get("reason") or "No compatible Codex CLI found."
        raise OrchestratorError(f"No compatible Codex CLI found: {reason}")
    return {
        "binary": "",
        "version": "",
        "capabilities": {},
        "compatible": False,
        "reason": last_result.get("reason") or "No compatible Codex CLI found.",
        "resolved_at": now_iso(),
    }


def codex_candidate_paths(configured_binary: str = DEFAULT_CODEX_BINARY) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    env_binary = os.environ.get("CODEX_BIN", "").strip()
    if env_binary:
        candidates.append((env_binary, "env"))
    if configured_binary and configured_binary != DEFAULT_CODEX_BINARY:
        candidates.append((configured_binary, "config"))
    configured = load_toolchain_config().get("codex", {})
    if isinstance(configured, dict) and configured.get("binary"):
        source = "pin" if configured.get("pinned") else "contract"
        candidates.append((str(configured["binary"]), source))
    candidates.append((str(Path.home() / ".local" / "bin" / "codex"), "default-local"))
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        for path in sorted(nvm_root.glob("*/bin/codex"), reverse=True):
            candidates.append((str(path), "nvm"))
    candidates.append(("/Applications/Codex.app/Contents/Resources/codex", "codex-app"))
    for path_dir in os.get_exec_path():
        if not path_dir:
            continue
        candidates.append((str(Path(path_dir) / "codex"), "path"))
    return dedupe_candidates(candidates)


def dedupe_candidates(candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for candidate, source in candidates:
        resolved = str(Path(candidate).expanduser())
        key = str(Path(resolved).resolve()) if Path(resolved).exists() else resolved
        if key in seen:
            continue
        seen.add(key)
        result.append((resolved, source))
    return result


def inspect_codex_binary(binary: str) -> dict[str, Any]:
    path = Path(binary).expanduser()
    if not path.exists() or not os.access(path, os.X_OK):
        return {
            "binary": str(path),
            "version": "",
            "capabilities": {},
            "compatible": False,
            "reason": "binary missing or not executable",
        }
    version_result = run_probe([str(path), "--version"])
    help_result = run_probe([str(path), "exec", "--help"])
    version_text = version_result.stdout.strip() if version_result else ""
    help_text = "\n".join(
        item for item in (
            help_result.stdout if help_result else "",
            help_result.stderr if help_result else "",
        )
        if item
    )
    capabilities = {flag: flag in help_text for flag in REQUIRED_CODEX_EXEC_FLAGS}
    version_ok = codex_version_at_least(version_text, MIN_CODEX_VERSION)
    flags_ok = all(capabilities.values())
    compatible = bool(version_result and version_result.returncode == 0 and help_result and help_result.returncode == 0 and version_ok and flags_ok)
    missing_flags = [flag for flag, present in capabilities.items() if not present]
    reason = ""
    if not version_result or version_result.returncode != 0:
        reason = "version probe failed"
    elif not help_result or help_result.returncode != 0:
        reason = "exec help probe failed"
    elif not version_ok:
        reason = f"version below {format_version_tuple(MIN_CODEX_VERSION)}"
    elif missing_flags:
        reason = f"missing flags: {', '.join(missing_flags)}"
    return {
        "binary": str(path.resolve()),
        "version": version_text,
        "capabilities": capabilities,
        "compatible": compatible,
        "reason": reason,
    }


def run_probe(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=5)
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired):
        return None


def codex_version_at_least(version_text: str, minimum: tuple[int, int, int]) -> bool:
    parsed = parse_version_tuple(version_text)
    return parsed >= minimum if parsed else False


def parse_version_tuple(version_text: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text)
    if not match:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def format_version_tuple(value: tuple[int, int, int]) -> str:
    return ".".join(str(item) for item in value)


def default_state_dir() -> Path:
    base_dir = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))).expanduser()
    path = base_dir / CONFIG_DIRNAME / "orchestrator"
    path.mkdir(parents=True, exist_ok=True)
    return path


def launch_agent_plist_path(label: str = LAUNCH_AGENT_LABEL) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def launch_agent_log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / CONFIG_DIRNAME


def launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def build_launch_agent_plist(
    *,
    label: str,
    platform_root: Path,
    config_path: Path | None,
    log_dir: Path,
) -> dict[str, Any]:
    program_args = [
        str(platform_root / "bin" / "platform"),
        "orchestrator",
        "run",
        "--poll-only",
    ]
    if config_path:
        program_args.extend(["--config", str(config_path)])
    return {
        "Label": label,
        "ProgramArguments": program_args,
        "WorkingDirectory": str(platform_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_dir / "orchestrator.out.log"),
        "StandardErrorPath": str(log_dir / "orchestrator.err.log"),
    }


def unload_launch_agent(label: str, plist_path: Path, *, ignore_errors: bool) -> None:
    result = run_optional(["launchctl", "bootout", launchctl_domain(), str(plist_path)])
    if result and result.returncode == 0:
        return
    result = run_optional(["launchctl", "bootout", f"{launchctl_domain()}/{label}"])
    if result and result.returncode == 0:
        return
    if ignore_errors:
        return
    message = "Could not unload LaunchAgent"
    if result and result.stderr.strip():
        message += f": {result.stderr.strip()}"
    raise OrchestratorError(message)


def default_projects_root() -> str:
    platform_config = load_platform_user_config()
    projects_root = platform_config.get("projects_root")
    if projects_root:
        return str(Path(projects_root).expanduser().resolve())
    return str((Path.home() / "workspaces").expanduser().resolve())


def parse_bind_address(value: str) -> tuple[str, int]:
    host, _, port = value.strip().partition(":")
    return host or DEFAULT_BIND_HOST, int(port or str(DEFAULT_BIND_PORT))


def normalize_public_base_url(value: str) -> str:
    text = value.strip().rstrip("/")
    if not text:
        return ""
    return text if "://" in text else f"https://{text}"


def apply_legacy_listen_url(config: dict[str, Any], value: str) -> None:
    text = value.strip()
    if not text:
        return
    if "://" in text:
        if not config.get("public_base_url"):
            config["public_base_url"] = normalize_public_base_url(text)
        return
    host, port = parse_bind_address(text)
    config["bind_host"] = host
    config["bind_port"] = port


def require_runtime_credentials(settings: WorkerSettings) -> None:
    missing: list[str] = []
    if not settings.jira_site_url:
        missing.append("jira_site_url")
    if not settings.jira_admin_email:
        missing.append("jira_admin_email")
    if not atlassian_api_token():
        missing.append(f"ATLASSIAN_API_TOKEN or Keychain:{ATLASSIAN_TOKEN_KEYCHAIN_SERVICE}")
    if shutil.which("gh") is None:
        missing.append("gh")
    if shutil.which("claude") is None:
        missing.append("claude")
    if not settings.codex_toolchain_compatible:
        missing.append("compatible codex")
    if missing:
        raise OrchestratorError(
            "Missing orchestrator runtime prerequisites: " + ", ".join(missing)
        )


def discover_projects(project_roots: tuple[Path, ...]) -> list[RepoProject]:
    discovered: dict[str, RepoProject] = {}
    for root in project_roots:
        if not root.exists():
            continue
        for manifest_path in root.rglob(".platform/platform.yaml"):
            if any(part in {"node_modules", ".git", "__pycache__"} for part in manifest_path.parts):
                continue
            repo_root = manifest_path.parents[1]
            try:
                project = load_repo_project(repo_root)
            except OrchestratorError:
                continue
            existing = discovered.get(project.project_key)
            if existing and existing.repo_path != project.repo_path:
                raise OrchestratorError(
                    f"Duplicate Jira project key `{project.project_key}` discovered in "
                    f"{existing.repo_path} and {project.repo_path}."
                )
            discovered[project.project_key] = project
    return sorted(discovered.values(), key=lambda item: (item.project_key, str(item.repo_path)))


def load_repo_project(target: Path) -> RepoProject:
    manifest_path = target / ".platform" / "platform.yaml"
    if not manifest_path.exists():
        raise OrchestratorError(f"Missing manifest: {manifest_path}")
    manifest = load_manifest(manifest_path)
    project_key = str(manifest["issue"]["project_key"]).upper()
    return RepoProject(
        project_key=project_key,
        repo_path=target,
        repo_name=target.name,
        confluence_space=str(manifest["integrations"]["atlassian"]["confluence_space"]).upper(),
        codex_review_mode=str(
            manifest.get("integrations", {})
            .get("github", {})
            .get("codex_review", {})
            .get("mode", "auto_required")
        ),
        transition_policy=normalize_transition_policy(
            manifest.get("integrations", {}).get("atlassian", {}).get("transition_policy")
        ),
        manifest_path=manifest_path,
        source_repo=str(manifest["integrations"]["github"]["source_repo"]),
        workflow_ref=str(manifest["integrations"]["github"]["workflow_ref"]),
    )


def issue_is_auto_ready(issue: dict[str, Any]) -> bool:
    fields = issue.get("fields", {})
    labels = {str(label).lower() for label in fields.get("labels", [])}
    status = str(fields.get("status", {}).get("name", ""))
    return START_LABEL.lower() in labels and status in DEFAULT_READY_STATUSES


def default_transition_policy() -> dict[str, Any]:
    return {
        "mode": DEFAULT_TRANSITION_POLICY_MODE,
        "active_statuses": list(DEFAULT_ACTIVE_STATUS_ALIASES),
        "done_statuses": list(DEFAULT_DONE_STATUS_ALIASES),
    }


def normalize_transition_policy(value: Any) -> dict[str, Any]:
    policy = default_transition_policy()
    if not isinstance(value, dict):
        return policy
    mode = str(value.get("mode", policy["mode"])).strip() or DEFAULT_TRANSITION_POLICY_MODE
    policy["mode"] = mode
    for key, defaults in (
        ("active_statuses", DEFAULT_ACTIVE_STATUS_ALIASES),
        ("done_statuses", DEFAULT_DONE_STATUS_ALIASES),
    ):
        raw = value.get(key, defaults)
        if isinstance(raw, list):
            aliases = [str(item).strip() for item in raw if str(item).strip()]
            if aliases:
                policy[key] = aliases
    return policy


def transition_policy_from_project(project: dict[str, Any] | None) -> dict[str, Any]:
    if not project:
        return default_transition_policy()
    try:
        return normalize_transition_policy(json.loads(str(project.get("transition_policy_json") or "{}")))
    except json.JSONDecodeError:
        return default_transition_policy()


def desired_status_aliases_for_state(state: str, policy: dict[str, Any]) -> tuple[str, ...]:
    normalized = normalize_transition_policy(policy)
    if normalized.get("mode") != DEFAULT_TRANSITION_POLICY_MODE:
        return ()
    if state in ACTIVE_JIRA_TRANSITION_STATES:
        return tuple(str(item) for item in normalized["active_statuses"])
    if state == "done":
        return tuple(str(item) for item in normalized["done_statuses"])
    return ()


def status_name_matches_aliases(status_name: str, aliases: tuple[str, ...]) -> bool:
    normalized = normalize_status_name(status_name)
    return any(normalized == normalize_status_name(alias) for alias in aliases)


def normalize_status_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def select_transition_for_aliases(
    transitions: list[dict[str, Any]], aliases: tuple[str, ...]
) -> dict[str, Any] | None:
    for transition in transitions:
        to_status = str(transition.get("to", {}).get("name", ""))
        name = str(transition.get("name", ""))
        if status_name_matches_aliases(to_status, aliases) or status_name_matches_aliases(name, aliases):
            return transition
    return None


def transition_jira_issue_for_job(
    settings: WorkerSettings,
    job: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    aliases = desired_status_aliases_for_state(str(job.get("state", "")), policy)
    issue_key = str(job.get("issue_key", ""))
    if not aliases or not issue_key:
        return {"transitioned": False, "status_name": str(job.get("status_name", ""))}
    try:
        current_status = jira_issue_current_status(settings, issue_key)
        if status_name_matches_aliases(current_status, aliases):
            return {"transitioned": False, "status_name": current_status}
        transition = select_transition_for_aliases(jira_issue_transitions(settings, issue_key), aliases)
        if not transition:
            return {
                "transitioned": False,
                "status_name": current_status,
                "warning": f"No Jira transition available from `{current_status}` to one of: {', '.join(aliases)}.",
            }
        target_status = str(transition.get("to", {}).get("name") or transition.get("name") or aliases[0])
        jira_transition_issue(settings, issue_key, str(transition["id"]))
        return {
            "transitioned": True,
            "status_name": target_status,
            "transition_id": str(transition["id"]),
            "target_status": target_status,
        }
    except Exception as exc:
        return {
            "transitioned": False,
            "status_name": str(job.get("status_name", "")),
            "warning": f"Jira transition warning: {exc}",
        }


def transition_jira_issue_to_aliases(
    settings: WorkerSettings,
    issue_key: str,
    aliases: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    normalized_aliases = tuple(str(alias).strip() for alias in aliases if str(alias).strip())
    if not normalized_aliases:
        return {"transitioned": False, "status_name": ""}
    try:
        current_status = jira_issue_current_status(settings, issue_key)
        if status_name_matches_aliases(current_status, normalized_aliases):
            return {"transitioned": False, "status_name": current_status}
        transition = select_transition_for_aliases(jira_issue_transitions(settings, issue_key), normalized_aliases)
        if not transition:
            return {
                "transitioned": False,
                "status_name": current_status,
                "warning": f"No Jira transition available from `{current_status}` to one of: {', '.join(normalized_aliases)}.",
            }
        target_status = str(transition.get("to", {}).get("name") or transition.get("name") or normalized_aliases[0])
        jira_transition_issue(settings, issue_key, str(transition["id"]))
        return {
            "transitioned": True,
            "status_name": target_status,
            "transition_id": str(transition["id"]),
            "target_status": target_status,
        }
    except Exception as exc:
        return {"transitioned": False, "status_name": "", "warning": f"Jira transition warning: {exc}"}


def jira_search_auto_issues(settings: WorkerSettings, project_key: str) -> list[dict[str, Any]]:
    jql = (
        f'project = {project_key} AND labels = "{START_LABEL}" '
        f'AND status in ("{DEFAULT_READY_STATUSES[0]}", "{DEFAULT_READY_STATUSES[1]}") '
        "ORDER BY created ASC"
    )
    payload = jira_request(
        settings=settings,
        method="POST",
        path="/rest/api/3/search/jql",
        payload={
            "jql": jql,
            "fields": ["summary", "status", "labels"],
            "maxResults": 20,
        },
    )
    return list(payload.get("issues", [])) if isinstance(payload, dict) else []


def jira_search_jql(settings: WorkerSettings, jql: str, *, max_results: int = 50) -> list[dict[str, Any]]:
    payload = jira_request(
        settings=settings,
        method="POST",
        path="/rest/api/3/search/jql",
        payload={
            "jql": jql,
            "fields": ["summary", "status", "labels"],
            "maxResults": max_results,
        },
    )
    return list(payload.get("issues", [])) if isinstance(payload, dict) else []


def create_batch_from_jql(
    *,
    store: OrchestratorStore,
    settings: WorkerSettings,
    project_key: str,
    jql: str,
    max_parallel: int,
) -> dict[str, Any]:
    project = store.project(project_key)
    if not project:
        raise OrchestratorError(f"Project is not registered with the orchestrator: {project_key}")
    issues = jira_search_jql(settings, jql)
    issue_summaries = [
        {
            "issue_key": str(issue.get("key", "")),
            "summary": str(issue.get("fields", {}).get("summary", "")),
            "status": str(issue.get("fields", {}).get("status", {}).get("name", "")),
        }
        for issue in issues
        if normalize_issue_key(issue.get("key"))
    ]
    if not issue_summaries:
        raise OrchestratorError("Batch JQL returned no issues.")
    batch_id = f"{project_key}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    plan = run_claude_batch_plan(
        store=store,
        settings=settings,
        project=project,
        batch_id=batch_id,
        jql=jql,
        issues=issue_summaries,
        max_parallel=max_parallel,
    )
    store.create_batch(
        batch_id=batch_id,
        project_key=project_key,
        jql=jql,
        max_parallel=max_parallel,
        summary=str(plan.get("summary", "")),
        design_memo=str(plan.get("design_memo", "")),
    )
    planned_units = {str(item.get("issue_key", "")).upper(): item for item in plan.get("work_units", []) if isinstance(item, dict)}
    for issue in issue_summaries:
        issue_key = str(issue["issue_key"]).upper()
        unit = planned_units.get(issue_key, {})
        contract = normalize_task_contract(
            unit.get("task_contract") if isinstance(unit.get("task_contract"), dict) else {
                "issue_key": issue_key,
                "goal": issue["summary"],
                "files_in_scope": [],
                "constraints": [],
                "dependencies": unit.get("dependencies", []),
            },
            issue_key=issue_key,
        )
        dependencies = list_of_strings(unit.get("dependencies") or contract.get("dependencies") or [])
        conflict_group = str(unit.get("conflict_group") or derive_conflict_group(contract) or issue_key)
        store.enqueue_issue(
            project_key=project_key,
            repo_path=str(project["repo_path"]),
            issue_key=issue_key,
            status=str(issue["status"]),
            summary=str(issue["summary"]),
        )
        store.upsert_work_unit(
            issue_key=issue_key,
            batch_id=batch_id,
            project_key=project_key,
            repo_path=str(project["repo_path"]),
            conflict_group=conflict_group,
            dependencies=dependencies,
            contract=contract,
        )
        store.record_contract(
            issue_key,
            batch_id=batch_id,
            round_number=0,
            contract=contract,
            created_by="claude-batch",
        )
    return {"batch_id": batch_id, "project_key": project_key, "issue_count": len(issue_summaries), "plan": plan}


def run_claude_batch_plan(
    *,
    store: OrchestratorStore,
    settings: WorkerSettings,
    project: dict[str, Any],
    batch_id: str,
    jql: str,
    issues: list[dict[str, str]],
    max_parallel: int,
) -> dict[str, Any]:
    schema = REPO_ROOT / "schemas" / "orchestrator" / "batch-plan.json"
    prompt = f"""
You are the Claude coordinator for a multi-issue development batch.

Create a DAG plan that lets Codex workers implement independent issues in parallel without design drift.

Project: {project.get("project_key")}
Batch: {batch_id}
JQL: {jql}
Max parallel: {max_parallel}

Rules:
- Keep 1 issue = 1 worktree = 1 branch = 1 PR.
- Assign the same conflict_group to issues likely to touch the same files, protected paths, schema, or shared API.
- Use dependencies when one issue must wait for another PR to merge.
- Produce a task_contract for each issue.

Issues:
{json.dumps(issues, indent=2, ensure_ascii=False)}

Return JSON only and match the schema.
""".strip()
    try:
        result = run_claude_json(
            settings=settings,
            store=store,
            issue_key=str(issues[0]["issue_key"]),
            cwd=Path(str(project["repo_path"])),
            prompt=prompt,
            schema_path=schema,
        )
        result["status"] = "success"
        return result
    except Exception as exc:
        return fallback_batch_plan(issues, reason=str(exc))


def fallback_batch_plan(issues: list[dict[str, str]], *, reason: str) -> dict[str, Any]:
    return {
        "summary": f"Fallback deterministic batch plan used: {reason}",
        "design_memo": "Claude batch planning failed; each issue is treated as independent unless its contract declares dependencies.",
        "work_units": [
            {
                "issue_key": issue["issue_key"],
                "conflict_group": issue["issue_key"],
                "dependencies": [],
                "task_contract": {
                    "issue_key": issue["issue_key"],
                    "goal": issue["summary"],
                    "acceptance_criteria": [],
                    "files_in_scope": [],
                    "out_of_scope": [],
                    "constraints": [],
                    "validation_commands": [],
                    "risk_flags": ["fallback_batch_plan"],
                    "dependencies": [],
                },
            }
            for issue in issues
        ],
    }


def derive_conflict_group(contract: dict[str, Any]) -> str:
    files = list_of_strings(contract.get("files_in_scope"))
    if not files:
        return ""
    first = files[0].replace("\\", "/")
    parts = [part for part in first.split("/") if part]
    return "/".join(parts[:2]) if len(parts) >= 2 else parts[0]


def jira_get_issue(settings: WorkerSettings, issue_key: str) -> dict[str, Any]:
    payload = jira_request(
        settings=settings,
        method="GET",
        path=f"/rest/api/3/issue/{issue_key}?" + parse.urlencode(
            {
                "fields": "summary,description,status,labels",
            }
        ),
    )
    if not isinstance(payload, dict):
        raise OrchestratorError(f"Could not fetch Jira issue {issue_key}")
    return payload


def jira_issue_current_status(settings: WorkerSettings, issue_key: str) -> str:
    issue = jira_get_issue(settings, issue_key)
    return str(issue.get("fields", {}).get("status", {}).get("name", ""))


def jira_issue_transitions(settings: WorkerSettings, issue_key: str) -> list[dict[str, Any]]:
    payload = jira_request(
        settings=settings,
        method="GET",
        path=f"/rest/api/3/issue/{issue_key}/transitions",
    )
    transitions = payload.get("transitions", []) if isinstance(payload, dict) else []
    return [transition for transition in transitions if isinstance(transition, dict)]


def jira_transition_issue(settings: WorkerSettings, issue_key: str, transition_id: str) -> None:
    payload = {"transition": {"id": transition_id}}
    for attempt in range(2):
        try:
            jira_request(
                settings=settings,
                method="POST",
                path=f"/rest/api/3/issue/{issue_key}/transitions",
                payload=payload,
            )
            return
        except JiraRequestError as exc:
            if exc.status_code == HTTPStatus.CONFLICT and attempt == 0:
                time.sleep(1)
                continue
            raise


def jira_issue_comments(settings: WorkerSettings, issue_key: str) -> list[dict[str, Any]]:
    payload = jira_request(
        settings=settings,
        method="GET",
        path=f"/rest/api/3/issue/{issue_key}/comment?"
        + parse.urlencode({"orderBy": "created", "maxResults": 100}),
    )
    comments = payload.get("comments", []) if isinstance(payload, dict) else []
    return [comment for comment in comments if isinstance(comment, dict)]


def jira_get_project(
    *, site_url: str, admin_email: str, api_token: str, project_key: str
) -> dict[str, Any]:
    return jira_request_raw(
        site_url=site_url,
        admin_email=admin_email,
        api_token=api_token,
        method="GET",
        path=f"/rest/api/3/project/{project_key}",
    )


def jira_issue_types_for_project(settings: WorkerSettings, project_key: str) -> list[dict[str, Any]]:
    payload = jira_request(
        settings=settings,
        method="GET",
        path="/rest/api/3/issuetype/project?" + parse.urlencode({"projectId": project_key}),
    )
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def jira_find_issue(settings: WorkerSettings, jql: str) -> dict[str, Any] | None:
    payload = jira_request(
        settings=settings,
        method="POST",
        path="/rest/api/3/search/jql",
        payload={"jql": jql, "fields": ["summary"], "maxResults": 1},
    )
    issues = payload.get("issues", []) if isinstance(payload, dict) else []
    return issues[0] if issues else None


def ensure_control_issue(settings: WorkerSettings, project: RepoProject) -> str:
    existing = jira_find_issue(
        settings,
        f'project = {project.project_key} AND labels = "{CONTROL_LABEL}" ORDER BY created ASC',
    )
    if existing:
        return str(existing["key"])
    issue_type = jira_primary_issue_type(settings, project.project_key)
    payload = jira_request(
        settings=settings,
        method="POST",
        path="/rest/api/3/issue",
        payload={
            "fields": {
                "project": {"key": project.project_key},
                "issuetype": {"id": issue_type["id"]},
                "summary": f"[AI Control] {project.project_key} orchestrator control issue",
                "labels": [CONTROL_LABEL],
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Use /ai pause-project, /ai resume-project, or /ai drain-project in comments.",
                                }
                            ],
                        }
                    ],
                },
            }
        },
    )
    key = payload.get("key") if isinstance(payload, dict) else None
    if not key:
        raise OrchestratorError(f"Failed to create control issue for {project.project_key}")
    return str(key)


def jira_primary_issue_type(settings: WorkerSettings, project_key: str) -> dict[str, Any]:
    payload = jira_request(
        settings=settings,
        method="GET",
        path=f"/rest/api/3/project/{project_key}",
    )
    style = str(payload.get("style", "team-managed")) if isinstance(payload, dict) else "team-managed"
    project_id = str(payload.get("id", project_key)) if isinstance(payload, dict) else project_key
    metadata = jira_request(
        settings=settings,
        method="GET",
        path="/rest/api/3/issuetype/project?" + parse.urlencode({"projectId": project_id}),
    )
    issue_types = metadata if isinstance(metadata, list) else []
    for preferred in ("Task", "Story"):
        for issue_type in issue_types:
            if issue_type.get("name") == preferred:
                return issue_type
    if issue_types:
        return issue_types[0]
    raise OrchestratorError(f"No issue type available for Jira project {project_key} ({style})")


def jira_request(settings: WorkerSettings, *, method: str, path: str, payload: Any | None = None) -> Any:
    api_token = atlassian_api_token()
    return jira_request_raw(
        site_url=settings.jira_site_url,
        admin_email=settings.jira_admin_email,
        api_token=api_token,
        method=method,
        path=path,
        payload=payload,
    )


def jira_request_raw(
    *,
    site_url: str,
    admin_email: str,
    api_token: str,
    method: str,
    path: str,
    payload: Any | None = None,
) -> Any:
    if not site_url or not admin_email or not api_token:
        raise OrchestratorError(
            "Jira request requires site URL, admin email, and ATLASSIAN_API_TOKEN "
            f"or Keychain:{ATLASSIAN_TOKEN_KEYCHAIN_SERVICE}."
        )
    url = f"{site_url}{path}"
    headers = {
        "Accept": "application/json",
        "Authorization": "Basic "
        + base64.b64encode(f"{admin_email}:{api_token}".encode("utf-8")).decode("ascii"),
    }
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method=method.upper())
    last_error: Exception | None = None
    for attempt in range(DEFAULT_API_RETRY_ATTEMPTS):
        try:
            with request.urlopen(req, timeout=30) as response:
                content = response.read().decode("utf-8")
                if not content:
                    return {}
                return json.loads(content)
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = JiraRequestError(
                f"Jira request failed ({method} {path}): {exc.code} {body}",
                status_code=exc.code,
            )
            if exc.code not in TRANSIENT_HTTP_STATUSES or attempt == DEFAULT_API_RETRY_ATTEMPTS - 1:
                raise last_error from exc
        except error.URLError as exc:
            last_error = OrchestratorError(f"Jira request failed ({method} {path}): {exc.reason}")
            if attempt == DEFAULT_API_RETRY_ATTEMPTS - 1:
                raise last_error from exc
        time.sleep(api_retry_delay(attempt))
    if last_error:
        raise last_error
    return {}


def jira_cloud_id(settings: WorkerSettings) -> str:
    site_url = settings.jira_site_url.rstrip("/")
    req = request.Request(f"{site_url}/_edge/tenant_info", headers={"Accept": "application/json"})
    try:
        with request.urlopen(req) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise OrchestratorError(f"Could not resolve Atlassian cloud id: {exc.code} {body}") from exc
    cloud_id = payload.get("cloudId")
    if not cloud_id:
        raise OrchestratorError("Atlassian cloudId is missing from /_edge/tenant_info")
    return str(cloud_id)


def automation_request(
    settings: WorkerSettings, *, method: str, path: str, payload: Any | None = None
) -> Any:
    api_token = atlassian_api_token()
    cloud_id = jira_cloud_id(settings)
    url = f"https://api.atlassian.com/automation/public/jira/{cloud_id}/rest/v1{path}"
    headers = {
        "Accept": "application/json",
        "Authorization": "Basic "
        + base64.b64encode(f"{settings.jira_admin_email}:{api_token}".encode("utf-8")).decode("ascii"),
    }
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, headers=headers, data=data, method=method.upper())
    try:
        with request.urlopen(req) as response:
            content = response.read().decode("utf-8")
            return json.loads(content) if content else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise OrchestratorError(f"Automation API request failed ({method} {path}): {exc.code} {body}") from exc


def ensure_automation_rules(
    settings: WorkerSettings,
    project: RepoProject,
    project_id: str,
    webhook_secret: str,
) -> dict[str, str]:
    project_ari = f"ari:cloud:jira:{jira_cloud_id(settings)}:project/{project_id}"
    summaries = automation_request(
        settings,
        method="POST",
        path="/rule/summary",
        payload={"scope": project_ari, "limit": 100},
    )
    rules = summaries.get("data", []) if isinstance(summaries, dict) else []
    by_name = {str(item.get("name")): item for item in rules if isinstance(item, dict)}
    lifecycle_name = f"{project.project_key} / AI lifecycle"
    comment_name = f"{project.project_key} / AI comments"
    lifecycle_uuid = ensure_automation_rule(
        settings=settings,
        existing=by_name.get(lifecycle_name) or by_name.get(f"[platform] {project.project_key} lifecycle -> orchestrator"),
        payload=build_lifecycle_rule_payload(
            settings,
            project,
            project_id,
            project_ari,
            lifecycle_name,
            webhook_secret,
        ),
    )
    comment_uuid = ensure_automation_rule(
        settings=settings,
        existing=by_name.get(comment_name) or by_name.get(f"[platform] {project.project_key} comments -> orchestrator"),
        payload=build_comment_rule_payload(
            settings,
            project,
            project_id,
            project_ari,
            comment_name,
            webhook_secret,
        ),
    )
    return {"lifecycle_rule_uuid": lifecycle_uuid, "comment_rule_uuid": comment_uuid}


def disable_automation_rules(settings: WorkerSettings, rule_uuids: list[str]) -> list[str]:
    disabled: list[str] = []
    for rule_uuid in dict.fromkeys(rule_uuids):
        if not rule_uuid:
            continue
        try:
            payload = automation_request(
                settings,
                method="GET",
                path=f"/rule/{rule_uuid}?redactSensitiveFields=true",
            )
            rule = payload.get("rule", {}) if isinstance(payload, dict) else {}
            if not rule or rule.get("state") == "DISABLED":
                continue
            rule["state"] = "DISABLED"
            automation_request(
                settings,
                method="PUT",
                path=f"/rule/{rule_uuid}",
                payload={"rule": rule, "connections": payload.get("connections", [])},
            )
            disabled.append(rule_uuid)
        except OrchestratorError as exc:
            print(f"warning: could not disable legacy Automation rule {rule_uuid}: {exc}", file=sys.stderr)
    return disabled


def ensure_automation_rule(
    *, settings: WorkerSettings, existing: dict[str, Any] | None, payload: dict[str, Any]
) -> str:
    if existing and existing.get("uuid"):
        automation_request(
            settings,
            method="PUT",
            path=f"/rule/{existing['uuid']}",
            payload=payload,
        )
        return str(existing["uuid"])
    result = automation_request(settings, method="POST", path="/rule", payload=payload)
    rule_uuid = result.get("ruleUuid") if isinstance(result, dict) else None
    if not rule_uuid:
        raise OrchestratorError("Automation rule creation did not return ruleUuid.")
    return str(rule_uuid)


def build_lifecycle_rule_payload(
    settings: WorkerSettings,
    project: RepoProject,
    project_id: str,
    project_ari: str,
    rule_name: str,
    webhook_secret: str,
) -> dict[str, Any]:
    return {
        "rule": build_rule_payload(
            settings=settings,
            project=project,
            project_id=project_id,
            project_ari=project_ari,
            rule_name=rule_name,
            trigger_type="jira.issue.event.trigger:updated",
            trigger_value={"eventKey": "jira:issue_updated", "issueEvent": "issue_updated"},
            custom_body=(
                '{"event_type":"issue_lifecycle",'
                '"project_key":"{{issue.project.key}}",'
                '"issue_key":"{{issue.key}}",'
                '"issue_id":"{{issue.id}}",'
                '"status":"{{issue.status.name}}"}'
            ),
            webhook_secret=webhook_secret,
        ),
        "connections": [],
    }


def build_comment_rule_payload(
    settings: WorkerSettings,
    project: RepoProject,
    project_id: str,
    project_ari: str,
    rule_name: str,
    webhook_secret: str,
) -> dict[str, Any]:
    return {
        "rule": build_rule_payload(
            settings=settings,
            project=project,
            project_id=project_id,
            project_ari=project_ari,
            rule_name=rule_name,
            trigger_type="jira.issue.event.trigger:commented",
            trigger_value={
                "eventKey": "jira:issue_updated",
                "issueEvent": "issue_commented",
                "eventTypes": [],
            },
            custom_body=(
                '{"event_type":"comment_control",'
                '"project_key":"{{issue.project.key}}",'
                '"issue_key":"{{issue.key}}",'
                '"issue_id":"{{issue.id}}",'
                '"comment_id":"{{comment.id}}",'
                '"command":"{{comment.body}}"}'
            ),
            webhook_secret=webhook_secret,
        ),
        "connections": [],
    }


def build_rule_payload(
    *,
    settings: WorkerSettings,
    project: RepoProject,
    project_id: str,
    project_ari: str,
    rule_name: str,
    trigger_type: str,
    trigger_value: dict[str, Any],
    custom_body: str,
    webhook_secret: str,
) -> dict[str, Any]:
    cloud_id = "{{CLOUD_ID}}"
    actor_account_id = "{{ACCOUNT_ID}}"
    public_base_url = settings.public_base_url or "{{PUBLIC_BASE_URL}}"
    if settings.jira_site_url and settings.jira_admin_email and atlassian_api_token():
        cloud_id = jira_cloud_id(settings)
        actor_account_id = jira_request(settings, method="GET", path="/rest/api/3/myself").get("accountId", "")
    webhook_action = {
        "component": "ACTION",
        "schemaVersion": 1,
        "type": "jira.issue.outgoing.webhook",
        "value": {
            "url": f"{public_base_url}/jira/events/{project.project_key}",
            "headers": [
                {
                    "id": "_header_secret",
                    "name": DEFAULT_HEADER_NAME,
                    "value": webhook_secret,
                    "headerSecure": True,
                },
                {
                    "id": "_header_content_type",
                    "name": "Content-Type",
                    "value": "application/json",
                    "headerSecure": False,
                },
            ],
            "sendIssue": False,
            "contentType": "custom",
            "customBody": custom_body,
            "method": "POST",
            "responseEnabled": False,
            "continueOnErrorEnabled": False,
        },
        "children": [],
        "conditions": [],
        "connectionId": None,
    }
    trigger = {
        "component": "TRIGGER",
        "schemaVersion": 1,
        "type": trigger_type,
        "value": trigger_value,
        "children": [],
        "conditions": [],
        "connectionId": None,
    }
    return {
        "name": rule_name,
        "description": f"Managed by the platform orchestrator for {project.project_key}.",
        "authorAccountId": actor_account_id,
        "actor": {"actor": actor_account_id, "type": "ACCOUNT_ID"},
        "canOtherRuleTrigger": False,
        "notifyOnError": "FIRSTERROR",
        "labels": ["platform-orchestrator"],
        "projects": [{"projectId": str(project_id), "projectTypeKey": "software"}],
        "ruleScopeARIs": [project_ari],
        "ruleHome": {
            "ruleLifeCycleHome": {"locationARI": project_ari},
            "ruleBillingHome": {"locationARI": f"ari:cloud:jira-software::site/{cloud_id}"},
        },
        "writeAccessType": "OWNER_ONLY",
        "state": "ENABLED",
        "trigger": trigger,
        "components": [webhook_action],
    }


def export_automation_rule_blueprints(settings: WorkerSettings, project: RepoProject) -> None:
    export_dir = project.repo_path / ".platform" / "orchestrator" / "automation-rules"
    export_dir.mkdir(parents=True, exist_ok=True)
    placeholder_id = "{{PROJECT_ID}}"
    placeholder_ari = "{{PROJECT_ARI}}"
    lifecycle = build_lifecycle_rule_payload(
        settings=settings,
        project=project,
        project_id=placeholder_id,
        project_ari=placeholder_ari,
        rule_name=f"{project.project_key} / AI lifecycle",
        webhook_secret="{{WEBHOOK_SECRET}}",
    )
    comment = build_comment_rule_payload(
        settings=settings,
        project=project,
        project_id=placeholder_id,
        project_ari=placeholder_ari,
        rule_name=f"{project.project_key} / AI comments",
        webhook_secret="{{WEBHOOK_SECRET}}",
    )
    (export_dir / "lifecycle.rule.json").write_text(
        json.dumps(lifecycle, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (export_dir / "comment.rule.json").write_text(
        json.dumps(comment, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def ensure_issue_worktree(repo_path: Path, project_key: str, issue_key: str, summary: str) -> tuple[str, Path]:
    worktree_root = default_state_dir() / WORKTREE_ROOTNAME / project_key / repo_path.name
    worktree_root.mkdir(parents=True, exist_ok=True)
    branch = f"feat/{issue_key}-{slugify(summary)}"
    worktree_path = worktree_root / issue_key
    if (worktree_path / ".git").exists():
        return branch, worktree_path
    run_command(["git", "fetch", "origin", "main"], cwd=repo_path)
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    run_command(
        ["git", "worktree", "add", "-B", branch, str(worktree_path), "origin/main"],
        cwd=repo_path,
    )
    return branch, worktree_path


def ensure_issue_spec(issue_key: str, worktree_path: Path, summary: str) -> None:
    spec_path = worktree_path / "docs" / "specs" / f"{issue_key}.md"
    if spec_path.exists():
        return
    run_command(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "platform.py"),
            "new-spec",
            issue_key,
            "--target",
            str(worktree_path),
            "--title",
            summary,
            "--force",
        ],
        cwd=worktree_path,
    )


def run_claude_planning(
    store: OrchestratorStore,
    settings: WorkerSettings,
    worktree_path: Path,
    issue_key: str,
    project_key: str,
    *,
    issue_summary: str,
    issue_description: str,
) -> dict[str, Any]:
    schema = REPO_ROOT / "schemas" / "orchestrator" / "claude-plan.json"
    prompt = f"""
You are the planning stage of the resident orchestrator.

Constraints:
- Work only inside Jira project {project_key}.
- Use Jira/Confluence scope from this repo only.
- Update docs/specs/{issue_key}.md in this repo before finishing.
- Return JSON only and match the provided schema.

Issue summary:
{issue_summary}

Issue description:
{issue_description or '(empty)'}

Output requirements:
- goal: one-sentence implementation goal
- files_in_scope: list of likely files Codex should touch
- constraints: risk/compatibility/rollback constraints
- tasks: concrete coding tasks for Codex
- jira_summary: short progress summary for Jira
""".strip()
    result = run_claude_json(
        settings=settings,
        store=store,
        issue_key=issue_key,
        cwd=worktree_path,
        prompt=prompt,
        schema_path=schema,
    )
    result["status"] = "success"
    return result


def normalize_task_contract(plan_payload: dict[str, Any], *, issue_key: str) -> dict[str, Any]:
    existing = plan_payload.get("task_contract")
    if isinstance(existing, dict):
        contract = dict(existing)
    else:
        contract = {
            "issue_key": issue_key,
            "goal": str(plan_payload.get("goal") or plan_payload.get("summary") or issue_key),
            "acceptance_criteria": list_of_strings(plan_payload.get("acceptance_criteria") or []),
            "files_in_scope": list_of_strings(plan_payload.get("files_in_scope") or []),
            "out_of_scope": list_of_strings(plan_payload.get("out_of_scope") or []),
            "constraints": list_of_strings(plan_payload.get("constraints") or []),
            "validation_commands": list_of_strings(plan_payload.get("validation_commands") or []),
            "risk_flags": list_of_strings(plan_payload.get("risk_flags") or []),
            "dependencies": list_of_strings(plan_payload.get("dependencies") or []),
        }
    contract["issue_key"] = str(contract.get("issue_key") or issue_key).upper()
    for key in (
        "acceptance_criteria",
        "files_in_scope",
        "out_of_scope",
        "constraints",
        "validation_commands",
        "risk_flags",
        "dependencies",
    ):
        contract[key] = list_of_strings(contract.get(key))
    contract["goal"] = str(contract.get("goal") or issue_key)
    return contract


def list_of_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def run_contract_handshake(
    store: OrchestratorStore,
    settings: WorkerSettings,
    worktree_path: Path,
    issue_key: str,
    project_key: str,
    batch_id: str,
    task_contract: dict[str, Any],
) -> dict[str, Any]:
    contract = dict(task_contract)
    max_rounds = int(getattr(settings, "max_baton_rounds", DEFAULT_MAX_BATON_ROUNDS))
    for round_number in range(1, max_rounds + 1):
        understanding = run_codex_understanding(
            store,
            settings,
            worktree_path,
            project_key,
            issue_key,
            contract,
        )
        store.record_message(
            issue_key,
            batch_id=batch_id,
            sender="codex",
            message_type="task_understanding",
            payload=understanding,
        )
        if understanding.get("changed_files"):
            return {
                "status": "blocked",
                "summary": "Codex changed files during the read-only understanding stage.",
                "task_contract": contract,
                "round": round_number,
            }
        decision = run_claude_coordinator_decision(
            store,
            settings,
            worktree_path,
            issue_key,
            project_key,
            contract,
            understanding,
        )
        store.record_decision(
            issue_key,
            batch_id=batch_id,
            round_number=round_number,
            decision=decision,
        )
        if decision.get("decision") == "approved":
            return {
                "status": "approved",
                "summary": decision.get("summary", "Contract approved."),
                "task_contract": contract,
                "round": round_number,
            }
        if decision.get("decision") == "revise_contract":
            contract = normalize_task_contract(
                decision.get("task_contract") if isinstance(decision.get("task_contract"), dict) else contract,
                issue_key=issue_key,
            )
            store.record_contract(
                issue_key,
                batch_id=batch_id,
                round_number=round_number,
                contract=contract,
                created_by="claude",
            )
            continue
        return {
            "status": "blocked",
            "summary": decision.get("blocker") or decision.get("summary") or "Contract handshake blocked.",
            "task_contract": contract,
            "round": round_number,
        }
    return {
        "status": "blocked",
        "summary": f"Contract handshake exceeded max baton rounds ({max_rounds}).",
        "task_contract": contract,
        "round": max_rounds,
    }


def run_claude_coordinator_decision(
    store: OrchestratorStore,
    settings: WorkerSettings,
    worktree_path: Path,
    issue_key: str,
    project_key: str,
    task_contract: dict[str, Any],
    understanding: dict[str, Any],
) -> dict[str, Any]:
    schema = REPO_ROOT / "schemas" / "orchestrator" / "coordinator-decision.json"
    prompt = f"""
You are the Claude coordinator for a mediated Claude-Codex baton.

Issue: {issue_key}
Project: {project_key}

Decide whether Codex understood the task before implementation.
- approved: Codex may implement exactly this contract.
- revise_contract: return a corrected task_contract.
- split_task: the issue is too broad for one PR.
- block: unsafe or ambiguous enough to stop.

Task contract:
{json.dumps(task_contract, indent=2, ensure_ascii=False)}

Codex understanding:
{json.dumps(understanding, indent=2, ensure_ascii=False)}

Return JSON only and match the schema.
""".strip()
    result = run_claude_json(
        settings=settings,
        store=store,
        issue_key=issue_key,
        cwd=worktree_path,
        prompt=prompt,
        schema_path=schema,
    )
    result["status"] = "success"
    return result


def run_claude_integrate(
    store: OrchestratorStore,
    settings: WorkerSettings,
    worktree_path: Path,
    issue_key: str,
    project_key: str,
    review_result: dict[str, Any],
) -> dict[str, Any]:
    schema = REPO_ROOT / "schemas" / "orchestrator" / "claude-integrate.json"
    prompt = f"""
You are the integration stage of the resident orchestrator.

Issue: {issue_key}
Project: {project_key}

The coding stage and local review already ran. Decide the next orchestrator action.
- open_pr: implementation is ready for a PR
- retry_coding: another Codex coding pass is needed
- blocked: stop and report the blocker

Review signal:
{json.dumps(review_result, indent=2, ensure_ascii=False)}

Return JSON only and match the schema.
""".strip()
    result = run_claude_json(
        settings=settings,
        store=store,
        issue_key=issue_key,
        cwd=worktree_path,
        prompt=prompt,
        schema_path=schema,
    )
    result["status"] = "success"
    return result


def run_claude_json(
    *,
    settings: WorkerSettings,
    store: OrchestratorStore,
    issue_key: str,
    cwd: Path,
    prompt: str,
    schema_path: Path,
) -> dict[str, Any]:
    argv = [
        "claude",
        "-p",
    ]
    if settings.claude_model:
        argv.extend(["--model", settings.claude_model])
    if settings.claude_effort:
        argv.extend(["--effort", settings.claude_effort])
    argv.extend(
        [
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "json",
            "--json-schema",
            schema_path.read_text(encoding="utf-8"),
            prompt,
        ]
    )
    result = run_tracked_command(
        store,
        issue_key,
        argv,
        cwd=cwd,
        timeout_seconds=int(getattr(settings, "claude_timeout_seconds", DEFAULT_CLAUDE_TIMEOUT_SECONDS)),
    )
    payload = json.loads(result.stdout)
    structured = payload.get("structured_output")
    if not isinstance(structured, dict):
        raise OrchestratorError("Claude did not return structured_output.")
    return structured


def run_codex_understanding(
    store: OrchestratorStore,
    settings: WorkerSettings,
    worktree_path: Path,
    project_key: str,
    issue_key: str,
    task_contract: dict[str, Any],
) -> dict[str, Any]:
    schema = REPO_ROOT / "schemas" / "orchestrator" / "task-understanding.json"
    output_file = issue_state_dir(project_key, issue_key) / "codex.understanding.json"
    if output_file.exists():
        output_file.unlink()
    prompt = f"""
Read the task contract and return your understanding before implementation.

Rules:
- Do not edit, create, delete, move, stage, or commit files.
- Do not open a PR.
- Report ambiguity and risky assumptions explicitly.
- Return JSON matching the provided schema.

Task contract:
{json.dumps(task_contract, indent=2, ensure_ascii=False)}
""".strip()
    try:
        argv = codex_exec_argv(settings)
    except OrchestratorError as exc:
        return {
            "summary": f"Codex CLI unavailable: {exc}",
            "understood_scope": "",
            "planned_files": [],
            "ambiguities": [str(exc)],
            "risky_assumptions": [],
            "validation_plan": [],
            "changed_files": git_changed_files(worktree_path),
            "status": "blocked",
        }
    argv.extend(
        [
            "--json",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output_file),
            "--cd",
            str(worktree_path),
            prompt,
        ]
    )
    process = start_process(argv, cwd=worktree_path)
    store.update_job(issue_key, active_pid=process.pid)
    try:
        stdout, stderr, timed_out = communicate_or_terminate(
            process,
            timeout_seconds=int(getattr(settings, "codex_review_timeout_seconds", DEFAULT_LOCAL_REVIEW_TIMEOUT_SECONDS)),
        )
    finally:
        store.update_job(issue_key, active_pid=None)
    changed_files = git_changed_files(worktree_path)
    payload: dict[str, Any] = {
        "summary": summarize_codex_jsonl(stdout) or "Codex understanding completed.",
        "understood_scope": "",
        "planned_files": [],
        "ambiguities": [],
        "risky_assumptions": [],
        "validation_plan": [],
        "changed_files": changed_files,
        "status": status_from_process(process.returncode, timed_out=timed_out, has_fallback=False),
        "exit_code": 124 if timed_out else process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
    }
    if output_file.exists():
        try:
            structured = json.loads(output_file.read_text(encoding="utf-8"))
            if isinstance(structured, dict):
                payload.update(structured)
        except json.JSONDecodeError:
            payload["summary"] = output_file.read_text(encoding="utf-8").strip()
    if timed_out:
        payload["ambiguities"] = [*list_of_strings(payload.get("ambiguities")), "Codex understanding timed out."]
    return payload


def run_codex_exec(
    store: OrchestratorStore,
    settings: WorkerSettings,
    worktree_path: Path,
    project_key: str,
    issue_key: str,
    branch: str,
    plan_payload: dict[str, Any],
) -> dict[str, Any]:
    schema = REPO_ROOT / "schemas" / "orchestrator" / "task-result.json"
    output_file = issue_state_dir(project_key, issue_key) / "codex.exec.json"
    if output_file.exists():
        output_file.unlink()
    prompt = f"""
Implement the current issue on branch {branch}.

Constraints:
- Work only inside this repository and this branch.
- Keep Jira/Confluence scope limited to the repo manifest.
- Run the repo validation commands that are relevant after editing.
- Do not open a PR.

Return JSON matching the provided schema.

Claude plan:
{json.dumps(plan_payload, indent=2, ensure_ascii=False)}

Task contract:
{json.dumps(normalize_task_contract(plan_payload, issue_key=issue_key), indent=2, ensure_ascii=False)}
""".strip()
    first = run_codex_exec_attempt(
        store,
        settings,
        worktree_path,
        project_key,
        issue_key,
        branch,
        prompt,
        schema,
        output_file,
        exclude_binaries=(),
    )
    if classify_codex_cli_failure(first) == "codex_cli_incompatible":
        store.record_step(issue_key, "toolchain_recovery", "warning", codex_recovery_payload(first, None))
        retry = run_codex_exec_attempt(
            store,
            settings,
            worktree_path,
            project_key,
            issue_key,
            branch,
            prompt,
            schema,
            output_file,
            exclude_binaries=(str(first.get("codex_binary", "")),),
        )
        if classify_codex_cli_failure(retry) != "codex_cli_incompatible" and retry["status"] in {"success", "fallback"}:
            retry["toolchain_recovery"] = codex_recovery_payload(first, retry)
            retry["summary"] = retry.get("summary") or "Codex CLI recovered and completed."
            store.record_step(issue_key, "toolchain_recovery", "success", retry["toolchain_recovery"])
            return retry
        retry["status"] = "blocked"
        retry["toolchain_recovery"] = codex_recovery_payload(first, retry)
        retry["summary"] = codex_recovery_blocker_summary(retry["toolchain_recovery"])
        store.record_step(issue_key, "toolchain_recovery", "failed", retry["toolchain_recovery"])
        return retry
    return first


def run_codex_exec_attempt(
    store: OrchestratorStore,
    settings: WorkerSettings,
    worktree_path: Path,
    project_key: str,
    issue_key: str,
    branch: str,
    prompt: str,
    schema: Path,
    output_file: Path,
    *,
    exclude_binaries: tuple[str, ...],
) -> dict[str, Any]:
    if output_file.exists():
        output_file.unlink()
    try:
        argv = codex_exec_argv(settings, exclude_binaries=exclude_binaries)
    except OrchestratorError as exc:
        return {
            "status": "blocked",
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "changed_files": git_changed_files(worktree_path),
            "validation_passed": False,
            "fallback_used": False,
            "timed_out": False,
            "branch": branch,
            "codex_binary": "",
            "codex_version": "",
            "summary": f"Codex CLI unavailable: {exc}",
        }
    argv.extend(
        [
            "--json",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output_file),
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            str(worktree_path),
            prompt,
        ]
    )
    process = start_process(argv, cwd=worktree_path)
    store.update_job(issue_key, active_pid=process.pid)
    try:
        stdout, stderr, timed_out = communicate_or_terminate(
            process,
            timeout_seconds=int(getattr(settings, "codex_exec_timeout_seconds", DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS)),
        )
    finally:
        store.update_job(issue_key, active_pid=None)
    changed_files = git_changed_files(worktree_path)
    payload = {
        "status": status_from_process(process.returncode, timed_out=timed_out, has_fallback=bool(changed_files)),
        "exit_code": 124 if timed_out else process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "changed_files": changed_files,
        "validation_passed": process.returncode == 0 and not timed_out,
        "fallback_used": bool(timed_out and changed_files),
        "timed_out": timed_out,
        "branch": branch,
        "codex_binary": argv[0],
        "codex_version": inspect_codex_binary(argv[0]).get("version", ""),
    }
    if output_file.exists():
        try:
            payload.update(json.loads(output_file.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            payload["summary"] = output_file.read_text(encoding="utf-8").strip()
    else:
        payload["summary"] = summarize_codex_jsonl(stdout)
    if timed_out and not payload.get("summary"):
        payload["summary"] = build_exec_timeout_summary(
            worktree_path,
            changed_files,
            timeout_seconds=int(getattr(settings, "codex_exec_timeout_seconds", DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS)),
        )
    if timed_out:
        payload["status"] = status_from_process(process.returncode, timed_out=True, has_fallback=bool(changed_files))
        payload["exit_code"] = 124
        payload["validation_passed"] = False
        payload["fallback_used"] = bool(changed_files)
        payload["timed_out"] = True
        payload["summary"] = payload.get("summary") or build_exec_timeout_summary(
            worktree_path,
            changed_files,
            timeout_seconds=int(getattr(settings, "codex_exec_timeout_seconds", DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS)),
        )
    return payload


def run_codex_review(
    store: OrchestratorStore,
    settings: WorkerSettings,
    issue_key: str,
    worktree_path: Path,
) -> dict[str, Any]:
    stage_meaningful_changes(worktree_path)
    first = run_codex_review_attempt(store, settings, issue_key, worktree_path, exclude_binaries=())
    if classify_codex_cli_failure(first) == "codex_cli_incompatible":
        store.record_step(issue_key, "toolchain_recovery", "warning", codex_recovery_payload(first, None))
        retry = run_codex_review_attempt(
            store,
            settings,
            issue_key,
            worktree_path,
            exclude_binaries=(str(first.get("codex_binary", "")),),
        )
        if classify_codex_cli_failure(retry) != "codex_cli_incompatible" and retry["status"] in {"success", "fallback"}:
            retry["toolchain_recovery"] = codex_recovery_payload(first, retry)
            retry["summary"] = retry.get("summary") or "Codex CLI recovered and completed review."
            store.record_step(issue_key, "toolchain_recovery", "success", retry["toolchain_recovery"])
            return retry
        retry["status"] = "blocked"
        retry["toolchain_recovery"] = codex_recovery_payload(first, retry)
        retry["summary"] = codex_recovery_blocker_summary(retry["toolchain_recovery"])
        store.record_step(issue_key, "toolchain_recovery", "failed", retry["toolchain_recovery"])
        return retry
    return first


def run_codex_review_attempt(
    store: OrchestratorStore,
    settings: WorkerSettings,
    issue_key: str,
    worktree_path: Path,
    *,
    exclude_binaries: tuple[str, ...],
) -> dict[str, Any]:
    try:
        argv = codex_exec_argv(settings, exclude_binaries=exclude_binaries)
    except OrchestratorError as exc:
        return {
            "status": "blocked",
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "summary": f"Codex CLI unavailable: {exc}",
            "changed_files": git_changed_files(worktree_path),
            "timed_out": False,
            "fallback_used": False,
            "codex_binary": "",
            "codex_version": "",
        }
    argv.extend(
        [
            "review",
            "--json",
            "--base",
            "main",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
    )
    process = start_process(argv, cwd=worktree_path)
    store.update_job(issue_key, active_pid=process.pid)
    try:
        stdout, stderr, timed_out = communicate_or_terminate(
            process,
            timeout_seconds=int(getattr(settings, "codex_review_timeout_seconds", DEFAULT_LOCAL_REVIEW_TIMEOUT_SECONDS)),
        )
    finally:
        store.update_job(issue_key, active_pid=None)
    changed_files = git_changed_files(worktree_path)
    if timed_out:
        summary = build_review_timeout_summary(
            worktree_path,
            changed_files,
            timeout_seconds=int(getattr(settings, "codex_review_timeout_seconds", DEFAULT_LOCAL_REVIEW_TIMEOUT_SECONDS)),
        )
        return {
            "status": "fallback",
            "exit_code": 124,
            "stdout": stdout,
            "stderr": stderr,
            "summary": summary,
            "changed_files": changed_files,
            "timed_out": True,
            "fallback_used": True,
            "codex_binary": argv[0],
            "codex_version": inspect_codex_binary(argv[0]).get("version", ""),
        }
    summary = summarize_codex_jsonl(stdout)
    return {
        "status": "success" if process.returncode == 0 else "failed",
        "exit_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "summary": summary,
        "changed_files": changed_files,
        "fallback_used": False,
        "timed_out": False,
        "codex_binary": argv[0],
        "codex_version": inspect_codex_binary(argv[0]).get("version", ""),
    }


def codex_exec_argv(settings: WorkerSettings, *, exclude_binaries: tuple[str, ...] = ()) -> list[str]:
    toolchain = resolve_codex_toolchain(
        configured_binary=str(getattr(settings, "codex_binary", DEFAULT_CODEX_BINARY)),
        write=True,
        require=True,
        exclude_binaries=exclude_binaries,
    )
    argv = [str(toolchain["binary"]), "exec"]
    if settings.codex_ignore_user_config:
        argv.append("--ignore-user-config")
    if settings.codex_model:
        argv.extend(["--model", settings.codex_model])
    return argv


def classify_codex_cli_failure(payload: dict[str, Any]) -> str:
    text = "\n".join(
        str(payload.get(key, ""))
        for key in ("stderr", "stdout", "summary")
        if payload.get(key)
    ).lower()
    if any(pattern in text for pattern in CODEX_CLI_INCOMPATIBLE_PATTERNS):
        return "codex_cli_incompatible"
    return ""


def codex_recovery_payload(first: dict[str, Any], retry: dict[str, Any] | None) -> dict[str, Any]:
    previous_binary = str(first.get("codex_binary", ""))
    recovered_binary = str((retry or {}).get("codex_binary", ""))
    recovered_version = str((retry or {}).get("codex_version", ""))
    payload = {
        "classification": "codex_cli_incompatible",
        "previous_binary": previous_binary,
        "previous_version": str(first.get("codex_version", "")),
        "recovered_binary": recovered_binary,
        "recovered_version": recovered_version,
        "summary": "Codex CLI compatibility issue detected.",
    }
    if recovered_binary:
        payload["summary"] = f"Codex CLI recovered: {previous_binary} -> {recovered_binary}"
    return payload


def codex_recovery_blocker_summary(payload: dict[str, Any]) -> str:
    return (
        "Codex CLI compatibility recovery failed. "
        f"Previous binary: {payload.get('previous_binary') or 'unknown'}. "
        f"Retry binary: {payload.get('recovered_binary') or 'none'}. "
        "Run `platform toolchain doctor` and `platform toolchain pin-codex --binary <compatible-codex>`."
    )


def ensure_pull_request(
    *,
    settings: WorkerSettings,
    worktree_path: Path,
    issue_key: str,
    branch: str,
    summary: str,
    project_key: str,
) -> dict[str, Any]:
    if git_has_meaningful_changes(worktree_path):
        stage_meaningful_changes(worktree_path)
        run_command(["git", "commit", "-m", f"{issue_key}: {summary}"], cwd=worktree_path)
    run_command(["git", "push", "-u", "origin", branch], cwd=worktree_path)

    existing = github_pull_request_status(worktree_path, branch, "")
    if existing:
        return existing

    spec_path = f"docs/specs/{issue_key}.md"
    body = "\n".join(
        [
            f"Issue key: {issue_key}",
            f"Spec: {spec_path}",
            "Risk class: pending",
            "Breaking change: pending",
            "Rollout strategy: see spec",
            "Rollback summary: see spec",
        ]
    )
    result = run_command(
        [
            "gh",
            "pr",
            "create",
            "--title",
            f"{issue_key}: {summary}",
            "--body",
            body,
            "--base",
            "main",
            "--head",
            branch,
        ],
        cwd=worktree_path,
    )
    pr = github_pull_request_status(worktree_path, branch, "")
    return pr


def github_pull_request_status(worktree_path: Path, branch: str, pr_number: str) -> dict[str, Any]:
    argv = [
        "gh",
        "pr",
        "view",
        pr_number or branch,
        "--json",
        "number,url,state,mergeStateStatus,isDraft,reviews,statusCheckRollup,headRefName,headRefOid,reviewDecision,comments",
    ]
    result = run_optional_with_retry(argv, cwd=worktree_path)
    if not result or result.returncode != 0 or not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def summarize_reviews(
    reviews: list[dict[str, Any]],
    codex_review_authors: tuple[str, ...],
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_authors = {item.lower() for item in codex_review_authors} | {
        item.lower() for item in DEFAULT_CODEX_REVIEW_AUTHORS
    }
    codex_reviews = [
        review
        for review in reviews
        if str(review.get("author", {}).get("login", "")).lower() in normalized_authors
    ]
    codex_comments = [
        comment
        for comment in (comments or [])
        if is_codex_review_comment(comment, normalized_authors)
    ]
    if codex_comments:
        latest = codex_comments[-1]
        body = str(latest.get("body", "")).lower()
        no_major_issues = (
            "didn't find any major issues" in body
            or "did not find any major issues" in body
            or "no major issues" in body
        )
        return {
            "reviewed": True,
            "approved": no_major_issues,
            "changes_requested": not no_major_issues,
            "summary": "Codex review comment: no major issues."
            if no_major_issues
            else "Codex review comment returned feedback.",
        }
    if not codex_reviews:
        return {
            "reviewed": False,
            "approved": False,
            "changes_requested": False,
            "summary": "Codex review has not arrived yet.",
        }
    states = sorted(
        {
            str(review.get("state", "")).lower()
            for review in codex_reviews
            if review.get("state")
        }
    )
    approved = any(str(review.get("state")) == "APPROVED" for review in codex_reviews)
    changes_requested = any(
        str(review.get("state")) == "CHANGES_REQUESTED" for review in codex_reviews
    )
    summary = f"Codex reviews: {', '.join(states) if states else 'submitted'}"
    return {
        "reviewed": True,
        "approved": approved,
        "changes_requested": changes_requested,
        "summary": summary,
    }


def is_codex_review_comment(comment: dict[str, Any], authors: set[str]) -> bool:
    login = str(comment.get("author", {}).get("login", "")).lower()
    body = str(comment.get("body", "")).lower()
    return login in authors and "codex review:" in body


def summarize_checks(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"passed": False, "failed": False, "summary": "Checks have not reported yet.", "failed_items": []}
    conclusions: list[str] = []
    failed_items: list[str] = []
    for item in items:
        conclusion = str(item.get("conclusion") or item.get("state") or "").lower()
        if conclusion:
            conclusions.append(conclusion)
        if conclusion in {"failure", "timed_out", "cancelled", "startup_failure"}:
            name = str(item.get("name") or item.get("context") or item.get("workflowName") or item.get("__typename") or "unknown")
            failed_items.append(name)
    failed = any(value in {"failure", "timed_out", "cancelled", "startup_failure"} for value in conclusions)
    passed = all(value in {"success", "neutral", "skipped"} for value in conclusions) and bool(conclusions)
    return {
        "passed": passed,
        "failed": failed,
        "failed_items": failed_items,
        "summary": f"Checks: {', '.join(conclusions) if conclusions else 'pending'}"
        + (f" | failed: {', '.join(failed_items)}" if failed_items else ""),
    }


def classify_gate_result(
    *,
    check_summary: dict[str, Any] | None = None,
    review_summary: dict[str, Any] | None = None,
    pr: dict[str, Any] | None = None,
    latest_error: str = "",
) -> dict[str, str]:
    check_summary = check_summary or {}
    review_summary = review_summary or {}
    text = " ".join(
        [
            str(check_summary.get("summary", "")),
            " ".join(str(item) for item in check_summary.get("failed_items", []) or []),
            str(review_summary.get("summary", "")),
            str((pr or {}).get("mergeStateStatus", "")),
            latest_error,
        ]
    ).lower()
    if "changes_requested" in text or "changes requested" in text or review_summary.get("changes_requested"):
        return {
            "classification": "review_changes_requested",
            "state": "gate_waiting_human",
            "reason": str(review_summary.get("summary") or "Codex review requested changes."),
        }
    if any(fragment in text for fragment in ("manual", "approval", "risk", "ai-gate", "release-ready")):
        return {
            "classification": "intentional_gate",
            "state": "gate_waiting_human",
            "reason": str(check_summary.get("summary") or latest_error or "Intentional quality gate is waiting for human approval."),
        }
    if any(fragment in text for fragment in ("security", "secret", "vulnerability", "dependency-review")):
        return {
            "classification": "security_failure",
            "state": "gate_failed",
            "reason": str(check_summary.get("summary") or latest_error or "Security gate failed."),
        }
    if any(fragment in text for fragment in ("merge queue", "merge_group", "removed from merge")):
        return {
            "classification": "merge_queue_removed",
            "state": "gate_failed",
            "reason": latest_error or "PR was removed from merge queue or merge_group validation failed.",
        }
    if check_summary.get("failed"):
        return {
            "classification": "validation_failure",
            "state": "gate_failed",
            "reason": str(check_summary.get("summary") or latest_error or "Required check failed."),
        }
    return {
        "classification": "unknown",
        "state": "blocked",
        "reason": latest_error or "Unknown gate result.",
    }


def build_summary_comment(
    *,
    job: dict[str, Any],
    checks_summary: str,
    review_summary: str,
    fallback_summary: str,
    extra_notice: str,
) -> str:
    blocked_dependencies = parse_blocked_dependencies(job.get("blocked_dependencies_json"))
    gate_state = job.get("gate_state") or inferred_gate_state(job) or "none"
    gate_reason = job.get("gate_reason") or ("; ".join(f"{item.get('issue_key')}={item.get('state')}" for item in blocked_dependencies) if blocked_dependencies else "")
    body = [
        SUMMARY_MARKER,
        f"- state: `{job['state']}`",
        f"- gate_state: `{gate_state}`",
        f"- gate_reason: {gate_reason or 'none'}",
        f"- blocked_dependencies: {', '.join(item.get('issue_key', '') for item in blocked_dependencies if item.get('issue_key')) or 'none'}",
        f"- next_operator_action: {next_operator_action(job, blocked_dependencies)}",
        f"- branch: `{job.get('branch') or 'n/a'}`",
        f"- latest commit: `{job.get('latest_commit') or 'n/a'}`",
        f"- PR URL: {job.get('pr_url') or 'n/a'}",
        f"- checks: {checks_summary or 'pending'}",
        f"- review: {review_summary or 'pending'}",
        f"- fallback: {fallback_summary or 'none'}",
        f"- blocker: {job.get('latest_error') or 'none'}",
        f"- updated: {now_iso()}",
    ]
    if extra_notice:
        body.append(f"- note: {extra_notice}")
    return "\n".join(body)


def latest_step_summary(store: OrchestratorStore, issue_key: str, step_name: str) -> str:
    step = store.latest_step(issue_key, step_name)
    if not step:
        return ""
    try:
        payload = json.loads(step["payload_json"])
    except json.JSONDecodeError:
        payload = {}
    return str(payload.get("summary", ""))


def latest_fallback_summary(store: OrchestratorStore, issue_key: str) -> str:
    summaries: list[str] = []
    for step_name in ("coding", "reviewing"):
        step = store.latest_step(issue_key, step_name)
        if not step:
            continue
        try:
            payload = json.loads(step["payload_json"])
        except json.JSONDecodeError:
            continue
        if payload.get("fallback_used") or payload.get("timed_out") or step.get("status") == "fallback":
            summary = str(payload.get("summary", "")).strip()
            label = "Codex exec" if step_name == "coding" else "Codex local review"
            summaries.append(f"{label}: {summary or 'fallback path used'}")
    return " | ".join(summaries)


def upsert_summary_comment(
    settings: WorkerSettings, issue_key: str, body: str, existing_comment_id: str
) -> str:
    adf_body = jira_adf_from_text(body)
    if existing_comment_id:
        payload = jira_request(
            settings=settings,
            method="PUT",
            path=f"/rest/api/3/issue/{issue_key}/comment/{existing_comment_id}",
            payload={"body": adf_body},
        )
        return str(payload.get("id", existing_comment_id)) if isinstance(payload, dict) else existing_comment_id
    comments = jira_request(
        settings=settings,
        method="GET",
        path=f"/rest/api/3/issue/{issue_key}/comment",
    )
    values = comments.get("comments", []) if isinstance(comments, dict) else []
    for comment in values:
        rendered = extract_jira_text(comment.get("body"))
        if SUMMARY_MARKER in rendered:
            comment_id = str(comment.get("id", ""))
            jira_request(
                settings=settings,
                method="PUT",
                path=f"/rest/api/3/issue/{issue_key}/comment/{comment_id}",
                payload={"body": adf_body},
            )
            return comment_id
    created = jira_request(
        settings=settings,
        method="POST",
        path=f"/rest/api/3/issue/{issue_key}/comment",
        payload={"body": adf_body},
    )
    return str(created.get("id", "")) if isinstance(created, dict) else ""


def jira_adf_from_text(body: str) -> dict[str, Any]:
    paragraphs: list[dict[str, Any]] = []
    for line in body.splitlines():
        text = line if line else " "
        paragraphs.append(
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                    }
                ],
            }
        )
    if not paragraphs:
        paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": " "}]}]
    return {
        "type": "doc",
        "version": 1,
        "content": paragraphs,
    }


def parse_control_command(text: str) -> str:
    match = CONTROL_COMMAND_RE.search(text.strip())
    return match.group(1).lower() if match else ""


def normalize_issue_key(value: Any) -> str:
    text = str(value or "").upper().strip()
    return text if ISSUE_RE.fullmatch(text) else ""


def extract_jira_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        chunks: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "text" and node.get("text"):
                    chunks.append(str(node["text"]))
                for child in node.get("content", []):
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(value)
        return "\n".join(part for part in chunks if part).strip()
    return ""


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OrchestratorError(f"Manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OrchestratorError(f"Manifest must be JSON-compatible YAML: {path} ({exc})") from exc


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "change"


def issue_state_dir(project_key: str, issue_key: str) -> Path:
    path = default_state_dir() / "jobs" / project_key / issue_key
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def seconds_since(value: str) -> int:
    if not value:
        return 0
    try:
        started_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int((datetime.now(timezone.utc) - started_at).total_seconds())


def run_optional(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except (FileNotFoundError, PermissionError, OSError):
        return None


def run_optional_with_retry(
    argv: list[str],
    *,
    cwd: Path | None = None,
    attempts: int = DEFAULT_API_RETRY_ATTEMPTS,
) -> subprocess.CompletedProcess[str] | None:
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(attempts):
        result = run_optional(argv, cwd=cwd)
        if not result or result.returncode == 0:
            return result
        output = f"{result.stdout}\n{result.stderr}".lower()
        if not is_transient_cli_output(output) or attempt == attempts - 1:
            return result
        time.sleep(api_retry_delay(attempt))
    return result


def is_transient_cli_output(output: str) -> bool:
    fragments = (
        "could not resolve host",
        "nodename nor servname",
        "temporary failure",
        "connection reset",
        "connection refused",
        "timed out",
        "timeout",
        "rate limit",
        "secondary rate limit",
        "502",
        "503",
        "504",
    )
    return any(fragment in output for fragment in fragments)


def api_retry_delay(attempt: int) -> float:
    return min(8.0, 0.5 * (2**attempt)) + (0.1 * attempt)


def run_command(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        message = [f"Command failed: {' '.join(argv)}"]
        if result.stdout.strip():
            message.append(result.stdout.strip())
        if result.stderr.strip():
            message.append(result.stderr.strip())
        raise OrchestratorError("\n".join(message))
    return result


def run_tracked_command(
    store: OrchestratorStore,
    issue_key: str,
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str]:
    process = start_process(argv, cwd=cwd)
    store.update_job(issue_key, active_pid=process.pid)
    timed_out = False
    try:
        if timeout_seconds is None:
            stdout, stderr = process.communicate()
        else:
            stdout, stderr, timed_out = communicate_or_terminate(
                process,
                timeout_seconds=timeout_seconds,
            )
    finally:
        store.update_job(issue_key, active_pid=None)
    result = subprocess.CompletedProcess(argv, 124 if timed_out else process.returncode, stdout, stderr)
    if result.returncode != 0:
        message = [
            f"Command timed out after {timeout_seconds}s: {' '.join(argv)}"
            if timed_out
            else f"Command failed: {' '.join(argv)}"
        ]
        if result.stdout.strip():
            message.append(result.stdout.strip())
        if result.stderr.strip():
            message.append(result.stderr.strip())
        raise OrchestratorError("\n".join(message))
    return result


def start_process(argv: list[str], *, cwd: Path | None = None) -> subprocess.Popen[str]:
    try:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise OrchestratorError(f"Failed to start process: {' '.join(argv)} ({exc})") from exc


def communicate_or_terminate(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: int,
) -> tuple[str, str, bool]:
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return stdout, stderr, False
    except subprocess.TimeoutExpired:
        terminate_process_group(process, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_process_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return stdout, stderr, True


def terminate_process_group(process: subprocess.Popen[str], signum: signal.Signals) -> None:
    if process.poll() is not None:
        return
    signal_pid_group(process.pid, signum)


def signal_pid_group(pid: int, signum: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signum)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signum)


def status_from_process(returncode: int | None, *, timed_out: bool, has_fallback: bool) -> str:
    if timed_out and has_fallback:
        return "partial"
    if returncode == 0:
        return "success"
    return "failed"


def git_status_entries(worktree_path: Path) -> list[tuple[str, str]]:
    result = run_command(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=worktree_path,
    )
    entries: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if not path:
            continue
        entries.append((status, path))
    return entries


def is_meaningful_changed_path(path: str) -> bool:
    normalized = path.strip().replace("\\", "/")
    if not normalized:
        return False
    if normalized in IGNORED_WORKTREE_PATHS:
        return False
    return not any(normalized.startswith(prefix) for prefix in IGNORED_WORKTREE_PREFIXES)


def git_has_meaningful_changes(worktree_path: Path) -> bool:
    return any(is_meaningful_changed_path(path) for _, path in git_status_entries(worktree_path))


def git_changed_files(worktree_path: Path) -> list[str]:
    return [path for _, path in git_status_entries(worktree_path) if is_meaningful_changed_path(path)]


def stage_meaningful_changes(worktree_path: Path) -> list[str]:
    files = git_changed_files(worktree_path)
    if not files:
        return []
    run_command(["git", "add", "-A", "--", *files], cwd=worktree_path)
    return files


def git_cached_diff_stat(worktree_path: Path) -> str:
    result = run_optional(["git", "diff", "--cached", "--stat"], cwd=worktree_path)
    if not result or result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_worktree_diff_stat(worktree_path: Path) -> str:
    result = run_optional(["git", "diff", "--stat"], cwd=worktree_path)
    if not result or result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_head(worktree_path: Path) -> str:
    result = run_command(["git", "rev-parse", "HEAD"], cwd=worktree_path)
    return result.stdout.strip()


def request_codex_review(worktree_path: Path, pr_url: str, *, existing_timestamp: str) -> str:
    if existing_timestamp:
        return existing_timestamp
    with contextlib.suppress(OrchestratorError):
        run_command(["gh", "pr", "comment", pr_url, "--body", "@codex review"], cwd=worktree_path)
        return now_iso()
    return existing_timestamp


def ensure_github_auto_merge(worktree_path: Path, pr_url: str) -> dict[str, Any]:
    if not pr_url:
        return {"enabled": False, "warning": "PR URL is missing; cannot enable GitHub merge queue/auto-merge."}
    result = run_optional(["gh", "pr", "merge", pr_url, "--auto", "--merge"], cwd=worktree_path)
    if not result:
        return {"enabled": False, "warning": "gh is unavailable; cannot enable GitHub merge queue/auto-merge."}
    output = "\n".join(item for item in (result.stdout.strip(), result.stderr.strip()) if item)
    if result.returncode != 0:
        benign = ("already enabled", "already in queue", "pull request is already merged")
        if any(fragment in output.lower() for fragment in benign):
            return {"enabled": True, "summary": output or "GitHub merge queue/auto-merge already enabled."}
        return {"enabled": False, "warning": output or "Could not enable GitHub merge queue/auto-merge."}
    return {"enabled": True, "summary": output or "GitHub merge queue/auto-merge enabled."}


def summarize_codex_jsonl(output: str) -> str:
    summary = ""
    for line in output.splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(line)
            if payload.get("type") == "item.completed":
                item = payload.get("item", {})
                if item.get("type") == "agent_message":
                    summary = str(item.get("text", "")).strip()
    return summary


def build_review_timeout_summary(
    worktree_path: Path,
    changed_files: list[str],
    *,
    timeout_seconds: int = DEFAULT_LOCAL_REVIEW_TIMEOUT_SECONDS,
) -> str:
    diff_stat = git_cached_diff_stat(worktree_path)
    changed_text = ", ".join(changed_files) if changed_files else "no meaningful files detected"
    if diff_stat:
        return (
            f"Codex local review timed out after {timeout_seconds}s. "
            f"Fallback review summary based on staged diff. Changed files: {changed_text}. "
            f"Diff stat: {diff_stat}"
        )
    return (
        f"Codex local review timed out after {timeout_seconds}s. "
        f"Fallback review summary based on staged diff. Changed files: {changed_text}."
    )


def build_exec_timeout_summary(
    worktree_path: Path,
    changed_files: list[str],
    *,
    timeout_seconds: int = DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS,
) -> str:
    diff_stat = git_worktree_diff_stat(worktree_path)
    changed_text = ", ".join(changed_files) if changed_files else "no meaningful files detected"
    if diff_stat:
        return (
            f"Codex implementation timed out after {timeout_seconds}s. "
            f"Fallback execution summary based on current worktree diff. Changed files: {changed_text}. "
            f"Diff stat: {diff_stat}"
        )
    return (
        f"Codex implementation timed out after {timeout_seconds}s. "
        f"Fallback execution summary based on current worktree diff. Changed files: {changed_text}."
    )


def signal_active_process(active_pid: Any) -> None:
    try:
        pid = int(active_pid)
    except (TypeError, ValueError):
        return
    for signum in (signal.SIGTERM, signal.SIGKILL):
        signal_pid_group(pid, signum)
        time.sleep(1)
