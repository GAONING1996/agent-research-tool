"""Offline tests for the Open Notebook extraction route."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fetch_smart


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class NotebookFetchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.state_path = root / ".notebook_state.json"
        replacements = {
            "NOTEBOOK_STATE_PATH": self.state_path,
            "NOTEBOOK_STATE_LOCK_PATH": root / ".notebook_state.lock",
            "NOTEBOOK_REQUEST_LOCK_STEM": root / ".notebook_request",
        }
        for name, value in replacements.items():
            context = patch.object(fetch_smart, name, value)
            context.start()
            self.addCleanup(context.stop)

    def test_auto_without_url_makes_no_notebook_requests(self):
        body = "Readable article body with enough useful detail. " * 50
        with patch.dict(os.environ, {"OPEN_NOTEBOOK_URL": ""}), patch.object(
            fetch_smart, "_load_local_env"
        ), patch.object(fetch_smart, "_http_extract", return_value=("Article", body)), patch.object(
            fetch_smart.requests, "get"
        ) as get, patch.object(fetch_smart.requests, "post") as post, patch.object(
            fetch_smart.requests, "delete"
        ) as delete:
            result = fetch_smart.fetch_smart("https://example.com/article")

        self.assertEqual(result["method"], "http")
        self.assertEqual(result["text"], body.strip())
        get.assert_not_called()
        post.assert_not_called()
        delete.assert_not_called()

    def test_connection_failure_keeps_existing_body_and_records_reason(self):
        with patch.dict(os.environ, {"OPEN_NOTEBOOK_URL": "http://notebook.test"}), patch.object(
            fetch_smart, "_load_local_env"
        ), patch.object(fetch_smart, "_http_extract", return_value=("Article", "Partial body")), patch.object(
            fetch_smart, "_tavily_extract", side_effect=RuntimeError("Tavily unavailable")
        ), patch.object(fetch_smart, "_browser_extract", side_effect=RuntimeError("Browser unavailable")), patch.object(
            fetch_smart, "_opencli_extract", side_effect=RuntimeError("OpenCLI unavailable")
        ), patch.object(
            fetch_smart.requests, "post", side_effect=fetch_smart.requests.exceptions.ConnectionError("refused")
        ), patch.object(fetch_smart.requests, "get") as get:
            result = fetch_smart.fetch_smart("https://example.com/article")

        self.assertEqual(result["text"], "Partial body")
        self.assertEqual(result["method"], "http:low")
        self.assertIn("notebook_unavailable", result["reasons"])
        get.assert_not_called()

    def test_login_wall_skips_notebook_and_uses_opencli(self):
        opencli_body = "Authenticated article text. " * 80
        with patch.dict(os.environ, {"OPEN_NOTEBOOK_URL": "http://notebook.test"}), patch.object(
            fetch_smart, "_load_local_env"
        ), patch.object(fetch_smart, "_http_extract", return_value=("Login", "请先登录")), patch.object(
            fetch_smart, "_tavily_extract", side_effect=RuntimeError("no Tavily")
        ), patch.object(fetch_smart, "_browser_extract", side_effect=RuntimeError("no browser")), patch.object(
            fetch_smart, "_opencli_extract", return_value=("Article", opencli_body)
        ), patch.object(fetch_smart.requests, "get") as get, patch.object(
            fetch_smart.requests, "post"
        ) as post, patch.object(fetch_smart.requests, "delete") as delete:
            result = fetch_smart.fetch_smart("https://example.com/private")

        self.assertEqual(result["method"], "opencli")
        self.assertEqual(result["text"], opencli_body.strip())
        get.assert_not_called()
        post.assert_not_called()
        delete.assert_not_called()

    def test_running_source_times_out_within_budget_and_is_deleted(self):
        clock = [0.0]

        def make_response(method, url, **_kwargs):
            if method == "post" and url.endswith("/api/notebooks"):
                return FakeResponse({"id": "notebook-1"})
            if method == "post" and url.endswith("/api/sources"):
                return FakeResponse({"id": "source-1"})
            if method == "get" and url.endswith("/api/sources/source-1/status"):
                return FakeResponse({"status": "running"})
            if method == "delete" and url.endswith("/api/sources/source-1"):
                return FakeResponse({}, 204)
            self.fail(f"unexpected Notebook request: {method} {url}")

        with patch.dict(os.environ, {"OPEN_NOTEBOOK_URL": "http://notebook.test"}), patch.object(
            fetch_smart, "_load_local_env"
        ), patch.object(fetch_smart, "NOTEBOOK_BUDGET_SECONDS", 3.0), patch.object(
            fetch_smart.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(
            fetch_smart.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        ), patch.object(fetch_smart.requests, "post", side_effect=lambda url, **kw: make_response("post", url, **kw)), patch.object(
            fetch_smart.requests, "get", side_effect=lambda url, **kw: make_response("get", url, **kw)
        ), patch.object(fetch_smart.requests, "delete", side_effect=lambda url, **kw: make_response("delete", url, **kw)) as delete:
            with self.assertRaises(fetch_smart.NotebookError) as raised:
                fetch_smart._notebook_extract("https://example.com/article", 100.0)

        self.assertEqual(raised.exception.reason, "notebook_timeout")
        self.assertLessEqual(clock[0], 3.0)
        delete.assert_called_once()
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["pending_sources"], [])

    def test_completed_source_body_is_adopted_and_source_is_deleted(self):
        body = "Clean article body from Open Notebook. " * 100
        post_calls = []

        def fake_post(url, **kwargs):
            post_calls.append((url, kwargs))
            if url.endswith("/api/notebooks"):
                return FakeResponse({"id": "notebook-1"})
            return FakeResponse({"id": "source-1"})

        def fake_get(url, **_kwargs):
            if url.endswith("/api/sources/source-1/status"):
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["pending_sources"], [{"id": "source-1", "pid": os.getpid()}])
                return FakeResponse({"status": "completed"})
            if url.endswith("/api/sources/source-1"):
                return FakeResponse({"title": "Clean article", "full_text": body})
            self.fail(f"unexpected Notebook GET: {url}")

        with patch.dict(os.environ, {"OPEN_NOTEBOOK_URL": "http://notebook.test"}), patch.object(
            fetch_smart, "_load_local_env"
        ), patch.object(fetch_smart.requests, "post", side_effect=fake_post), patch.object(
            fetch_smart.requests, "get", side_effect=fake_get
        ), patch.object(fetch_smart.requests, "delete", return_value=FakeResponse({}, 204)) as delete:
            result = fetch_smart.fetch_smart("https://example.com/article", route="notebook")

        self.assertEqual(result["method"], "notebook")
        self.assertEqual(result["title"], "Clean article")
        self.assertEqual(result["text"], body.strip())
        self.assertEqual(post_calls[1][1]["files"], {
            "type": (None, "link"),
            "notebook_id": (None, "notebook-1"),
            "url": (None, "https://example.com/article"),
            "embed": (None, "false"),
            "async_processing": (None, "true"),
        })
        delete.assert_called_once()
        self.assertEqual(delete.call_args.args[0], "http://notebook.test/api/sources/source-1")
        self.assertLessEqual(sum(delete.call_args.kwargs["timeout"]), fetch_smart.NOTEBOOK_BUDGET_SECONDS)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["notebook_id"], "notebook-1")
        self.assertEqual(state["pending_sources"], [])

    def test_notebook_login_wall_falls_back_to_opencli(self):
        opencli_body = "Authenticated article body. " * 80

        def fake_post(url, **_kwargs):
            if url.endswith("/api/notebooks"):
                return FakeResponse({"id": "notebook-1"})
            return FakeResponse({"id": "source-1"})

        def fake_get(url, **_kwargs):
            if url.endswith("/api/sources/source-1/status"):
                return FakeResponse({"status": "completed"})
            return FakeResponse({"title": "Login", "full_text": "请先登录"})

        with patch.dict(os.environ, {"OPEN_NOTEBOOK_URL": "http://notebook.test"}), patch.object(
            fetch_smart, "_load_local_env"
        ), patch.object(fetch_smart.requests, "post", side_effect=fake_post), patch.object(
            fetch_smart.requests, "get", side_effect=fake_get
        ), patch.object(fetch_smart.requests, "delete", return_value=FakeResponse({}, 204)) as delete, patch.object(
            fetch_smart, "_opencli_extract", return_value=("Article", opencli_body)
        ) as opencli:
            result = fetch_smart.fetch_smart("https://example.com/private", route="notebook")

        self.assertEqual(result["method"], "opencli")
        self.assertEqual(result["text"], opencli_body.strip())
        self.assertEqual(result["reasons"], ["notebook_login_wall"])
        opencli.assert_called_once()
        delete.assert_called_once()

    def test_startup_deletes_stale_pending_sources(self):
        self.state_path.write_text(json.dumps({
            "notebook_id": "notebook-1",
            "pending_sources": [{"id": "stale-source", "pid": 0}],
        }), encoding="utf-8")
        deleted = []

        def fake_post(url, **_kwargs):
            return FakeResponse({"id": "new-source"})

        def fake_get(url, **_kwargs):
            if url.endswith("/status"):
                return FakeResponse({"status": "completed"})
            return FakeResponse({"title": "Article", "full_text": "Clean article body. " * 60})

        def fake_delete(url, **_kwargs):
            deleted.append(url.rsplit("/", 1)[-1])
            return FakeResponse({}, 204)

        with patch.dict(os.environ, {"OPEN_NOTEBOOK_URL": "http://notebook.test"}), patch.object(
            fetch_smart, "requests"
        ) as requests_mock:
            requests_mock.post.side_effect = fake_post
            requests_mock.get.side_effect = fake_get
            requests_mock.delete.side_effect = fake_delete
            title, text = fetch_smart._notebook_extract("https://example.com/article", fetch_smart.time.monotonic() + 20)

        self.assertEqual(title, "Article")
        self.assertIn("Clean article body", text)
        self.assertEqual(deleted, ["stale-source", "new-source"])
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["pending_sources"], [])

    def test_stale_notebook_id_is_recreated_once(self):
        self.state_path.write_text(
            json.dumps({"notebook_id": "notebook-stale", "pending_sources": []}), encoding="utf-8"
        )
        source_attempts = []

        def fake_post(url, **kwargs):
            if url.endswith("/api/notebooks"):
                return FakeResponse({"id": "notebook-new"})
            source_attempts.append(kwargs["files"]["notebook_id"][1])
            if kwargs["files"]["notebook_id"][1] == "notebook-stale":
                raise fetch_smart.NotebookError("notebook_failed", "HTTP 404")
            return FakeResponse({"id": "source-9"})

        def fake_get(url, **_kwargs):
            if url.endswith("/api/sources/source-9/status"):
                return FakeResponse({"status": "completed"})
            if url.endswith("/api/sources/source-9"):
                return FakeResponse({"title": "Recovered", "full_text": "body after recovery " * 80})
            self.fail(f"unexpected Notebook GET: {url}")

        with patch.dict(os.environ, {"OPEN_NOTEBOOK_URL": "http://notebook.test"}), patch.object(
            fetch_smart.requests, "post", side_effect=fake_post
        ), patch.object(fetch_smart.requests, "get", side_effect=fake_get), patch.object(
            fetch_smart.requests, "delete", return_value=FakeResponse({}, 204)
        ):
            title, text = fetch_smart._notebook_extract(
                "https://example.com/article", fetch_smart.time.monotonic() + 20
            )

        self.assertEqual(title, "Recovered")
        self.assertIn("body after recovery", text)
        self.assertEqual(source_attempts, ["notebook-stale", "notebook-new"])
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["notebook_id"], "notebook-new")


if __name__ == "__main__":
    unittest.main(verbosity=2)
