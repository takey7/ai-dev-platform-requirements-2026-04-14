from __future__ import annotations

import importlib.util
import http.client
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "platform_orchestrator.py"
SPEC = importlib.util.spec_from_file_location("platform_orchestrator", MODULE_PATH)
orchestrator = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = orchestrator
SPEC.loader.exec_module(orchestrator)


class PlatformOrchestratorTests(unittest.TestCase):
    def test_parse_control_command(self) -> None:
        self.assertEqual(orchestrator.parse_control_command("/ai pause"), "pause")
        self.assertEqual(orchestrator.parse_control_command("  /ai resume-project  "), "resume-project")
        self.assertEqual(orchestrator.parse_control_command("ship it"), "")

    def test_issue_is_auto_ready(self) -> None:
        issue = {
            "fields": {
                "labels": ["ai:auto", "backend"],
                "status": {"name": "To Do"},
            }
        }
        self.assertTrue(orchestrator.issue_is_auto_ready(issue))
        issue["fields"]["status"]["name"] = "Done"
        self.assertFalse(orchestrator.issue_is_auto_ready(issue))

    def test_discover_projects_scans_multiple_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "billing-api"
            manifest = repo / ".platform" / "platform.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                """
{
  "platform": {"version": "0.1.5", "adapter": "node-ts", "service_name": "billing-api"},
  "issue": {"project_key": "BILL"},
  "paths": {"spec_dir": "docs/specs", "spec_template": "docs/specs/ISSUE_SPEC_TEMPLATE.md"},
  "commands": {
    "install": "pnpm install",
    "lint": "pnpm lint",
    "typecheck": "pnpm typecheck",
    "test_unit": "pnpm test:unit",
    "test_integration": "pnpm test:integration",
    "build": "pnpm build"
  },
  "risk": {"protected_paths": ["api/public/**"]},
  "checks": {"enabled": ["ci"]},
  "deploy": {"mode": "staging-prod"},
  "integrations": {
    "atlassian": {
      "mcp_url": "https://mcp.atlassian.com/v1/mcp",
      "auth_mode": "oauth2.1",
      "project_scoped": true,
      "api_token_opt_in": false,
      "confluence_space": "ENG"
    },
    "github": {
      "source_repo": "takey7/platform",
      "workflow_ref": "v0.1.5",
      "template_repository": true,
      "codex_review": {"mode": "auto_required"}
    }
  }
}
                """.strip()
                + "\n",
                encoding="utf-8",
            )

            discovered = orchestrator.discover_projects((root,))

            self.assertEqual(len(discovered), 1)
            self.assertEqual(discovered[0].project_key, "BILL")
            self.assertEqual(discovered[0].repo_path, repo)
            self.assertEqual(discovered[0].codex_review_mode, "auto_required")

    def test_discover_projects_rejects_duplicate_project_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for repo_name in ("billing-api", "billing-worker"):
                manifest = root / repo_name / ".platform" / "platform.yaml"
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(
                    """
{
  "platform": {"version": "0.1.5", "adapter": "node-ts", "service_name": "billing-api"},
  "issue": {"project_key": "BILL"},
  "paths": {"spec_dir": "docs/specs", "spec_template": "docs/specs/ISSUE_SPEC_TEMPLATE.md"},
  "commands": {
    "install": "pnpm install",
    "lint": "pnpm lint",
    "typecheck": "pnpm typecheck",
    "test_unit": "pnpm test:unit",
    "test_integration": "pnpm test:integration",
    "build": "pnpm build"
  },
  "risk": {"protected_paths": ["api/public/**"]},
  "checks": {"enabled": ["ci"]},
  "deploy": {"mode": "staging-prod"},
  "integrations": {
    "atlassian": {
      "mcp_url": "https://mcp.atlassian.com/v1/mcp",
      "auth_mode": "oauth2.1",
      "project_scoped": true,
      "api_token_opt_in": false,
      "confluence_space": "ENG"
    },
    "github": {
      "source_repo": "takey7/platform",
      "workflow_ref": "v0.1.5",
      "template_repository": true,
      "codex_review": {"mode": "auto_required"}
    }
  }
}
                    """.strip()
                    + "\n",
                    encoding="utf-8",
                )

            with self.assertRaises(orchestrator.OrchestratorError):
                orchestrator.discover_projects((root,))

    def test_store_handles_pause_and_leases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = orchestrator.OrchestratorStore(Path(tmpdir) / "orchestrator.db")
            project = orchestrator.RepoProject(
                project_key="BILL",
                repo_path=Path(tmpdir) / "billing-api",
                repo_name="billing-api",
                confluence_space="ENG",
                codex_review_mode="auto_required",
                manifest_path=Path(tmpdir) / "billing-api" / ".platform" / "platform.yaml",
                source_repo="takey7/platform",
                workflow_ref="v0.1.5",
            )
            store.sync_projects([project])
            store.enqueue_issue(
                project_key="BILL",
                repo_path=str(project.repo_path),
                issue_key="BILL-1",
                status="To Do",
                summary="test orchestrator",
            )
            self.assertTrue(store.acquire_lease(str(project.repo_path), "BILL-1"))
            self.assertFalse(store.acquire_lease(str(project.repo_path), "BILL-2"))
            store.release_lease(str(project.repo_path), "BILL-1")
            store.set_control_flag("project", "BILL", "pause", "paused")
            self.assertEqual(store.control_flag_value("project", "BILL", "pause"), "paused")
            store.set_requested_action("BILL-1", "pause")
            self.assertEqual(store.get_job("BILL-1")["requested_action"], "pause")
            secret = store.ensure_project_webhook_secret("BILL")
            self.assertTrue(secret)
            self.assertEqual(store.project("BILL")["webhook_secret"], secret)
            store.acquire_lease(str(project.repo_path), "BILL-1")
            store.clear_all_leases()
            self.assertTrue(store.acquire_lease(str(project.repo_path), "BILL-2"))

    def test_healthz_supports_get(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = SimpleNamespace(
                bind_host="127.0.0.1",
                bind_port=0,
                projects_roots=(Path(tmpdir),),
            )
            store = orchestrator.OrchestratorStore(Path(tmpdir) / "orchestrator.db")
            service = orchestrator.OrchestratorService(settings=settings, store=store)
            server = service.start_http_server()
            try:
                host, port = server.server_address
                connection = http.client.HTTPConnection(host, port, timeout=5)
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["status"], "ok")
            finally:
                server.shutdown()
                server.server_close()

    def test_enqueue_issue_does_not_requeue_active_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = orchestrator.OrchestratorStore(Path(tmpdir) / "orchestrator.db")
            store.enqueue_issue(
                project_key="BILL",
                repo_path="/tmp/billing-api",
                issue_key="BILL-1",
                status="To Do",
                summary="first pass",
            )
            store.update_job("BILL-1", state="waiting_review", pr_url="https://example/pr/1")

            store.enqueue_issue(
                project_key="BILL",
                repo_path="/tmp/billing-api",
                issue_key="BILL-1",
                status="To Do",
                summary="updated summary",
            )

            job = store.get_job("BILL-1")
            self.assertEqual(job["state"], "waiting_review")
            self.assertEqual(job["pr_url"], "https://example/pr/1")
            self.assertEqual(job["summary"], "updated summary")

    def test_recover_inflight_jobs_requeues_processing_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = orchestrator.OrchestratorStore(Path(tmpdir) / "orchestrator.db")
            store.enqueue_issue(
                project_key="BILL",
                repo_path="/tmp/billing-api",
                issue_key="BILL-1",
                status="To Do",
                summary="recover me",
            )
            store.update_job("BILL-1", state="planning", active_pid=12345, requested_action="cancel")

            store.recover_inflight_jobs()

            job = store.get_job("BILL-1")
            self.assertEqual(job["state"], "queued")
            self.assertIsNone(job["active_pid"])
            self.assertEqual(job["requested_action"], "")

    def test_status_hints_warn_when_waiting_without_refresh(self) -> None:
        hints = orchestrator.status_hints([{"state": "waiting_checks"}], refreshed=False)

        self.assertTrue(hints)
        self.assertEqual(orchestrator.status_hints([{"state": "waiting_checks"}], refreshed=True), [])

    def test_poll_github_jobs_blocks_when_codex_review_never_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            store = orchestrator.OrchestratorStore(Path(tmpdir) / "orchestrator.db")
            project = orchestrator.RepoProject(
                project_key="BILL",
                repo_path=repo,
                repo_name="repo",
                confluence_space="ENG",
                codex_review_mode="auto_required",
                manifest_path=repo / ".platform" / "platform.yaml",
                source_repo="takey7/platform",
                workflow_ref="v0.1.5",
            )
            store.sync_projects([project])
            store.enqueue_issue(
                project_key="BILL",
                repo_path=str(repo),
                issue_key="BILL-1",
                status="To Do",
                summary="review timeout",
            )
            store.update_job(
                "BILL-1",
                state="waiting_review",
                branch="codex/BILL-1-review-timeout",
                worktree_path=str(repo),
                pr_number="1",
                review_requested_at="2000-01-01T00:00:00+00:00",
                review_fallback_requested_at="2000-01-01T00:00:00+00:00",
            )
            settings = SimpleNamespace(
                codex_review_authors=("codex", "codex[bot]"),
                auto_review_grace_seconds=0,
                fallback_review_grace_seconds=0,
            )
            service = orchestrator.OrchestratorService(settings=settings, store=store)
            original_status = orchestrator.github_pull_request_status
            original_upsert = orchestrator.upsert_summary_comment
            try:
                orchestrator.github_pull_request_status = lambda *_args, **_kwargs: {
                    "number": 1,
                    "url": "https://github.example/pull/1",
                    "state": "OPEN",
                    "headRefOid": "abc123",
                    "reviews": [],
                    "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                }
                orchestrator.upsert_summary_comment = lambda *_args, **_kwargs: "comment-1"

                refreshed = service.poll_github_jobs(issue_key="BILL-1")
            finally:
                orchestrator.github_pull_request_status = original_status
                orchestrator.upsert_summary_comment = original_upsert

            job = store.get_job("BILL-1")
            self.assertEqual(refreshed, 1)
            self.assertEqual(job["state"], "blocked")
            self.assertIn("Codex review did not arrive", job["latest_error"])

    def test_status_from_process_marks_timeout_fallback_distinct_from_success(self) -> None:
        self.assertEqual(
            orchestrator.status_from_process(None, timed_out=True, has_fallback=True),
            "fallback",
        )
        self.assertEqual(
            orchestrator.status_from_process(0, timed_out=False, has_fallback=False),
            "success",
        )
        self.assertEqual(
            orchestrator.status_from_process(1, timed_out=False, has_fallback=False),
            "failed",
        )

    def test_run_tracked_command_timeout_reports_timeout(self) -> None:
        class FakeProcess:
            pid = 4242
            returncode = None

            def __init__(self) -> None:
                self.calls = 0

            def communicate(self, timeout: int | None = None) -> tuple[str, str]:
                if timeout is not None and self.calls == 0:
                    self.calls += 1
                    raise subprocess.TimeoutExpired(["fake"], timeout)
                self.returncode = -15
                return "", "stopped"

            def poll(self) -> int | None:
                return self.returncode

        with tempfile.TemporaryDirectory() as tmpdir:
            store = orchestrator.OrchestratorStore(Path(tmpdir) / "orchestrator.db")
            original_start = orchestrator.start_process
            original_terminate = orchestrator.terminate_process_group
            try:
                orchestrator.start_process = lambda *_args, **_kwargs: FakeProcess()
                orchestrator.terminate_process_group = lambda *_args, **_kwargs: None

                with self.assertRaises(orchestrator.OrchestratorError) as context:
                    orchestrator.run_tracked_command(
                        store,
                        "BILL-1",
                        ["fake"],
                        timeout_seconds=1,
                    )
            finally:
                orchestrator.start_process = original_start
                orchestrator.terminate_process_group = original_terminate

            self.assertIn("timed out after 1s", str(context.exception))

    def test_summarize_reviews_detects_codex_reviews(self) -> None:
        summary = orchestrator.summarize_reviews(
            [
                {
                    "state": "COMMENTED",
                    "author": {"login": "codex"},
                }
            ],
            ("codex", "codex[bot]"),
        )

        self.assertTrue(summary["reviewed"])
        self.assertFalse(summary["changes_requested"])
        self.assertIn("commented", summary["summary"])

    def test_load_orchestrator_config_migrates_legacy_listen_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "orchestrator.json"
            config_path.write_text(
                '{"version":1,"listen_url":"https://orchestrator.example.com","projects_roots":["/tmp/workspaces"]}\n',
                encoding="utf-8",
            )

            config = orchestrator.load_orchestrator_config(config_path)

            self.assertEqual(config["public_base_url"], "https://orchestrator.example.com")
            self.assertEqual(config["bind_host"], orchestrator.DEFAULT_BIND_HOST)

    def test_export_automation_rule_blueprints_use_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "billing-api"
            repo.mkdir()
            project = orchestrator.RepoProject(
                project_key="BILL",
                repo_path=repo,
                repo_name="billing-api",
                confluence_space="ENG",
                codex_review_mode="auto_required",
                manifest_path=repo / ".platform" / "platform.yaml",
                source_repo="takey7/platform",
                workflow_ref="v0.1.5",
            )
            settings = SimpleNamespace(
                public_base_url="",
                jira_site_url="",
                jira_admin_email="",
            )

            orchestrator.export_automation_rule_blueprints(settings, project)

            lifecycle_rule = (repo / ".platform" / "orchestrator" / "automation-rules" / "lifecycle.rule.json").read_text(
                encoding="utf-8"
            )
            self.assertIn("{{PUBLIC_BASE_URL}}/jira/events/BILL", lifecycle_rule)
            self.assertIn("{{WEBHOOK_SECRET}}", lifecycle_rule)

    def test_git_changed_files_filters_orchestrator_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.name", "Test User")
            self._git(repo, "config", "user.email", "test@example.com")
            (repo / "package.json").write_text('{"name":"repo"}\n', encoding="utf-8")
            self._git(repo, "add", "package.json")
            self._git(repo, "commit", "-m", "init")

            (repo / "README.md").write_text("# repo\n", encoding="utf-8")
            (repo / "docs" / "specs").mkdir(parents=True, exist_ok=True)
            (repo / "docs" / "specs" / "BILL-1.md").write_text("# spec\n", encoding="utf-8")
            (repo / ".platform").mkdir(parents=True, exist_ok=True)
            (repo / ".platform" / ".last-validation.json").write_text("{}\n", encoding="utf-8")
            (repo / ".tmp").mkdir(parents=True, exist_ok=True)
            (repo / ".tmp" / "impact.patch").write_text("patch\n", encoding="utf-8")

            self.assertEqual(
                orchestrator.git_changed_files(repo),
                ["README.md", "docs/specs/BILL-1.md"],
            )
            self.assertTrue(orchestrator.git_has_meaningful_changes(repo))

    def test_stage_meaningful_changes_stages_new_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.name", "Test User")
            self._git(repo, "config", "user.email", "test@example.com")
            (repo / "package.json").write_text('{"name":"repo"}\n', encoding="utf-8")
            self._git(repo, "add", "package.json")
            self._git(repo, "commit", "-m", "init")

            (repo / "README.md").write_text("# repo\n", encoding="utf-8")
            (repo / ".platform").mkdir(parents=True, exist_ok=True)
            (repo / ".platform" / ".last-validation.json").write_text("{}\n", encoding="utf-8")

            staged = orchestrator.stage_meaningful_changes(repo)

            self.assertEqual(staged, ["README.md"])
            cached = self._git(repo, "diff", "--cached", "--name-only")
            self.assertEqual(cached.stdout.strip().splitlines(), ["README.md"])
            status_lines = self._git(repo, "status", "--short", "--untracked-files=all").stdout.splitlines()
            self.assertIn("A  README.md", status_lines)
            self.assertIn("?? .platform/.last-validation.json", status_lines)

    def _git(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
