#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
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
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 300
DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS = 120
DEFAULT_LOCAL_REVIEW_TIMEOUT_SECONDS = 60
DEFAULT_CODEX_MODEL = ""
DEFAULT_CODEX_IGNORE_USER_CONFIG = True
DEFAULT_CLAUDE_MODEL = "default"
DEFAULT_CLAUDE_EFFORT = ""
ATLASSIAN_TOKEN_KEYCHAIN_SERVICE = "ai-dev-platform.atlassian-api-token"
IGNORED_WORKTREE_PREFIXES = (".tmp/",)
IGNORED_WORKTREE_PATHS = {
    ".platform/.last-validation.json",
}
ORCHESTRATOR_CONFIG_FILENAME = "orchestrator.json"
PLATFORM_CONFIG_FILENAME = "config.json"
CONFIG_DIRNAME = "ai-dev-platform"
DB_FILENAME = "orchestrator.db"
SUMMARY_MARKER = "<!-- platform-orchestrator:summary -->"
START_LABEL = "ai:auto"
CONTROL_LABEL = "ai:control"
WORKTREE_ROOTNAME = "worktrees"
ISSUE_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")
CONTROL_COMMAND_RE = re.compile(
    r"^/ai\s+(pause|resume|cancel|retry|status|pause-project|resume-project|drain-project)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
EVENT_PATH_RE = re.compile(r"^/jira/events/(?P<project_key>[A-Za-z][A-Za-z0-9]+)$")
RUNNABLE_STATES = {
    "queued",
    "planning",
    "coding",
    "reviewing",
    "pr_open",
    "paused",
    "blocked",
    "failed",
}
WAITING_STATES = {"waiting_checks", "waiting_review", "ready_for_merge"}
TERMINAL_STATES = {"ready_for_merge", "done", "cancelled"}


class OrchestratorError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepoProject:
    project_key: str
    repo_path: Path
    repo_name: str
    confluence_space: str
    codex_review_mode: str
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
    codex_model: str
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
        or args.codex_use_user_config
        or args.codex_ignore_user_config
        or args.claude_model is not None
        or args.claude_effort is not None
    ):
        ai_config = dict(config.get("ai", {}))
        if args.codex_model is not None:
            ai_config["codex_model"] = args.codex_model.strip()
        if args.codex_use_user_config:
            ai_config["codex_ignore_user_config"] = False
        if args.codex_ignore_user_config:
            ai_config["codex_ignore_user_config"] = True
        if args.claude_model is not None:
            ai_config["claude_model"] = args.claude_model.strip()
        if args.claude_effort is not None:
            ai_config["claude_effort"] = args.claude_effort.strip()
        config["ai"] = ai_config
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
    payload = {
        "jobs": rows,
        "control_flags": flags,
        "hints": status_hints(rows, refreshed=args.refresh),
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


def status_hints(rows: list[dict[str, Any]], *, refreshed: bool) -> list[str]:
    if refreshed:
        return []
    if any(str(row.get("state", "")) in WAITING_STATES for row in rows):
        return [
            "Waiting job state may be stale if the worker is not running. "
            "Run `platform orchestrator poll` or `platform orchestrator status --refresh` to refresh GitHub checks/reviews and Jira reporting."
        ]
    return []


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
            if job and job["state"] in {"failed", "blocked"}:
                self.store.update_job(issue_key, state="queued", latest_error="", requested_action="")
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
            with self.thread_lock:
                if issue_key in self.active_threads:
                    continue
            if not self.store.acquire_lease(repo_path, issue_key):
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
            self.store.update_job(issue_key, state="failed", latest_error=str(exc), active_pid=None)
            self.refresh_issue_report(issue_key, extra_notice=f"Failed: {exc}")
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
                self.store.update_job(
                    issue_key,
                    state="failed",
                    latest_error=exec_result.get("summary", "Codex exec failed"),
                )
                self.refresh_issue_report(issue_key)
                return
            if not exec_result.get("changed_files"):
                self.store.update_job(
                    issue_key,
                    state="blocked",
                    latest_error="Codex finished without producing a diff.",
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
            elif check_summary["failed"]:
                state = "blocked"
                updates["latest_error"] = check_summary["summary"]
            elif state == "waiting_checks" and check_summary["passed"]:
                state = "waiting_review"
                updates["review_requested_at"] = str(job.get("review_requested_at") or now_iso())
                if codex_review_mode == "comment_fallback":
                    updates["review_fallback_requested_at"] = request_codex_review(
                        worktree_path,
                        pr["url"],
                        existing_timestamp=str(job.get("review_fallback_requested_at", "")),
                    )
            if state == "waiting_review":
                job_snapshot = {**job, **updates}
                state, extra_updates = self.resolve_review_state(
                    job=job_snapshot,
                    worktree_path=worktree_path,
                    pr=pr,
                    review_summary=review_summary,
                    codex_review_mode=codex_review_mode,
                )
                updates.update(extra_updates)
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
            return (
                "blocked",
                {"latest_error": review_summary["summary"]},
            )
        if review_summary["reviewed"]:
            return ("ready_for_merge", updates)

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
                    "blocked",
                    {
                        **updates,
                        "latest_error": "Codex review did not arrive after the fallback review request.",
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
                "blocked",
                {
                    **updates,
                    "latest_error": "Codex review did not arrive after the fallback review request.",
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
        comment_body = build_summary_comment(
            job=job,
            checks_summary=checks_summary or latest_step_summary(self.store, issue_key, "waiting_checks"),
            review_summary=review_summary or latest_step_summary(self.store, issue_key, "reviewing"),
            fallback_summary=latest_fallback_summary(self.store, issue_key),
            extra_notice=extra_notice,
        )
        comment_id = upsert_summary_comment(self.settings, issue_key, comment_body, self.store.report_comment_id(issue_key))
        self.store.upsert_report(issue_key, comment_id, comment_body)


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
                    status_name TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    branch TEXT DEFAULT '',
                    worktree_path TEXT DEFAULT '',
                    pr_url TEXT DEFAULT '',
                    pr_number TEXT DEFAULT '',
                    latest_commit TEXT DEFAULT '',
                    latest_error TEXT DEFAULT '',
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
                        manifest_path, source_repo, workflow_ref, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_key) DO UPDATE SET
                        repo_path=excluded.repo_path,
                        repo_name=excluded.repo_name,
                        confluence_space=excluded.confluence_space,
                        codex_review_mode=excluded.codex_review_mode,
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
                WHERE state = 'queued'
                ORDER BY created_at ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def list_waiting_jobs(
        self, *, issue_key: str | None = None, project_key: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["state IN ('waiting_checks', 'waiting_review', 'ready_for_merge')"]
        params: list[Any] = []
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

    def acquire_lease(self, repo_path: str, issue_key: str) -> bool:
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

    def clear_all_leases(self) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM leases")

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

    state_dir = default_state_dir()
    configured_codex_review_authors = tuple(
        str(item).strip().lower()
        for item in config.get("github", {}).get("codex_review_authors", DEFAULT_CODEX_REVIEW_AUTHORS)
        if str(item).strip()
    )
    codex_review_authors = tuple(
        dict.fromkeys([*configured_codex_review_authors, *DEFAULT_CODEX_REVIEW_AUTHORS])
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
        codex_model=str(config.get("ai", {}).get("codex_model", DEFAULT_CODEX_MODEL)).strip(),
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
                    if key not in {"poll_intervals", "github", "ai", "listen_url", "shared_secret"}
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
        },
        "ai": {
            "codex_model": DEFAULT_CODEX_MODEL,
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


def default_state_dir() -> Path:
    base_dir = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))).expanduser()
    path = base_dir / CONFIG_DIRNAME / "orchestrator"
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    if shutil.which("codex") is None:
        missing.append("codex")
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
        manifest_path=manifest_path,
        source_repo=str(manifest["integrations"]["github"]["source_repo"]),
        workflow_ref=str(manifest["integrations"]["github"]["workflow_ref"]),
    )


def issue_is_auto_ready(issue: dict[str, Any]) -> bool:
    fields = issue.get("fields", {})
    labels = {str(label).lower() for label in fields.get("labels", [])}
    status = str(fields.get("status", {}).get("name", ""))
    return START_LABEL.lower() in labels and status in DEFAULT_READY_STATUSES


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
    try:
        with request.urlopen(req) as response:
            content = response.read().decode("utf-8")
            if not content:
                return {}
            return json.loads(content)
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise OrchestratorError(f"Jira request failed ({method} {path}): {exc.code} {body}") from exc


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
        timeout_seconds=DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    )
    payload = json.loads(result.stdout)
    structured = payload.get("structured_output")
    if not isinstance(structured, dict):
        raise OrchestratorError("Claude did not return structured_output.")
    return structured


def run_codex_exec(
    store: OrchestratorStore,
    settings: WorkerSettings,
    worktree_path: Path,
    project_key: str,
    issue_key: str,
    branch: str,
    plan_payload: dict[str, Any],
) -> dict[str, Any]:
    schema = REPO_ROOT / "schemas" / "orchestrator" / "codex-exec.json"
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
""".strip()
    argv = codex_exec_argv(settings)
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
            timeout_seconds=DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS,
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
    }
    if output_file.exists():
        try:
            payload.update(json.loads(output_file.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            payload["summary"] = output_file.read_text(encoding="utf-8").strip()
    else:
        payload["summary"] = summarize_codex_jsonl(stdout)
    if timed_out and not payload.get("summary"):
        payload["summary"] = build_exec_timeout_summary(worktree_path, changed_files)
    if timed_out:
        payload["status"] = status_from_process(process.returncode, timed_out=True, has_fallback=bool(changed_files))
        payload["exit_code"] = 124
        payload["validation_passed"] = False
        payload["fallback_used"] = bool(changed_files)
        payload["timed_out"] = True
    return payload

def run_codex_review(
    store: OrchestratorStore,
    settings: WorkerSettings,
    issue_key: str,
    worktree_path: Path,
) -> dict[str, Any]:
    stage_meaningful_changes(worktree_path)
    argv = codex_exec_argv(settings)
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
            timeout_seconds=DEFAULT_LOCAL_REVIEW_TIMEOUT_SECONDS,
        )
    finally:
        store.update_job(issue_key, active_pid=None)
    changed_files = git_changed_files(worktree_path)
    if timed_out:
        summary = build_review_timeout_summary(worktree_path, changed_files)
        return {
            "status": "fallback",
            "exit_code": 124,
            "stdout": stdout,
            "stderr": stderr,
            "summary": summary,
            "changed_files": changed_files,
            "timed_out": True,
            "fallback_used": True,
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
    }


def codex_exec_argv(settings: WorkerSettings) -> list[str]:
    argv = ["codex", "exec"]
    if settings.codex_ignore_user_config:
        argv.append("--ignore-user-config")
    if settings.codex_model:
        argv.extend(["--model", settings.codex_model])
    return argv


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
    result = run_optional(argv, cwd=worktree_path)
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
        return {"passed": False, "failed": False, "summary": "Checks have not reported yet."}
    conclusions: list[str] = []
    for item in items:
        conclusion = str(item.get("conclusion") or item.get("state") or "").lower()
        if conclusion:
            conclusions.append(conclusion)
    failed = any(value in {"failure", "timed_out", "cancelled", "startup_failure"} for value in conclusions)
    passed = all(value in {"success", "neutral", "skipped"} for value in conclusions) and bool(conclusions)
    return {
        "passed": passed,
        "failed": failed,
        "summary": f"Checks: {', '.join(conclusions) if conclusions else 'pending'}",
    }


def build_summary_comment(
    *,
    job: dict[str, Any],
    checks_summary: str,
    review_summary: str,
    fallback_summary: str,
    extra_notice: str,
) -> str:
    body = [
        SUMMARY_MARKER,
        f"- state: `{job['state']}`",
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
        return "fallback"
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


def build_review_timeout_summary(worktree_path: Path, changed_files: list[str]) -> str:
    diff_stat = git_cached_diff_stat(worktree_path)
    changed_text = ", ".join(changed_files) if changed_files else "no meaningful files detected"
    if diff_stat:
        return (
            f"Codex local review timed out after {DEFAULT_LOCAL_REVIEW_TIMEOUT_SECONDS}s. "
            f"Fallback review summary based on staged diff. Changed files: {changed_text}. "
            f"Diff stat: {diff_stat}"
        )
    return (
        f"Codex local review timed out after {DEFAULT_LOCAL_REVIEW_TIMEOUT_SECONDS}s. "
        f"Fallback review summary based on staged diff. Changed files: {changed_text}."
    )


def build_exec_timeout_summary(worktree_path: Path, changed_files: list[str]) -> str:
    diff_stat = git_worktree_diff_stat(worktree_path)
    changed_text = ", ".join(changed_files) if changed_files else "no meaningful files detected"
    if diff_stat:
        return (
            f"Codex implementation timed out after {DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS}s. "
            f"Fallback execution summary based on current worktree diff. Changed files: {changed_text}. "
            f"Diff stat: {diff_stat}"
        )
    return (
        f"Codex implementation timed out after {DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS}s. "
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
