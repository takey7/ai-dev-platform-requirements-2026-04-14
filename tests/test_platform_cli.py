from __future__ import annotations

import argparse
import contextlib
import io
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "platform.py"
SPEC = importlib.util.spec_from_file_location("platform_cli", MODULE_PATH)
platform = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = platform
SPEC.loader.exec_module(platform)


class PlatformCliTests(unittest.TestCase):
    def test_generate_jira_project_key_candidate(self) -> None:
        self.assertEqual(platform.generate_jira_project_key_candidate("billing-api"), "BA")
        self.assertEqual(platform.generate_jira_project_key_candidate("core"), "CORE")
        self.assertEqual(platform.generate_jira_project_key_candidate("123-platform"), "PLAT")

    def test_resolve_create_project_settings_prefers_cli_values(self) -> None:
        args = argparse.Namespace(
            project_name="Billing API",
            github_owner="override-owner",
            root="/tmp/projects",
            repo_name="billing-api",
            jira_key="BILL",
            jira_name="Billing API",
            confluence_space="DOCS",
            source_repo="override/platform",
            version="v9.9.9",
            adapter="node-ts",
            launch_mode="none",
            keep_partials=False,
        )
        config = {
            "github_owner": "config-owner",
            "projects_root": "/tmp/config-root",
            "source_repo": "config/platform",
            "source_ref": "v1.0.0",
            "adapter": "node-ts",
            "launch_mode": "tmux",
            "deploy_mode": "staging-prod",
            "jira": {
                "site_url": "https://example.atlassian.net",
                "admin_email": "admin@example.com",
            },
        }

        settings = platform.resolve_create_project_settings(args, config)

        self.assertEqual(settings["github_owner"], "override-owner")
        self.assertEqual(settings["repo_name"], "billing-api")
        self.assertEqual(settings["jira_key"], "BILL")
        self.assertEqual(settings["confluence_space"], "DOCS")
        self.assertEqual(settings["source_repo"], "override/platform")
        self.assertEqual(settings["source_ref"], "v9.9.9")
        self.assertEqual(settings["launch_mode"], "none")

    def test_resolve_create_project_settings_reads_keychain_token(self) -> None:
        args = argparse.Namespace(
            project_name="Billing API",
            github_owner="takey7",
            root="/tmp/projects",
            repo_name="billing-api",
            jira_key="BILL",
            jira_name="Billing API",
            confluence_space=None,
            source_repo="takey7/platform",
            version="v1.0.0",
            adapter="node-ts",
            launch_mode="none",
            keep_partials=False,
        )
        config = {
            "jira": {
                "site_url": "https://example.atlassian.net",
                "admin_email": "admin@example.com",
            },
        }
        original_token = platform.platform_orchestrator.atlassian_api_token
        try:
            platform.platform_orchestrator.atlassian_api_token = lambda: "stored-token"
            settings = platform.resolve_create_project_settings(args, config)
        finally:
            platform.platform_orchestrator.atlassian_api_token = original_token

        self.assertEqual(settings["jira_api_token"], "stored-token")

    def test_resolve_setup_repo_settings_defaults_to_current_repo_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "Existing Service"
            target.mkdir()
            args = argparse.Namespace(
                target=str(target),
                project_name=None,
                github_owner="takey7",
                repo_name=None,
                jira_key=None,
                jira_name=None,
                confluence_space=None,
                source_repo="takey7/platform",
                version="v1.0.0",
                adapter="node-ts",
                deploy_mode=None,
                launch_mode="none",
                skip_github_create=False,
                skip_jira_create=False,
                skip_register=False,
                no_commit_push=False,
                allow_dirty=False,
                force=False,
            )
            config = {
                "deploy_mode": "staging-prod",
                "jira": {
                    "site_url": "https://example.atlassian.net",
                    "admin_email": "admin@example.com",
                },
            }
            original_token = platform.platform_orchestrator.atlassian_api_token
            try:
                platform.platform_orchestrator.atlassian_api_token = lambda: "stored-token"
                settings = platform.resolve_setup_repo_settings(args, config)
            finally:
                platform.platform_orchestrator.atlassian_api_token = original_token

        self.assertEqual(settings["repo_name"], "existing-service")
        self.assertEqual(settings["project_name"], "existing service")
        self.assertEqual(settings["jira_key"], "ES")
        self.assertEqual(settings["github_repo"], "takey7/existing-service")
        self.assertEqual(settings["jira_api_token"], "stored-token")
        self.assertTrue(settings["create_github"])
        self.assertTrue(settings["create_jira"])
        self.assertTrue(settings["register_orchestrator"])

    def test_setup_repo_dirty_guard_blocks_existing_committed_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir)
            (target / ".git").mkdir()
            settings = {
                "target": target,
                "commit_and_push": True,
                "allow_dirty": False,
                "create_github": False,
                "create_jira": False,
                "force": False,
                "register_orchestrator": False,
                "launch_mode": "none",
                "repo_name": "demo",
            }
            original_resolve = platform.resolve_setup_repo_settings
            original_preflight = platform.preflight_setup_repo
            original_ensure_git = platform.ensure_git_repository
            original_has_commits = platform.git_has_commits
            original_status = platform.git_status_porcelain
            original_remote_url = platform.git_remote_url
            try:
                platform.resolve_setup_repo_settings = lambda _args, _config: settings
                platform.preflight_setup_repo = lambda _settings: None
                platform.ensure_git_repository = lambda _target: None
                platform.git_has_commits = lambda _target: True
                platform.git_status_porcelain = lambda _target: " M app.ts"
                platform.git_remote_url = lambda _target: "https://github.com/takey7/demo.git"

                code = platform.cmd_setup_repo(argparse.Namespace())
            finally:
                platform.resolve_setup_repo_settings = original_resolve
                platform.preflight_setup_repo = original_preflight
                platform.ensure_git_repository = original_ensure_git
                platform.git_has_commits = original_has_commits
                platform.git_status_porcelain = original_status
                platform.git_remote_url = original_remote_url

        self.assertEqual(code, 1)

    def test_new_spec_rejects_foreign_project_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir)
            platform.bootstrap_repository(
                target=target,
                adapter="node-ts",
                service_name="demo-service",
                issue_project_key="DEMO",
                confluence_space="PLATFORM",
                source_repo="takey7/example",
                source_ref="v1.0.0",
                deploy_mode="staging-prod",
                overwrite=False,
                include_adapter_starter=False,
                existing_manifest=None,
            )
            args = argparse.Namespace(
                issue_key="OTHER-123",
                target=str(target),
                title="foreign key",
                owner="platform-team",
                branch=None,
                force=False,
            )
            with self.assertRaises(SystemExit):
                platform.cmd_new_spec(args)

    def test_build_manifest_includes_codex_review_mode(self) -> None:
        manifest = platform.build_manifest(
            {
                "RELEASE_TAG": "1.2.3",
                "ADAPTER": "node-ts",
                "SERVICE_NAME": "demo-service",
                "ISSUE_PROJECT_KEY": "DEMO",
                "CONFLUENCE_SPACE": "ENG",
                "SOURCE_REPO": "takey7/platform",
                "SOURCE_REF": "v1.2.3",
                "DEPLOY_MODE": "staging-prod",
                "INSTALL_COMMAND": "pnpm install",
                "LINT_COMMAND": "pnpm lint",
                "TYPECHECK_COMMAND": "pnpm typecheck",
                "UNIT_TEST_COMMAND": "pnpm test:unit",
                "INTEGRATION_TEST_COMMAND": "pnpm test:integration",
                "BUILD_COMMAND": "pnpm build",
            },
            None,
        )

        self.assertEqual(
            manifest["integrations"]["github"]["codex_review"]["mode"],
            platform.DEFAULT_CODEX_REVIEW_MODE,
        )
        self.assertEqual(
            manifest["integrations"]["atlassian"]["transition_policy"]["mode"],
            "kanban_minimal",
        )

    def test_inspect_target_flags_foreign_spec_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir)
            platform.bootstrap_repository(
                target=target,
                adapter="node-ts",
                service_name="demo-service",
                issue_project_key="DEMO",
                confluence_space="PLATFORM",
                source_repo="takey7/example",
                source_ref="v1.0.0",
                deploy_mode="staging-prod",
                overwrite=False,
                include_adapter_starter=False,
                existing_manifest=None,
            )
            bad_spec = target / "docs" / "specs" / "OTHER-123.md"
            bad_spec.write_text("# OTHER-123 Spec\n", encoding="utf-8")

            errors, _warnings = platform.inspect_target(target)

            self.assertTrue(
                any("OTHER-123.md does not match Jira project key `DEMO`" in error for error in errors)
            )

    def test_codex_review_health_distinguishes_fallback_comment_from_review(self) -> None:
        manifest = {"integrations": {"github": {"codex_review": {"mode": "auto_required"}}}}
        original_run_optional = platform.run_optional
        original_which = platform.shutil.which
        try:
            platform.shutil.which = lambda name: "/usr/bin/gh" if name == "gh" else original_which(name)
            platform.run_optional = lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "number": 1,
                            "url": "https://github.example/pull/1",
                            "reviews": [],
                            "comments": [{"body": "@codex review"}],
                        }
                    ]
                ),
            )

            warnings = platform.check_codex_review_health(Path("/tmp/repo"), manifest)
        finally:
            platform.run_optional = original_run_optional
            platform.shutil.which = original_which

        self.assertTrue(any("fallback requests" in warning for warning in warnings))
        self.assertTrue(any(platform.CODEX_CODE_REVIEW_SETTINGS_URL in warning for warning in warnings))

    def test_codex_review_health_accepts_real_review(self) -> None:
        warnings = platform.codex_review_health_from_prs(
            [
                {
                    "reviews": [{"author": {"login": "codex[bot]"}}],
                    "comments": [],
                }
            ]
        )

        self.assertEqual(warnings, [])

    def test_codex_review_health_accepts_codex_connector_comment(self) -> None:
        warnings = platform.codex_review_health_from_prs(
            [
                {
                    "reviews": [],
                    "comments": [
                        {
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": "Codex Review: Didn't find any major issues.",
                        }
                    ],
                }
            ]
        )

        self.assertEqual(warnings, [])

    def test_toolchain_doctor_reports_resolved_codex(self) -> None:
        original_resolve = platform.platform_orchestrator.resolve_codex_toolchain
        try:
            platform.platform_orchestrator.resolve_codex_toolchain = lambda **_kwargs: {
                "binary": "/tmp/codex",
                "version": "codex-cli 0.125.0",
                "compatible": True,
                "capabilities": {"--ignore-user-config": True},
            }
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = platform.cmd_toolchain_doctor(SimpleNamespace(codex_binary=None))
        finally:
            platform.platform_orchestrator.resolve_codex_toolchain = original_resolve

        self.assertEqual(code, 0)
        self.assertIn("/tmp/codex", output.getvalue())

    def test_orchestrator_registration_missing_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir)
            (target / "orchestrator.db").write_text("", encoding="utf-8")
            manifest = {"issue": {"project_key": "DEMO"}}
            original_load = platform.platform_orchestrator.load_worker_settings
            original_store = platform.platform_orchestrator.OrchestratorStore
            try:
                platform.platform_orchestrator.load_worker_settings = lambda: SimpleNamespace(
                    db_path=target / "orchestrator.db",
                    public_base_url="https://orchestrator.example.com",
                )

                class EmptyStore:
                    def __init__(self, _path: Path) -> None:
                        pass

                    def project(self, _project_key: str) -> None:
                        return None

                platform.platform_orchestrator.OrchestratorStore = EmptyStore

                warnings = platform.check_orchestrator_registration(target, manifest)
            finally:
                platform.platform_orchestrator.load_worker_settings = original_load
                platform.platform_orchestrator.OrchestratorStore = original_store

            self.assertTrue(any("Orchestrator registration not found" in warning for warning in warnings))

    def test_orchestrator_polling_mode_does_not_require_public_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "repo"
            target.mkdir()
            manifest = {"issue": {"project_key": "DEMO"}}
            original_load = platform.platform_orchestrator.load_worker_settings
            original_store = platform.platform_orchestrator.OrchestratorStore
            try:
                platform.platform_orchestrator.load_worker_settings = lambda: SimpleNamespace(
                    db_path=Path(tmpdir) / "orchestrator.db",
                    public_base_url="",
                    event_mode="polling",
                )

                class RegisteredStore:
                    def __init__(self, _path: Path) -> None:
                        pass

                    def project(self, _project_key: str) -> dict[str, str]:
                        return {
                            "repo_path": str(target),
                            "control_issue_key": "DEMO-1",
                            "webhook_secret": "",
                            "lifecycle_rule_uuid": "",
                            "comment_rule_uuid": "",
                        }

                platform.platform_orchestrator.OrchestratorStore = RegisteredStore

                warnings = platform.check_orchestrator_registration(target, manifest)
            finally:
                platform.platform_orchestrator.load_worker_settings = original_load
                platform.platform_orchestrator.OrchestratorStore = original_store

            self.assertFalse(any("public_base_url" in warning for warning in warnings))
            self.assertFalse(any("Automation rule IDs" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
