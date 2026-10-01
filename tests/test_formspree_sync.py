"""Offline regression tests. Every requests entry point is a mock transport."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "formspree_sync.py"
SPEC = importlib.util.spec_from_file_location("formspree_sync", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)

SUBMISSION = {
    "_id": "private-submission-sentinel",
    "body": {
        "company": "private-company-sentinel",
        "name": "private-person-sentinel",
        "email": "private-contact-sentinel@example.invalid",
        "service": "その他",
        "message": "private-message-sentinel",
    },
}
SENSITIVE_ERROR = (
    "private-submission-sentinel private-person-sentinel "
    "private-contact-sentinel@example.invalid private-message-sentinel "
    "https://private-webhook.invalid/private-token-sentinel"
)


def response(data=None, error=None):
    result = Mock()
    result.json.return_value = data
    if error:
        result.raise_for_status.side_effect = error
    return result


class LeadSyncTests(unittest.TestCase):
    def setUp(self):
        self.transport = {}
        for method in ("get", "post", "patch"):
            mocked = self.enterContext(patch.object(sync.requests, method))
            mocked.side_effect = AssertionError("unexpected_offline_http_call")
            self.transport[method] = mocked
        for name in (
            "FORMSPREE_API_KEY", "FORMSPREE_FORM_ID", "NOTION_API_TOKEN",
            "NOTION_DATABASE_ID", "DISCORD_WEBHOOK",
        ):
            self.enterContext(patch.object(sync, name, "synthetic-config"))

    def run_main(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = sync.main()
        return status, stdout.getvalue(), stderr.getvalue()

    def assert_no_requests(self):
        for transport in self.transport.values():
            transport.assert_not_called()

    def allow_sequence_for_offline_test_only(self):
        # Test injection exercises the unreachable sequence, not a production
        # approval mechanism or an implemented persistence/uniqueness guarantee.
        return patch.object(sync, "require_durable_delivery_contract", return_value=None)

    def prepare_fetch(self):
        self.transport["get"].side_effect = None
        self.transport["get"].return_value = response({"submissions": [SUBMISSION]})

    def test_production_guard_blocks_before_all_http_on_each_retry(self):
        for _ in range(2):
            status, stdout, stderr = self.run_main()
            self.assertEqual(status, 1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "[ERROR] lead_sync: durable_delivery_contract_missing\n")
        self.assert_no_requests()

    def test_missing_configuration_is_nonzero_before_http(self):
        for name in (
            "FORMSPREE_API_KEY", "FORMSPREE_FORM_ID", "NOTION_API_TOKEN",
            "NOTION_DATABASE_ID", "DISCORD_WEBHOOK",
        ):
            with self.subTest(name=name), patch.object(sync, name, " "):
                self.assertEqual(self.run_main(), (
                    1, "", "[ERROR] lead_sync: configuration_missing\n",
                ))
        self.assert_no_requests()

    def test_missing_id_in_later_submission_blocks_whole_batch(self):
        with self.assertRaises(sync.SyncFailure) as failure:
            sync.sync_submissions([SUBMISSION, {"body": SUBMISSION["body"]}])
        self.assertEqual(str(failure.exception), "submission_id_missing")
        self.assert_no_requests()

    def test_invalid_and_duplicate_ids_block_before_effects(self):
        cases = [
            [None], [{"id": 42}], [{"id": " "}],
            [{"id": "synthetic", "body": []}], [SUBMISSION, SUBMISSION],
        ]
        for batch in cases:
            with self.subTest(batch_shape=len(batch)), self.assertRaises(sync.SyncFailure):
                sync.sync_submissions(batch)
        self.assert_no_requests()

    def test_direct_write_helpers_reject_missing_id_before_http(self):
        for operation in (
            lambda: sync.register_to_notion({"body": {}}),
            lambda: sync.notify_discord({"body": {}}, "https://notion.invalid/page"),
            lambda: sync.mark_as_read(""),
        ):
            with self.assertRaises(sync.SyncFailure):
                operation()
        self.assert_no_requests()

    def test_fetch_http_failure_is_nonzero_and_sanitized(self):
        self.transport["get"].side_effect = None
        failed = response(error=sync.requests.exceptions.HTTPError(SENSITIVE_ERROR))
        self.transport["get"].return_value = failed
        with self.allow_sequence_for_offline_test_only():
            self.assertEqual(self.run_main(), (1, "", "[ERROR] lead_sync: http_failure\n"))
        failed.raise_for_status.assert_called_once()
        failed.json.assert_not_called()
        self.transport["post"].assert_not_called()
        self.transport["patch"].assert_not_called()

    def test_malformed_fetch_cannot_be_reported_as_no_unread_submissions(self):
        self.transport["get"].side_effect = None
        for payload in ({}, [], {"submissions": None}, {"submissions": {}}):
            with self.subTest(payload_shape=type(payload).__name__):
                self.transport["get"].return_value = response(payload)
                with self.allow_sequence_for_offline_test_only():
                    self.assertEqual(self.run_main(), (
                        1, "", "[ERROR] lead_sync: invalid_response\n",
                    ))
        self.transport["post"].assert_not_called()
        self.transport["patch"].assert_not_called()

    def test_notion_http_failure_stops_discord_and_mark_read(self):
        self.prepare_fetch()
        self.transport["post"].side_effect = None
        failed = response(error=sync.requests.exceptions.HTTPError(SENSITIVE_ERROR))
        self.transport["post"].return_value = failed
        with self.allow_sequence_for_offline_test_only():
            self.assertEqual(self.run_main(), (1, "", "[ERROR] lead_sync: http_failure\n"))
        failed.raise_for_status.assert_called_once()
        self.assertEqual(self.transport["post"].call_count, 1)
        self.transport["patch"].assert_not_called()

    def test_discord_http_failure_blocks_markread_and_production_retry(self):
        self.prepare_fetch()
        notion = response({"url": "https://notion.invalid/synthetic-page"})
        discord = response(error=sync.requests.exceptions.HTTPError(SENSITIVE_ERROR))
        self.transport["post"].side_effect = [notion, discord]
        with self.allow_sequence_for_offline_test_only():
            self.assertEqual(self.run_main(), (1, "", "[ERROR] lead_sync: http_failure\n"))
        discord.raise_for_status.assert_called_once()
        self.transport["patch"].assert_not_called()
        before = {method: transport.call_count for method, transport in self.transport.items()}
        self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(before, {
            method: transport.call_count for method, transport in self.transport.items()
        })

    def test_ambiguous_create_or_discord_timeout_never_replays_on_retry(self):
        for stage in ("notion", "discord"):
            with self.subTest(stage=stage):
                for transport in self.transport.values():
                    transport.reset_mock()
                self.prepare_fetch()
                effects = []

                def uncertain_post(url, **kwargs):
                    effects.append("create" if url == "https://api.notion.com/v1/pages" else "notify")
                    if stage == "notion" or effects[-1] == "notify":
                        # Transport recorded a possible remote effect before the
                        # reply was lost. Its outcome must never be assumed absent.
                        raise sync.requests.exceptions.Timeout(SENSITIVE_ERROR)
                    return response({"url": "https://notion.invalid/synthetic-page"})

                self.transport["post"].side_effect = uncertain_post
                with self.allow_sequence_for_offline_test_only():
                    self.assertEqual(self.run_main(), (
                        1, "", "[ERROR] lead_sync: request_outcome_uncertain\n",
                    ))
                self.transport["patch"].assert_not_called()
                self.assertEqual(effects.count("create"), 1)
                before = list(effects)
                self.assertEqual(self.run_main()[0], 1)
                self.assertEqual(effects, before)

    def test_markread_http_failure_is_nonzero_and_retry_does_not_recreate(self):
        self.prepare_fetch()
        self.transport["post"].side_effect = [
            response({"url": "https://notion.invalid/synthetic-page"}), response(),
        ]
        failed = response(error=sync.requests.exceptions.HTTPError(SENSITIVE_ERROR))
        self.transport["patch"].side_effect = None
        self.transport["patch"].return_value = failed
        with self.allow_sequence_for_offline_test_only():
            self.assertEqual(self.run_main(), (1, "", "[ERROR] lead_sync: http_failure\n"))
        failed.raise_for_status.assert_called_once()
        self.assertEqual(self.transport["post"].call_count, 2)
        self.assertEqual(self.transport["patch"].call_count, 1)
        self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(self.transport["post"].call_count, 2)
        self.assertEqual(self.transport["patch"].call_count, 1)

    def test_missing_notion_url_is_uncertain_and_stops_followup(self):
        self.prepare_fetch()
        self.transport["post"].side_effect = None
        self.transport["post"].return_value = response({"id": "synthetic-page"})
        with self.allow_sequence_for_offline_test_only():
            self.assertEqual(self.run_main(), (1, "", "[ERROR] lead_sync: invalid_response\n"))
        self.assertEqual(self.transport["post"].call_count, 1)
        self.transport["patch"].assert_not_called()

    def test_success_is_reported_only_after_all_http_checks(self):
        self.prepare_fetch()
        notion, discord, markread = (
            response({"url": "https://notion.invalid/synthetic-page"}), response(), response(),
        )
        self.transport["post"].side_effect = [notion, discord]
        self.transport["patch"].side_effect = None
        self.transport["patch"].return_value = markread
        with self.allow_sequence_for_offline_test_only():
            self.assertEqual(self.run_main(), (0, "[OK] lead_sync: delivery_complete\n", ""))
        for reply in (notion, discord, markread):
            reply.raise_for_status.assert_called_once()
        payload = self.transport["post"].call_args_list[0].kwargs["json"]
        self.assertEqual(set(payload["properties"]), {
            "名前", "クライアント", "担当者", "種別", "ステータス", "問い合わせ日", "メモ",
        })

    def test_unknown_exception_text_is_not_logged(self):
        with self.allow_sequence_for_offline_test_only(), patch.object(
            sync, "fetch_submissions", side_effect=RuntimeError(SENSITIVE_ERROR),
        ):
            self.assertEqual(self.run_main(), (1, "", "[ERROR] lead_sync: unexpected_failure\n"))
        self.assert_no_requests()

    def test_unrecognized_internal_error_cannot_leak_or_break_reporter(self):
        with self.allow_sequence_for_offline_test_only(), patch.object(
            sync, "fetch_submissions", side_effect=sync.SyncFailure({"private": SENSITIVE_ERROR}),
        ):
            self.assertEqual(self.run_main(), (1, "", "[ERROR] lead_sync: unexpected_failure\n"))
        self.assert_no_requests()

    def test_actual_cli_with_synthetic_env_and_fake_transport(self):
        harness = r'''
import json, runpy, sys, types
fake = types.ModuleType("requests")
calls = []
class RequestException(Exception): pass
class HTTPError(RequestException): pass
class Timeout(RequestException): pass
fake.exceptions = types.SimpleNamespace(
    RequestException=RequestException, HTTPError=HTTPError, Timeout=Timeout,
)
def forbidden(*args, **kwargs):
    calls.append("unexpected_call")
    raise AssertionError("offline_transport_blocked")
fake.get = fake.post = fake.patch = forbidden
sys.modules["requests"] = fake
try:
    runpy.run_path(sys.argv[1], run_name="__main__")
except SystemExit as exit_result:
    print(json.dumps({"http_calls": len(calls)}))
    raise SystemExit(exit_result.code)
'''
        base_env = {"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")}
        synthetic = dict(base_env, **{
            "FORMSPREE_API_KEY": "synthetic-key", "FORMSPREE_FORM_ID": "synthetic-form",
            "NOTION_API_TOKEN": "synthetic-token", "NOTION_DATABASE_ID": "synthetic-db",
            "DISCORD_WEBHOOK_URL": "https://webhook.invalid/synthetic",
            "LEAD_SYNC_APPROVED": "true",  # Cannot bypass the production guard.
        })
        for env, category in (
            (base_env, "configuration_missing"),
            (synthetic, "durable_delivery_contract_missing"),
        ):
            with self.subTest(category=category):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", harness, str(SCRIPT)],
                    env=env, capture_output=True, text=True, timeout=20, check=False,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, '{"http_calls": 0}\n')
                self.assertEqual(result.stderr, f"[ERROR] lead_sync: {category}\n")


if __name__ == "__main__":
    unittest.main()
