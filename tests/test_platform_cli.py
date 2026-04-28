from __future__ import annotations

import argparse
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


if __name__ == "__main__":
    unittest.main()
