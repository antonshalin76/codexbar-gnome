from __future__ import annotations

import ast
import itertools
import json
import math
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from gi.repository import GLib

from tests.support import MODULE_PATH, load_indicator

indicator = load_indicator("codexbar_gnome_indicator_settings_gateway")

DEFAULT_SETTINGS = {
    "runtimes": {
        "codex": {"poll": True, "autoRefresh": True},
        "grok": {"poll": True, "autoRefresh": True},
        "claude": {"poll": False, "autoRefresh": False},
    },
}
FAIL_CLOSED_SETTINGS = {
    "runtimes": {
        "codex": {"poll": False, "autoRefresh": False},
        "grok": {"poll": False, "autoRefresh": False},
        "claude": {"poll": False, "autoRefresh": False},
    },
}


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _write_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


def _write_strict_codexbar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """#!/usr/bin/python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["CODEXBAR_FAKE_LEDGER"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")

if args == ["usage", "--help"]:
    print("--provider codex|grok|claude|zai --json-only --no-color")
    raise SystemExit(0)

allowed = {
    ("usage", "--provider", "codex", "--json-only", "--no-color"),
    ("usage", "--provider", "codex", "--json-only", "--no-color", "--source", "oauth"),
    ("usage", "--provider", "grok", "--json-only", "--no-color", "--source", "auto"),
    ("usage", "--provider", "claude", "--json-only", "--no-color", "--source", "oauth"),
    ("usage", "--provider", "zai", "--json-only", "--no-color"),
}
if tuple(args) not in allowed:
    raise SystemExit(64)
provider = args[args.index("--provider") + 1]
if provider == "zai" and not os.environ.get("Z_AI_API_KEY"):
    raise SystemExit(65)
print(json.dumps([{
    "provider": provider,
    "usage": {"primary": {"usedPercent": 12, "windowMinutes": 300}},
}]))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


class RecordingExecutor:
    """Strict executor double that never records credential values."""

    HELP = b"usage --provider {codex,grok,claude,zai} --json-only --no-color\n"

    def __init__(
        self,
        executable: Path,
        payloads: dict[str, object] | None = None,
        failures: dict[str, Exception] | None = None,
        expected_zai_token: str | None = None,
    ) -> None:
        self.executable = executable
        self.payloads = payloads or {}
        self.failures = failures or {}
        self.expected_zai_token = expected_zai_token
        self.calls: list[dict[str, object]] = []

    def reserve(self, _runtime: str) -> object:
        return object()

    def release(self, _handle: object) -> None:
        pass

    def cancel(self, _runtime: str) -> None:
        pass

    def close(self) -> None:
        pass

    def run(self, request: object, _handle: object | None = None) -> object:
        argv = tuple(str(value) for value in request.argv)
        overrides = dict(request.env_overrides)
        child_environment = dict(os.environ)
        for key, value in overrides.items():
            if value is None:
                child_environment.pop(key, None)
            else:
                child_environment[key] = value

        is_help = argv[1:] == ("usage", "--help")
        physical_provider = None
        if "--provider" in argv:
            physical_provider = argv[argv.index("--provider") + 1]
        self.calls.append(
            {
                "runtime": request.runtime,
                "argv": argv,
                "timeout": request.timeout,
                "env_keys": frozenset(overrides),
                "zai_present": "Z_AI_API_KEY" in child_environment,
                "zai_token_matches": (
                    self.expected_zai_token is not None
                    and child_environment.get("Z_AI_API_KEY") == self.expected_zai_token
                ),
            },
        )

        if is_help:
            return indicator.ProcessResult(0, self.HELP, b"")
        if physical_provider in self.failures:
            raise self.failures[physical_provider]

        payload = self.payloads.get(physical_provider)
        if payload is None:
            payload = [
                {
                    "provider": physical_provider,
                    "usage": {
                        "primary": {
                            "usedPercent": 12,
                            "windowMinutes": 300,
                            "resetDescription": "in 1 hour",
                        },
                    },
                },
            ]
        stdout = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        redaction_values = tuple(
            value
            for key, value in child_environment.items()
            if indicator._CREDENTIAL_ENV_KEY.search(key) and value
        )
        return indicator.ProcessResult(0, stdout, b"", redaction_values)

    @property
    def quota_calls(self) -> list[dict[str, object]]:
        return [call for call in self.calls if "--provider" in call["argv"]]


class SettingsStoreContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.path = self.root / "config" / "config.json"

    def test_bdd_s01_missing_config_returns_defaults_without_writing(self) -> None:
        result = indicator.SettingsStore(self.path).load()

        self.assertEqual(result.settings, DEFAULT_SETTINGS)
        self.assertIsNone(result.failure)
        self.assertFalse(self.path.exists())

    def test_bdd_s02_partial_legacy_config_fills_only_missing_values(self) -> None:
        supplied = {
            "runtimes": {
                "codex": {"autoRefresh": False},
                "claude": {"poll": True},
            },
        }
        self.path.parent.mkdir(parents=True)
        original = _canonical_json(supplied)
        self.path.write_bytes(original)

        result = indicator.SettingsStore(self.path).load()

        self.assertIsNone(result.failure)
        self.assertEqual(
            result.settings,
            {
                "runtimes": {
                    "codex": {"poll": True, "autoRefresh": False},
                    "grok": {"poll": True, "autoRefresh": True},
                    "claude": {"poll": True, "autoRefresh": False},
                },
            },
        )
        self.assertEqual(self.path.read_bytes(), original)

    def test_bdd_s03_rejects_truthy_and_falsy_non_booleans(self) -> None:
        invalid_values = (None, 0, 1, "false", [], {})
        for index, invalid in enumerate(invalid_values):
            with self.subTest(value=invalid):
                raw = {
                    "runtimes": {
                        "codex": {"poll": invalid, "autoRefresh": True},
                    },
                }
                original = json.dumps(raw).encode()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(original)

                result = indicator.SettingsStore(self.path).load()

                self.assertEqual(result.settings, FAIL_CLOSED_SETTINGS)
                self.assertIsInstance(result.failure, indicator.SettingsFailure)
                self.assertLessEqual(len(result.failure.message), 512)
                self.assertEqual(self.path.read_bytes(), original)
                self.path.unlink()

    def test_bdd_s04_poll_false_forces_auto_refresh_false(self) -> None:
        raw = {
            "runtimes": {
                "codex": {"poll": False, "autoRefresh": True},
            },
        }
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(_canonical_json(raw))

        result = indicator.SettingsStore(self.path).load()

        self.assertIsNone(result.failure)
        self.assertEqual(
            result.settings["runtimes"]["codex"],
            {"poll": False, "autoRefresh": False},
        )

    def test_bdd_s05_preserves_malformed_bytes_and_fails_closed(self) -> None:
        malformed_inputs = {
            "invalid-json": b'{"runtimes":',
            "invalid-utf8": b"\xff\xfe",
            "array-root": b"[]",
            "scalar-root": b"42",
            "oversized": b" " * (64 * 1024 + 1),
            "deeply-nested": (
                b'{"ignored":' + b"[" * 10000 + b"0" + b"]" * 10000 + b"}"
            ),
        }
        for name, original in malformed_inputs.items():
            with self.subTest(case=name):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(original)

                result = indicator.SettingsStore(self.path).load()

                self.assertEqual(result.settings, FAIL_CLOSED_SETTINGS)
                self.assertIsInstance(result.failure, indicator.SettingsFailure)
                self.assertLessEqual(len(result.failure.message), 512)
                self.assertEqual(self.path.read_bytes(), original)
                self.path.unlink()

    def test_bdd_s05_unreadable_input_fails_closed_without_mutation(self) -> None:
        self.path.mkdir(parents=True)

        result = indicator.SettingsStore(self.path).load()

        self.assertEqual(result.settings, FAIL_CLOSED_SETTINGS)
        self.assertIsInstance(result.failure, indicator.SettingsFailure)
        self.assertTrue(self.path.is_dir())

    def test_bdd_s05_non_regular_inputs_fail_closed_without_blocking(self) -> None:
        self.path.parent.mkdir(parents=True)
        target = self.root / "valid-target.json"
        target.write_bytes(_canonical_json(DEFAULT_SETTINGS))
        cases = ("fifo", "symlink")
        for case in cases:
            with self.subTest(case=case):
                if self.path.is_symlink() or self.path.exists():
                    self.path.unlink()
                if case == "fifo":
                    os.mkfifo(self.path)
                else:
                    self.path.symlink_to(target)
                started = time.monotonic()

                result = indicator.SettingsStore(self.path).load()

                self.assertLess(time.monotonic() - started, 0.25)
                self.assertEqual(result.settings, FAIL_CLOSED_SETTINGS)
                self.assertIsInstance(result.failure, indicator.SettingsFailure)
                self.assertTrue(self.path.is_symlink() or self.path.exists())
                if self.path.is_symlink() or self.path.exists():
                    self.path.unlink()
        self.assertEqual(target.read_bytes(), _canonical_json(DEFAULT_SETTINGS))

    def test_bdd_s06_save_delegates_atomic_durable_write_with_mode_0600(self) -> None:
        calls: list[tuple[str, bytes, object, int]] = []

        def writer(filename: str, contents: bytes, flags: object, mode: int) -> bool:
            payload = bytes(contents)
            calls.append((filename, payload, flags, mode))
            target = Path(filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            target.chmod(mode)
            return True

        store = indicator.SettingsStore(self.path, writer=writer)

        self.assertIsNone(store.save(DEFAULT_SETTINGS))
        self.assertEqual(len(calls), 1)
        filename, contents, flags, mode = calls[0]
        self.assertEqual(Path(filename), self.path)
        self.assertEqual(contents, _canonical_json(DEFAULT_SETTINGS))
        self.assertEqual(
            flags,
            GLib.FileSetContentsFlags.CONSISTENT | GLib.FileSetContentsFlags.DURABLE,
        )
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_bdd_s06_atomic_writer_is_the_only_commit_point(self) -> None:
        store = indicator.SettingsStore(self.path)

        with patch.object(
            indicator.os,
            "chmod",
            side_effect=AssertionError("post-commit chmod is forbidden"),
        ) as chmod:
            store.save(DEFAULT_SETTINGS)

        chmod.assert_not_called()
        self.assertEqual(self.path.read_bytes(), _canonical_json(DEFAULT_SETTINGS))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_bytes()), DEFAULT_SETTINGS)

    def test_bdd_s06_default_glib_writer_replaces_real_target_atomically(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'{"old":true}\n')
        self.path.chmod(0o644)

        store = indicator.SettingsStore(self.path)
        result = store.save(DEFAULT_SETTINGS)

        self.assertIsNone(result)
        self.assertEqual(self.path.read_bytes(), _canonical_json(DEFAULT_SETTINGS))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_bdd_s07_writer_failure_preserves_existing_target(self) -> None:
        self.path.parent.mkdir(parents=True)
        original = b'{"existing":true}\n'
        self.path.write_bytes(original)

        def failing_writer(*_args: object) -> bool:
            raise OSError("simulated pre-commit failure")

        store = indicator.SettingsStore(self.path, writer=failing_writer)

        with self.assertRaises(indicator.SettingsFailure) as raised:
            store.save(DEFAULT_SETTINGS)

        self.assertLessEqual(len(raised.exception.message), 512)
        self.assertEqual(self.path.read_bytes(), original)

    def test_bdd_s07_writer_failure_leaves_absent_target_absent(self) -> None:
        def failing_writer(*_args: object) -> bool:
            raise OSError("simulated pre-commit failure")

        store = indicator.SettingsStore(self.path, writer=failing_writer)

        with self.assertRaises(indicator.SettingsFailure):
            store.save(DEFAULT_SETTINGS)

        self.assertFalse(self.path.exists())

    def test_bdd_s07_false_writer_result_is_a_failure_without_target_damage(
        self,
    ) -> None:
        cases = ("absent", "existing")
        for case in cases:
            with self.subTest(case=case):
                if self.path.exists():
                    self.path.unlink()
                original = b'{"existing":true}\n'
                if case == "existing":
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.path.write_bytes(original)
                store = indicator.SettingsStore(self.path, writer=lambda *_args: False)

                with self.assertRaises(indicator.SettingsFailure):
                    store.save(DEFAULT_SETTINGS)

                if case == "existing":
                    self.assertEqual(self.path.read_bytes(), original)
                else:
                    self.assertFalse(self.path.exists())

    def test_bdd_s08_failed_toggle_save_rolls_back_selection_and_is_visible(
        self,
    ) -> None:
        class FailingStore:
            def __init__(self) -> None:
                self.saved: list[dict[str, object]] = []

            def save(self, settings: dict[str, object]) -> None:
                self.saved.append(settings)
                raise indicator.SettingsFailure("disk full")

        class NoCallGateway:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def reserve(self, runtime: str) -> object:
                self.calls.append(runtime)
                raise AssertionError("save failure must not reserve a provider")

            def fetch(self, runtime: str, _reservation: object) -> object:
                self.calls.append(runtime)
                raise AssertionError("save failure must not poll")

            def cancel(self, _runtime: str) -> None:
                pass

            def close(self) -> None:
                pass

        store = FailingStore()
        gateway = NoCallGateway()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            DEFAULT_SETTINGS,
            settings_store=store,
            on_snapshot=None,
        )
        candidate = json.loads(json.dumps(DEFAULT_SETTINGS))
        candidate["runtimes"]["codex"]["poll"] = False
        candidate["runtimes"]["codex"]["autoRefresh"] = False

        self.assertFalse(coordinator.update_settings(candidate))
        snapshot = coordinator.snapshot()
        self.assertEqual(
            [item.runtime for item in snapshot.runtimes], ["codex", "grok"]
        )
        self.assertEqual(snapshot.global_error, "Settings error: disk full")
        self.assertEqual(gateway.calls, [])
        self.assertEqual(len(store.saved), 1)

    def test_bdd_s09_concurrent_saves_are_serialized_and_last_commit_is_complete(
        self,
    ) -> None:
        first_entered = threading.Event()
        release_first = threading.Event()
        second_attempting = threading.Event()
        second_entered = threading.Event()
        ledger: list[str] = []
        failures: list[BaseException] = []

        settings_a = json.loads(json.dumps(DEFAULT_SETTINGS))
        settings_a["runtimes"]["grok"] = {"poll": False, "autoRefresh": False}
        settings_b = json.loads(json.dumps(DEFAULT_SETTINGS))
        settings_b["runtimes"]["claude"] = {"poll": True, "autoRefresh": True}

        def writer(filename: str, contents: bytes, _flags: object, mode: int) -> bool:
            payload = json.loads(bytes(contents))
            generation = "B" if payload == settings_b else "A"
            ledger.append(f"{generation}-start")
            if generation == "A":
                first_entered.set()
                if not release_first.wait(5):
                    raise AssertionError("first save was not released")
            else:
                second_entered.set()
            target = Path(filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes(contents))
            target.chmod(mode)
            ledger.append(f"{generation}-end")
            return True

        store = indicator.SettingsStore(self.path, writer=writer)

        def save(
            settings: dict[str, object], attempting: threading.Event | None
        ) -> None:
            if attempting is not None:
                attempting.set()
            try:
                store.save(settings)
            except Exception as exc:  # noqa: BLE001 - retained for the main test thread
                failures.append(exc)

        first = threading.Thread(target=save, args=(settings_a, None), daemon=True)
        second = threading.Thread(
            target=save,
            args=(settings_b, second_attempting),
            daemon=True,
        )

        def cleanup_threads() -> None:
            release_first.set()
            for worker in (first, second):
                if worker.ident is not None:
                    worker.join(5)

        self.addCleanup(cleanup_threads)

        first.start()
        self.assertTrue(first_entered.wait(5), "save A did not enter the writer")
        second.start()
        self.assertTrue(second_attempting.wait(5), "save B did not start")
        self.assertFalse(
            second_entered.wait(1),
            "save B entered the writer while save A held the store lock",
        )
        release_first.set()
        first.join(5)
        second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(ledger, ["A-start", "A-end", "B-start", "B-end"])
        self.assertEqual(self.path.read_bytes(), _canonical_json(settings_b))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)


class ProcessResolutionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.path_binary = self.root / "path-bin" / "codexbar"

    def resolve(self, env: dict[str, str], which: object) -> Path:
        return indicator.ProcessExecutor.resolve_executable(
            env=env,
            home=self.home,
            which=which,
        )

    def test_bdd_p01_explicit_executable_wins_without_other_lookup(self) -> None:
        explicit = self.root / "explicit path ; not shell" / "codexbar"
        _write_executable(explicit)
        _write_executable(self.home / ".local/bin/codexbar")
        which = Mock(side_effect=AssertionError("PATH lookup is forbidden"))

        resolved = self.resolve({"CODEXBAR_BIN": str(explicit)}, which)

        self.assertEqual(Path(resolved), explicit)
        which.assert_not_called()

    def test_bdd_p01_local_executable_precedes_path(self) -> None:
        local = self.home / ".local/bin/codexbar"
        _write_executable(local)
        _write_executable(self.path_binary)
        which = Mock(side_effect=AssertionError("PATH lookup is forbidden"))

        resolved = self.resolve({}, which)

        self.assertEqual(Path(resolved), local)
        which.assert_not_called()

    def test_bdd_p01_unusable_local_candidate_falls_back_to_path(self) -> None:
        local = self.home / ".local/bin/codexbar"
        local.parent.mkdir(parents=True)
        local.write_text("not executable")
        _write_executable(self.path_binary)
        which = Mock(return_value=str(self.path_binary))

        resolved = self.resolve({}, which)

        self.assertEqual(Path(resolved), self.path_binary)
        which.assert_called_once_with("codexbar")

    def test_bdd_p02_invalid_explicit_override_is_authoritative(self) -> None:
        _write_executable(self.home / ".local/bin/codexbar")
        _write_executable(self.path_binary)
        invalid_candidates = {
            "empty": "",
            "missing": str(self.root / "missing"),
            "directory": str(self.root),
            "non-executable": str(self.root / "plain-file"),
        }
        (self.root / "plain-file").write_text("plain")
        for name, explicit in invalid_candidates.items():
            with self.subTest(case=name):
                which = Mock(side_effect=AssertionError("fallback is forbidden"))

                with self.assertRaises(indicator.ProcessFailure) as raised:
                    self.resolve({"CODEXBAR_BIN": explicit}, which)

                self.assertLessEqual(len(raised.exception.sanitized_message), 512)
                self.assertIn("CODEXBAR_BIN", raised.exception.sanitized_message)
                which.assert_not_called()

    def test_bdd_p02_no_usable_candidate_fails_actionably(self) -> None:
        which = Mock(return_value=None)

        with self.assertRaises(indicator.ProcessFailure) as raised:
            self.resolve({}, which)

        self.assertLessEqual(len(raised.exception.sanitized_message), 512)
        self.assertIn("codexbar", raised.exception.sanitized_message.lower())


class _GatewayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.binary = self.root / "bin with spaces ; literal" / "codexbar"
        _write_executable(self.binary)
        self.claude_settings = self.root / "claude" / "settings.json"

    def write_claude_settings(self, environment: dict[str, object]) -> None:
        self.claude_settings.parent.mkdir(parents=True, exist_ok=True)
        self.claude_settings.write_bytes(_canonical_json({"env": environment}))

    def gateway(
        self,
        *,
        source: str = "",
        payloads: dict[str, object] | None = None,
        failures: dict[str, Exception] | None = None,
        expected_zai_token: str | None = None,
    ) -> tuple[object, RecordingExecutor]:
        executor = RecordingExecutor(
            self.binary,
            payloads=payloads,
            failures=failures,
            expected_zai_token=expected_zai_token,
        )
        gateway = indicator.ProviderGateway(
            executor,
            claude_settings_path=self.claude_settings,
            source=source,
        )
        return gateway, executor

    def assert_runtime_failure(self, result: object, runtime: str) -> None:
        self.assertIsInstance(result, indicator.RuntimeFailure)
        self.assertEqual(result.runtime, runtime)
        self.assertLessEqual(len(result.message), 512)

    def assert_window(
        self,
        window: object,
        *,
        percent: int,
        minutes: int | None,
        reset: str | None,
    ) -> None:
        self.assertEqual(window.percent, percent)
        self.assertEqual(window.window_minutes, minutes)
        self.assertEqual(window.reset_text, reset)


class ProviderGatewayContractTests(_GatewayTestCase):
    def test_bdd_p03_capability_probe_is_exact_cached_and_timeout_bounded(
        self,
    ) -> None:
        gateway, executor = self.gateway()

        first = gateway.fetch("codex")
        second = gateway.fetch("grok")

        self.assertIsInstance(first, indicator.RuntimeResult)
        self.assertIsInstance(second, indicator.RuntimeResult)
        help_calls = [
            call for call in executor.calls if call["argv"][1:] == ("usage", "--help")
        ]
        self.assertEqual(len(help_calls), 1)
        self.assertEqual(help_calls[0]["runtime"], "capability:codex")
        self.assertEqual(help_calls[0]["timeout"], 15.0)
        self.assertFalse(help_calls[0]["zai_present"])
        self.assertEqual([call["timeout"] for call in executor.quota_calls], [90, 90])

        injected = RecordingExecutor(self.binary)
        injected_gateway = indicator.ProviderGateway(
            injected,
            claude_settings_path=self.claude_settings,
            timeout=3.5,
        )
        self.assertIsInstance(injected_gateway.fetch("codex"), indicator.RuntimeResult)
        self.assertEqual([call["timeout"] for call in injected.calls], [3.5, 3.5])

    def test_bdd_p03_capability_probe_fails_closed_and_caches_failure(self) -> None:
        invalid_help = (
            b"--json-only codex grok claude",
            b"--provider codex grok claude zai",
            b"--json-only --provider codex grok claude za1",
            b"--json-only --provider codex grok claude zai-lookalike",
            b"--json-only --provider codex grok claude zai=false",
            b"--json-only-legacy --provider codex grok claude zai",
            b"--json-only=false --provider codex grok claude zai",
            b"--json-only --provider codex.legacy grok.legacy claude.legacy zai.legacy",
            b"--json-only --provider codex:false grok:false claude:false zai:false",
            b"--json-only --provider codex/v2 grok/v2 claude/v2 zai/v2",
            b"--json-only --provider legacy.codex legacy.grok legacy.claude legacy.zai",
        )
        for index, help_payload in enumerate(invalid_help):
            with self.subTest(case=index):

                class InvalidCapabilityExecutor(RecordingExecutor):
                    def __init__(self, executable: Path, response: bytes) -> None:
                        super().__init__(executable)
                        self.response = response

                    def run(
                        self, request: object, _handle: object | None = None
                    ) -> object:
                        if tuple(request.argv)[1:] == ("usage", "--help"):
                            self.calls.append(
                                {
                                    "runtime": request.runtime,
                                    "argv": tuple(request.argv),
                                    "timeout": request.timeout,
                                    "env_keys": frozenset(request.env_overrides),
                                    "zai_present": False,
                                    "zai_token_matches": False,
                                }
                            )
                            return indicator.ProcessResult(0, self.response, b"")
                        raise AssertionError(
                            "quota call crossed failed capability gate"
                        )

                executor = InvalidCapabilityExecutor(self.binary, help_payload)
                gateway = indicator.ProviderGateway(
                    executor, claude_settings_path=self.claude_settings
                )

                first = gateway.fetch("codex")
                second = gateway.fetch("codex")

                self.assert_runtime_failure(first, "codex")
                self.assert_runtime_failure(second, "codex")
                self.assertEqual(len(executor.calls), 1)

        cached_executor = InvalidCapabilityExecutor(self.binary, invalid_help[0])
        cached_gateway = indicator.ProviderGateway(
            cached_executor, claude_settings_path=self.claude_settings
        )
        for _ in range(100):
            self.assert_runtime_failure(cached_gateway.fetch("codex"), "codex")
        self.assertEqual(len(cached_executor.calls), 1)
        self.assertNotIsInstance(cached_gateway._capability_failure, BaseException)

        class IdentityAwareExecutor(RecordingExecutor):
            def run(self, request: object, _handle: object | None = None) -> object:
                if tuple(request.argv)[1:] == ("usage", "--help"):
                    original = self.HELP
                    self.HELP = self.executable.read_bytes()
                    try:
                        return super().run(request, _handle)
                    finally:
                        self.HELP = original
                return super().run(request, _handle)

        self.binary.write_bytes(b"--provider codex grok claude --json-only")
        self.binary.chmod(0o755)
        executor = IdentityAwareExecutor(self.binary)
        gateway = indicator.ProviderGateway(executor, self.claude_settings)

        self.assert_runtime_failure(gateway.fetch("codex"), "codex")
        replacement = self.root / "replacement-codexbar"
        replacement.write_bytes(RecordingExecutor.HELP)
        replacement.chmod(0o755)
        os.replace(replacement, self.binary)
        self.assertIsInstance(gateway.fetch("codex"), indicator.RuntimeResult)
        help_calls = [
            call for call in executor.calls if call["argv"][1:] == ("usage", "--help")
        ]
        self.assertEqual(len(help_calls), 2)

    def test_bdd_p03_mismatched_reservation_is_consumed_and_released(self) -> None:
        class LedgerExecutor(RecordingExecutor):
            def __init__(self, executable: Path) -> None:
                super().__init__(executable)
                self.released: list[object] = []
                self.cancelled: list[str] = []

            def release(self, handle: object) -> None:
                self.released.append(handle)

            def cancel(self, runtime: str) -> None:
                self.cancelled.append(runtime)

        executor = LedgerExecutor(self.binary)
        gateway = indicator.ProviderGateway(executor, self.claude_settings)
        reservation = gateway.reserve("codex")
        reserved_handle = gateway._reservations[reservation].handle

        mismatch = gateway.fetch("grok", reservation)

        self.assert_runtime_failure(mismatch, "grok")
        self.assertEqual(mismatch.kind, "configuration")
        self.assertEqual(executor.released, [reserved_handle])
        self.assertEqual(executor.cancelled, [])
        self.assertEqual(gateway._reservations, {})

        recovered = gateway.fetch("codex")
        self.assertIsInstance(recovered, indicator.RuntimeResult)

    def test_bdd_p10_cancel_attempts_all_owned_cleanup_after_first_failure(
        self,
    ) -> None:
        class FaultingCancelExecutor(RecordingExecutor):
            def __init__(self, executable: Path) -> None:
                super().__init__(executable)
                self.cancelled: list[str] = []
                self.released: list[object] = []

            def cancel(self, runtime: str) -> None:
                self.cancelled.append(runtime)
                if runtime == "codex":
                    raise PermissionError("injected exact-runtime cleanup failure")

            def release(self, handle: object) -> None:
                self.released.append(handle)

        executor = FaultingCancelExecutor(self.binary)
        gateway = indicator.ProviderGateway(executor, self.claude_settings)
        reservation = gateway.reserve("codex")
        request = gateway._reservations[reservation]
        waiter = object()
        with gateway._capability_state_lock:
            gateway._capability_waiters[waiter] = request
            gateway._capability_owner = ("codex", waiter)

        with self.assertRaises(PermissionError):
            gateway.cancel("codex")

        self.assertEqual(executor.cancelled, ["codex", "capability:codex"])
        self.assertEqual(executor.released, [request.handle])
        self.assertEqual(gateway._reservations, {})
        with gateway._capability_state_lock:
            self.assertEqual(gateway._capability_waiters, {})

    def test_bdd_p03_argv_is_exact_and_source_policy_is_provider_specific(self) -> None:
        cases = (
            ("codex", "", "codex", ()),
            ("codex", "oauth", "codex", ("--source", "oauth")),
            ("grok", "oauth", "grok", ("--source", "auto")),
            ("claude", "oauth", "claude", ("--source", "oauth")),
        )
        for index, (runtime, source, physical, source_args) in enumerate(cases):
            with self.subTest(runtime=runtime, source=source):
                binary = self.root / f"strict fake {index} ; literal" / "codexbar"
                ledger = self.root / f"strict-ledger-{index}.jsonl"
                _write_strict_codexbar(binary)
                executor = indicator.ProcessExecutor(
                    executable=binary,
                    supervisor_path=MODULE_PATH,
                )
                self.addCleanup(executor.close)
                gateway = indicator.ProviderGateway(
                    executor,
                    claude_settings_path=self.claude_settings,
                    source=source,
                )

                with patch.dict(
                    os.environ,
                    {"CODEXBAR_FAKE_LEDGER": str(ledger)},
                    clear=False,
                ):
                    result = gateway.fetch(runtime)

                self.assertIsInstance(result, indicator.RuntimeResult)
                self.assertEqual(
                    json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1]),
                    [
                        "usage",
                        "--provider",
                        physical,
                        "--json-only",
                        "--no-color",
                        *source_args,
                    ],
                )

    def test_bdd_p03_runtime_has_no_shell_execution_path(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        violations: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "os"
                and function.attr in {"popen", "system"}
            ):
                violations.append(f"os.{function.attr}")
            for keyword in node.keywords:
                if keyword.arg != "shell":
                    continue
                if not (
                    isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                ):
                    violations.append("shell")

        self.assertEqual(violations, [])

    def test_bdd_p04_valid_output_becomes_typed_bounded_result(self) -> None:
        payload = [
            {
                "provider": "codex",
                "usage": {
                    "primary": {
                        "usedPercent": 12.5,
                        "windowMinutes": 300,
                        "resetDescription": "in 1 hour",
                    },
                },
                "identity": {"accountEmail": "must-not-cross@example.invalid"},
                "unknown": {"nested": ["must-not-cross"]},
            },
        ]
        gateway, _executor = self.gateway(payloads={"codex": payload})

        result = gateway.fetch("codex")

        self.assertIsInstance(result, indicator.RuntimeResult)
        self.assertEqual(result.runtime, "codex")
        self.assertEqual(result.source, "codex")
        self.assert_window(
            result.usage.primary, percent=13, minutes=300, reset="in 1 hour"
        )
        self.assertIsNone(result.usage.secondary)
        self.assertEqual(result.usage.extras, ())
        self.assertNotIn("accountEmail", repr(result))
        self.assertNotIn("must-not-cross", repr(result))

    def test_bdd_p04_null_window_slots_are_absent(self) -> None:
        payload = [
            {
                "provider": "codex",
                "usage": {
                    "primary": None,
                    "secondary": {"usedPercent": 42, "windowMinutes": 10080},
                    "tertiary": None,
                },
            }
        ]
        gateway, _executor = self.gateway(payloads={"codex": payload})

        result = gateway.fetch("codex")

        self.assertIsInstance(result, indicator.RuntimeResult)
        self.assertIsNone(result.usage.primary)
        self.assert_window(
            result.usage.secondary, percent=42, minutes=10080, reset=None
        )
        self.assertEqual(result.usage.extras, ())

    def test_bdd_p05_executor_failure_never_accepts_stdout_as_success(self) -> None:
        failure = indicator.ProcessFailure(
            kind="exit",
            sanitized_message="codexbar exited with 7",
        )
        gateway, _executor = self.gateway(failures={"codex": failure})

        result = gateway.fetch("codex")

        self.assert_runtime_failure(result, "codex")
        self.assertEqual(result.kind, "exit")
        self.assertEqual(result.message, "codexbar exited with 7")

    def test_bdd_p06_rejects_invalid_json_and_supported_schema_shapes(self) -> None:
        malformed_payloads: dict[str, object] = {
            "invalid-json": b"{not-json",
            "unknown-nan": b'[{"provider":"codex","usage":{},"unknown":NaN}]',
            "unknown-infinity": (
                b'[{"provider":"codex","usage":{},"unknown":Infinity}]'
            ),
            "unknown-negative-infinity": (
                b'[{"provider":"codex","usage":{},"unknown":-Infinity}]'
            ),
            "object-root": {"provider": "codex"},
            "scalar-root": 7,
            "non-object-item": [7],
            "duplicate-provider": [
                {"provider": "codex", "usage": {}},
                {"provider": "codex", "usage": {}},
            ],
            "wrong-provider": [{"provider": "grok", "usage": {}}],
            "error-scalar": [{"provider": "codex", "error": "bad"}],
            "error-message-shape": [{"provider": "codex", "error": {"message": []}}],
            "usage-scalar": [{"provider": "codex", "usage": "bad"}],
            "primary-shape": [{"provider": "codex", "usage": {"primary": []}}],
            "secondary-shape": [{"provider": "codex", "usage": {"secondary": []}}],
            "tertiary-shape": [{"provider": "codex", "usage": {"tertiary": []}}],
            "window-minutes-null": [
                {
                    "provider": "codex",
                    "usage": {"primary": {"usedPercent": 1, "windowMinutes": None}},
                }
            ],
            "reset-description-shape": [
                {
                    "provider": "codex",
                    "usage": {
                        "primary": {
                            "usedPercent": 1,
                            "resetDescription": {"malformed": True},
                        }
                    },
                }
            ],
            "extras-shape": [{"provider": "codex", "usage": {"extraRateWindows": {}}}],
            "extra-item-shape": [
                {"provider": "codex", "usage": {"extraRateWindows": [7]}},
            ],
            "extra-title-shape": [
                {
                    "provider": "codex",
                    "usage": {
                        "extraRateWindows": [{"title": 7, "window": {"usedPercent": 1}}]
                    },
                },
            ],
            "extra-id-shape": [
                {
                    "provider": "codex",
                    "usage": {
                        "extraRateWindows": [
                            {
                                "title": "valid title",
                                "id": {"malformed": True},
                                "window": {"usedPercent": 1},
                            }
                        ]
                    },
                },
            ],
            "extra-tail-shape": [
                {
                    "provider": "codex",
                    "usage": {
                        "extraRateWindows": [
                            {"window": {"usedPercent": index}} for index in range(64)
                        ]
                        + [7]
                    },
                }
            ],
        }
        for name, payload in malformed_payloads.items():
            with self.subTest(case=name):
                gateway, _executor = self.gateway(payloads={"codex": payload})

                result = gateway.fetch("codex")

                self.assert_runtime_failure(result, "codex")

    def test_bdd_p06_discards_unknown_fields_of_any_json_type(self) -> None:
        marker = "unknown-field-marker"
        for value in (None, False, 3, marker, [marker], {"nested": marker}):
            with self.subTest(value=value):
                payload = [
                    {
                        "provider": "codex",
                        "identity": value,
                        "extraUsage": value,
                        "usage": {
                            "primary": {"usedPercent": 1, "unknown": value},
                            "unknown": value,
                        },
                    },
                ]
                gateway, _executor = self.gateway(payloads={"codex": payload})

                result = gateway.fetch("codex")

                self.assertIsInstance(result, indicator.RuntimeResult)
                self.assertNotIn(marker, repr(result))

    def test_bdd_p06_supported_error_and_all_usage_windows_are_typed(self) -> None:
        error_gateway, _executor = self.gateway(
            payloads={
                "codex": [
                    {
                        "provider": "codex",
                        "error": {"kind": "rate_limit", "message": "retry later"},
                    }
                ]
            }
        )
        error = error_gateway.fetch("codex")
        self.assert_runtime_failure(error, "codex")
        self.assertEqual(error.kind, "rate_limit")
        self.assertEqual(error.message, "retry later")

        payload = [
            {
                "provider": "codex",
                "usage": {
                    "primary": {
                        "usedPercent": 1,
                        "windowMinutes": 300,
                        "resetsAt": "2030-01-01T00:00:00Z",
                    },
                    "tertiary": {"usedPercent": 3, "windowMinutes": 43200},
                    "extraRateWindows": [
                        {
                            "title": "Fable",
                            "window": {"usedPercent": 4, "windowMinutes": 60},
                        }
                    ],
                },
            }
        ]
        gateway, _executor = self.gateway(payloads={"codex": payload})
        result = gateway.fetch("codex")
        self.assertIsInstance(result, indicator.RuntimeResult)
        self.assertEqual(
            result.usage.primary.reset_at,
            datetime.fromisoformat("2030-01-01T00:00:00+00:00"),
        )
        self.assertIsNone(result.usage.primary.reset_text)
        self.assertEqual(
            [
                (extra.slot, extra.provider_title, extra.window.percent)
                for extra in result.usage.extras
            ],
            [("tertiary", None, 3), ("extra", "Fable", 4)],
        )

        for resets_at in (
            "",
            "not-a-date",
            "2030-01-01T00:00:00",
            42,
            "x" * 129,
        ):
            with self.subTest(resets_at=resets_at):
                invalid_gateway, _executor = self.gateway(
                    payloads={
                        "codex": [
                            {
                                "provider": "codex",
                                "usage": {
                                    "primary": {
                                        "usedPercent": 1,
                                        "resetsAt": resets_at,
                                    }
                                },
                            }
                        ]
                    }
                )
                invalid = invalid_gateway.fetch("codex")
                self.assert_runtime_failure(invalid, "codex")
                self.assertEqual(invalid.kind, "schema")

                combined_gateway, _executor = self.gateway(
                    payloads={
                        "codex": [
                            {
                                "provider": "codex",
                                "usage": {
                                    "primary": {
                                        "usedPercent": 1,
                                        "resetDescription": "soon",
                                        "resetsAt": resets_at,
                                    }
                                },
                            }
                        ]
                    }
                )
                combined = combined_gateway.fetch("codex")
                self.assert_runtime_failure(combined, "codex")
                self.assertEqual(combined.kind, "schema")

    def test_bdd_v07_absolute_reset_wins_over_description_for_every_provider(
        self,
    ) -> None:
        resets_at = "2030-01-01T00:00:00Z"
        expected = datetime.fromisoformat("2030-01-01T00:00:00+00:00")
        window = {
            "usedPercent": 1,
            "windowMinutes": 300,
            "resetsAt": resets_at,
            "resetDescription": "provider-specific wording",
        }
        for runtime in ("codex", "grok", "claude"):
            with self.subTest(runtime=runtime):
                gateway, _executor = self.gateway(
                    payloads={
                        runtime: [{"provider": runtime, "usage": {"primary": window}}]
                    }
                )
                result = gateway.fetch(runtime)
                self.assertIsInstance(result, indicator.RuntimeResult)
                self.assertEqual(result.usage.primary.reset_at, expected)
                rendered = indicator._format_reset(
                    result.usage.primary,
                    now=datetime.fromisoformat("2029-12-30T00:00:00+00:00"),
                )
                self.assertEqual(rendered, "Jan 1 at 12:00\u202fAM")
                self.assertNotEqual(rendered, "provider-specific wording")

        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": "ZAI-MARKER",
            },
        )
        gateway, _executor = self.gateway(
            payloads={"zai": [{"provider": "zai", "usage": {"primary": window}}]}
        )
        result = gateway.fetch("claude")
        self.assertIsInstance(result, indicator.RuntimeResult)
        self.assertEqual(result.usage.primary.reset_at, expected)
        rendered = indicator._format_reset(
            result.usage.primary,
            now=datetime.fromisoformat("2029-12-30T00:00:00+00:00"),
        )
        self.assertEqual(rendered, "Jan 1 at 12:00\u202fAM")
        self.assertNotEqual(rendered, "provider-specific wording")

    def test_bdd_p06_supported_strings_are_redacted_and_bounded(self) -> None:
        marker = "SECRET-MARKER"
        payload = [
            {
                "provider": "codex",
                "usage": {
                    "primary": {
                        "usedPercent": 1,
                        "resetDescription": f"Authorization: Bearer {marker}",
                    },
                    "extraRateWindows": [
                        {
                            "title": f"token={marker}",
                            "window": {
                                "usedPercent": 2,
                                "resetDescription": "x" * 2000,
                            },
                        }
                    ],
                },
            }
        ]
        gateway, _executor = self.gateway(payloads={"codex": payload})

        result = gateway.fetch("codex")

        self.assertIsInstance(result, indicator.RuntimeResult)
        self.assertNotIn(marker, repr(result))
        self.assertLessEqual(len(result.usage.primary.reset_text), 512)
        self.assertLessEqual(len(result.usage.extras[0].window.reset_text), 512)

    def test_bdd_p07_percent_normalization_rejects_invalid_values(self) -> None:
        for value in (False, True, "2.5", float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                payload = [
                    {
                        "provider": "codex",
                        "usage": {"primary": {"usedPercent": value}},
                    },
                ]
                gateway, _executor = self.gateway(payloads={"codex": payload})

                result = gateway.fetch("codex")

                self.assert_runtime_failure(result, "codex")

    def test_bdd_p07_percent_normalization_rounds_half_up_and_clamps(self) -> None:
        huge = 10**1000
        cases = (
            (-1.1, 0),
            (0, 0),
            (2.49, 2),
            (2.5, 3),
            (99.5, 100),
            (100.1, 100),
            (-1e100, 0),
            (1e100, 100),
            (-huge, 0),
            (huge, 100),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                payload = [
                    {
                        "provider": "codex",
                        "usage": {"primary": {"usedPercent": value}},
                    },
                ]
                gateway, _executor = self.gateway(payloads={"codex": payload})

                result = gateway.fetch("codex")

                self.assertIsInstance(result, indicator.RuntimeResult)
                self.assertEqual(result.usage.primary.percent, expected)

    def test_bdd_p11_sanitizer_redacts_normalizes_controls_and_bounds(self) -> None:
        raw = (
            "provider failed; Authorization: Bearer SECRET; token=SECRET; context kept"
        )
        self.assertEqual(
            indicator.sanitize_diagnostic(raw, sensitive_values=("SECRET",)),
            "provider failed; Authorization: [redacted]; token=[redacted]; context kept",
        )
        for labeled_secret in (
            "token: SECRET",
            "api_key: SECRET",
            "x-api-key: SECRET",
            "access_token=SECRET",
            '"client_secret":"SECRET"',
            "ANTHROPIC_AUTH_TOKEN=SECRET",
            "password: SECRET",
        ):
            with self.subTest(diagnostic=labeled_secret):
                sanitized = indicator.sanitize_diagnostic(labeled_secret)
                self.assertNotIn("SECRET", sanitized)
                self.assertIn("[redacted]", sanitized)
        for diagnostic in (
            "password=correct horse battery staple; context kept",
            'token="correct horse battery staple"; context kept',
            "auth_token='correct\t horse\n battery staple'; context kept",
        ):
            with self.subTest(multiword_diagnostic=diagnostic):
                sanitized = indicator.sanitize_diagnostic(diagnostic)
                self.assertEqual(
                    sanitized.split(";", 1),
                    [sanitized.split("=", 1)[0] + "=[redacted]", " context kept"],
                )
                self.assertNotIn("horse", sanitized)
        split_secret = "REAL-ZAI\t \nTOKEN-MARKER"
        for diagnostic in (
            "token=REAL-ZAI TOKEN-MARKER",
            '"auth_token":"REAL-ZAI TOKEN-MARKER"',
        ):
            with self.subTest(normalized_diagnostic=diagnostic):
                sanitized = indicator.sanitize_diagnostic(
                    diagnostic, sensitive_values=(split_secret,)
                )
                self.assertNotIn("REAL-ZAI", sanitized)
                self.assertNotIn("TOKEN-MARKER", sanitized)
                self.assertIn("[redacted]", sanitized)
        self.assertEqual(indicator.sanitize_diagnostic("a\r\n\tb\x00c"), "a b c")
        unicode_safe = indicator.sanitize_diagnostic("before-\ud800-after")
        unicode_safe.encode("utf-8", errors="strict")
        self.assertNotIn("\ud800", unicode_safe)
        self.assertEqual(indicator.sanitize_diagnostic("x" * 600), "x" * 509 + "...")
        adversarial = "abc-" * (64 * 1024 // 4)
        started = time.monotonic()
        sanitized = indicator.sanitize_diagnostic(adversarial)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertLessEqual(len(sanitized), indicator.MAX_DIAGNOSTIC_CHARS)
        started = time.monotonic()
        marker = "DO-NOT-LEAK-MARKER"
        sanitized = indicator.sanitize_diagnostic(
            marker + "a" * (64 * 1024),
            (marker, "a", "e", "d", "c", "t", "["),
        )
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertLessEqual(len(sanitized), indicator.MAX_DIAGNOSTIC_CHARS)
        self.assertNotIn(marker, sanitized)

    def test_bdd_p11_secret_from_gateway_error_and_unknown_fields_never_crosses(
        self,
    ) -> None:
        marker = "SECRET-MARKER"
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai/endpoint",
                "ANTHROPIC_AUTH_TOKEN": marker,
            },
        )
        failure = indicator.ProcessFailure(
            kind="transport",
            sanitized_message=f"Authorization: Bearer {marker}; token={marker}",
        )
        gateway, _executor = self.gateway(
            failures={"zai": failure},
            expected_zai_token=marker,
        )

        result = gateway.fetch("claude")

        self.assert_runtime_failure(result, "claude")
        self.assertNotIn(marker, result.message)
        self.assertEqual(result.message, "Authorization: [redacted]; token=[redacted]")

    def test_bdd_p11_real_child_ambient_credential_never_crosses_json_boundary(
        self,
    ) -> None:
        marker = "BARE-AMBIENT-CREDENTIAL-MARKER-9f6a"
        database_password = "SENSITIVE-DB-PASSWORD-28f1"
        database_url = f"postgresql://app:{database_password}@localhost/codexbar-test"
        binary = self.root / "ambient-codexbar"
        binary.write_text(
            """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args == ['usage', '--help']:
    print('--provider codex|grok|claude|zai --json-only --no-color')
    raise SystemExit(0)
provider = args[args.index('--provider') + 1]
secret = os.environ['OPENAI_API_KEY']
database_url = os.environ['DATABASE_URL']
database_password = database_url.split(':')[2].split('@')[0]
if os.environ.get('CODEXBAR_TEST_MODE') == 'error':
    payload = [{'provider': provider, 'error': {'kind': secret, 'message': database_url + ' ' + database_password}}]
else:
    payload = [{
        'provider': provider,
        'usage': {
            'primary': {'usedPercent': 5, 'resetDescription': database_url},
            'extraRateWindows': [{
                'title': database_password,
                'window': {'usedPercent': 6},
            }],
        },
    }]
print(json.dumps(payload))
""",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        executor = indicator.ProcessExecutor(
            executable=binary, supervisor_path=MODULE_PATH
        )
        gateway = indicator.ProviderGateway(executor, self.claude_settings)
        try:
            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": marker,
                    "DATABASE_URL": database_url,
                    "CODEXBAR_TEST_MODE": "usage",
                },
                clear=False,
            ):
                usage_result = gateway.fetch("codex")
                os.environ["CODEXBAR_TEST_MODE"] = "error"
                error_result = gateway.fetch("grok")

            self.assertIsInstance(usage_result, indicator.RuntimeResult)
            self.assertNotIn(marker, repr(usage_result))
            self.assertEqual(usage_result.usage.primary.reset_text, "[redacted]")
            self.assertEqual(usage_result.usage.extras[0].provider_title, "[redacted]")
            self.assertNotIn(database_url, repr(usage_result))
            self.assertNotIn(database_password, repr(usage_result))
            self.assert_runtime_failure(error_result, "grok")
            self.assertEqual(error_result.kind, "provider")
            self.assertEqual(error_result.message, "[redacted] [redacted]")
            self.assertNotIn(marker, repr(error_result))
            self.assertNotIn(database_url, repr(error_result))
            self.assertNotIn(database_password, repr(error_result))
        finally:
            gateway.close()

    def test_bdd_p11_bare_zai_token_never_crosses_typed_fields(self) -> None:
        marker = "BARE-ZAI-SECRET-MARKER"
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": marker,
            },
        )
        usage_gateway, _executor = self.gateway(
            payloads={
                "zai": [
                    {
                        "provider": "zai",
                        "usage": {
                            "primary": {
                                "usedPercent": 7,
                                "windowMinutes": 300,
                                "resetDescription": marker,
                            }
                        },
                    }
                ]
            },
            expected_zai_token=marker,
        )

        usage_result = usage_gateway.fetch("claude")

        self.assertIsInstance(usage_result, indicator.RuntimeResult)
        self.assertNotIn(marker, repr(usage_result))
        self.assertEqual(usage_result.usage.primary.reset_text, "[redacted]")

        error_gateway, _executor = self.gateway(
            payloads={
                "zai": [
                    {
                        "provider": "zai",
                        "error": {"kind": marker, "message": "safe failure"},
                    }
                ]
            },
            expected_zai_token=marker,
        )

        error_result = error_gateway.fetch("claude")

        self.assert_runtime_failure(error_result, "claude")
        self.assertNotIn(marker, repr(error_result))
        self.assertEqual(error_result.kind, "provider")

    def test_bdd_p11_process_exception_kind_cannot_cross_typed_boundary(self) -> None:
        marker = "PROCESS-KIND-ZAI-MARKER"
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": marker,
            }
        )

        class KindFailureExecutor(RecordingExecutor):
            def run(self, request: object, handle: object | None = None) -> object:
                result = super().run(request, handle)
                if "--provider" in request.argv:
                    raise indicator.ProcessFailure(marker, "safe failure")
                return result

        executor = KindFailureExecutor(self.binary, expected_zai_token=marker)
        gateway = indicator.ProviderGateway(executor, self.claude_settings)

        result = gateway.fetch("claude")

        self.assert_runtime_failure(result, "claude")
        self.assertEqual(result.kind, "provider")
        self.assertNotIn(marker, repr(result))

        class ReserveFailureExecutor(RecordingExecutor):
            def reserve(self, _runtime: str) -> object:
                raise indicator.ProcessFailure(marker, "safe failure")

        pre_route = indicator.ProviderGateway(
            ReserveFailureExecutor(self.binary), self.claude_settings
        ).fetch("codex")
        self.assert_runtime_failure(pre_route, "codex")
        self.assertEqual(pre_route.kind, "provider")
        self.assertNotIn(marker, repr(pre_route))

    def test_bdd_p11_whitespace_normalized_zai_token_is_redacted_from_real_stderr(
        self,
    ) -> None:
        token = "REAL-ZAI\t  \nTOKEN-MARKER"
        normalized_token = "REAL-ZAI TOKEN-MARKER"
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": token,
            },
        )
        executable = self.root / "stderr-token-provider"
        executable.write_text(
            """#!/usr/bin/env python3
import os
import sys

if sys.argv[1:] == ["usage", "--help"]:
    print("--provider codex grok claude zai --json-only --no-color")
    raise SystemExit(0)
print("provider rejected " + " ".join(os.environ["Z_AI_API_KEY"].split()), file=sys.stderr)
raise SystemExit(9)
""",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        executor = indicator.ProcessExecutor(
            executable=executable,
            supervisor_path=MODULE_PATH,
        )
        self.addCleanup(executor.close)
        gateway = indicator.ProviderGateway(executor, self.claude_settings)

        result = gateway.fetch("claude")

        self.assert_runtime_failure(result, "claude")
        self.assertEqual(result.kind, "exit")
        self.assertNotIn(token, repr(result))
        self.assertNotIn(normalized_token, repr(result))
        self.assertEqual(result.message, "provider rejected [redacted]")

    def test_bdd_p12_ambient_zai_credential_is_removed_or_replaced_without_parent_mutation(
        self,
    ) -> None:
        configured_marker = "CONFIGURED-ZAI-MARKER"
        ambient_marker = "AMBIENT-ZAI-MARKER"
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai/endpoint",
                "ANTHROPIC_AUTH_TOKEN": f"  {configured_marker}  ",
            },
        )
        with patch.dict(
            os.environ,
            {
                "Z_AI_API_KEY": ambient_marker,
                "UNRELATED_API_TOKEN": "UNRELATED-MARKER",
            },
            clear=False,
        ):
            before = dict(os.environ)
            non_zai_records: list[dict[str, object]] = []
            for runtime in ("codex", "grok"):
                gateway, executor = self.gateway(expected_zai_token=configured_marker)
                self.assertIsInstance(gateway.fetch(runtime), indicator.RuntimeResult)
                non_zai_records.extend(executor.quota_calls)

            legacy_settings = self.root / "legacy-claude.json"
            legacy_settings.write_bytes(_canonical_json({"env": {}}))
            legacy_executor = RecordingExecutor(
                self.binary, expected_zai_token=configured_marker
            )
            legacy_gateway = indicator.ProviderGateway(
                legacy_executor,
                claude_settings_path=legacy_settings,
                source="",
            )
            self.assertIsInstance(
                legacy_gateway.fetch("claude"), indicator.RuntimeResult
            )
            non_zai_records.extend(legacy_executor.quota_calls)

            zai_gateway, zai_executor = self.gateway(
                expected_zai_token=configured_marker
            )
            self.assertIsInstance(zai_gateway.fetch("claude"), indicator.RuntimeResult)
            zai_record = zai_executor.quota_calls[0]

            self.assertEqual(os.environ, before)

        self.assertTrue(non_zai_records)
        self.assertTrue(all(not record["zai_present"] for record in non_zai_records))
        self.assertTrue(zai_record["zai_present"])
        self.assertTrue(zai_record["zai_token_matches"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "/proc is Linux-specific")
    def test_bdd_p12_zai_token_exists_only_in_actual_provider_environment(
        self,
    ) -> None:
        marker = "CONFIGURED-CHILD-ONLY-ZAI-MARKER"
        ready = self.root / "provider-ready"
        release = self.root / "provider-release"
        executable = self.root / "gated-zai-provider"
        executable.write_text(
            f"""#!/usr/bin/env python3
import json, os, pathlib, sys, time
if sys.argv[1:] == ["usage", "--help"]:
    print("--provider codex grok claude zai --json-only --no-color")
    raise SystemExit(0)
matching_keys = sorted(
    key for key, value in os.environ.items() if {marker!r} in value
)
matches = matching_keys == ["Z_AI_API_KEY"]
pathlib.Path(os.environ["P12_READY"]).write_text(
    f"{{os.getpid()}} {{int(matches)}} {{','.join(matching_keys)}}", encoding="utf-8"
)
deadline = time.monotonic() + 5
while not pathlib.Path(os.environ["P12_RELEASE"]).exists():
    if time.monotonic() >= deadline:
        raise SystemExit(70)
    time.sleep(0.01)
print(json.dumps([{{
    "provider": "zai",
    "usage": {{"primary": {{"usedPercent": 7, "windowMinutes": 300}}}},
}}]))
""",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": marker,
            }
        )
        executor = indicator.ProcessExecutor(
            executable=executable,
            supervisor_path=MODULE_PATH,
        )
        gateway = indicator.ProviderGateway(executor, self.claude_settings)
        results: list[object] = []
        failures: list[BaseException] = []

        def fetch() -> None:
            try:
                results.append(gateway.fetch("claude"))
            except Exception as exc:  # noqa: BLE001 - retained for assertion
                failures.append(exc)

        worker = threading.Thread(target=fetch, daemon=True)
        try:
            with patch.dict(
                os.environ,
                {
                    "P12_READY": str(ready),
                    "P12_RELEASE": str(release),
                    "ANTHROPIC_AUTH_TOKEN": marker,
                    "DUPLICATE_WRAPPED_TOKEN": f"prefix-{marker}-suffix",
                },
                clear=False,
            ):
                worker.start()
                deadline = time.monotonic() + 5
                while not ready.exists():
                    if time.monotonic() >= deadline:
                        self.fail("provider environment probe did not start")
                    threading.Event().wait(0.01)

                provider_pid, token_matches, matching_keys = ready.read_text(
                    encoding="utf-8"
                ).split()
                self.assertEqual(token_matches, "1")
                self.assertEqual(matching_keys, "Z_AI_API_KEY")
                with executor._lock:
                    active = executor._active["claude"]
                    self.assertIsNotNone(active.process)
                    supervisor_pid = active.process.pid
                self.assertEqual(
                    int(
                        next(
                            line.split()[1]
                            for line in Path(f"/proc/{provider_pid}/status")
                            .read_text(encoding="ascii")
                            .splitlines()
                            if line.startswith("PPid:")
                        )
                    ),
                    supervisor_pid,
                )
                supervisor_environment = Path(
                    f"/proc/{supervisor_pid}/environ"
                ).read_bytes()
                supervisor_command = Path(
                    f"/proc/{supervisor_pid}/cmdline"
                ).read_bytes()
                self.assertFalse(
                    marker.encode() in supervisor_environment,
                    "configured token reached supervisor environment",
                )
                self.assertFalse(
                    b"Z_AI_API_KEY=" in supervisor_environment,
                    "credential key reached supervisor environment",
                )
                self.assertFalse(
                    marker.encode() in supervisor_command,
                    "configured token reached supervisor argv",
                )
                release.touch()
                worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(results), 1)
            self.assertIsInstance(results[0], indicator.RuntimeResult)
        finally:
            release.touch()
            gateway.close()
            worker.join(timeout=5)


class ZaiRoutingContractTests(_GatewayTestCase):
    def test_bdd_z01_exact_https_origin_routes_to_zai_regardless_of_path(self) -> None:
        valid_origins = (
            "https://api.z.ai",
            "https://api.z.ai/",
            "https://api.z.ai/api/monitor/usage",
            "https://api.z.ai:443/custom",
            "HTTPS://API.Z.AI:443/custom",
        )
        for origin in valid_origins:
            with self.subTest(origin=origin):
                self.write_claude_settings(
                    {
                        "ANTHROPIC_BASE_URL": origin,
                        "ANTHROPIC_AUTH_TOKEN": "ZAI-MARKER",
                    },
                )
                gateway, executor = self.gateway(expected_zai_token="ZAI-MARKER")

                result = gateway.fetch("claude")

                self.assertIsInstance(result, indicator.RuntimeResult)
                self.assertEqual(executor.quota_calls[0]["argv"][3], "zai")

    def test_bdd_z02_origin_lookalikes_never_route_to_zai_or_receive_its_token(
        self,
    ) -> None:
        rejected_origins = (
            "http://api.z.ai/",
            "https://user@api.z.ai/",
            "https://api.z.ai:444/",
            "https://api.z.ai.evil/",
            "https://evilapi.z.ai/",
            "https:api.z.ai",
            "not a url",
            "https://example.invalid/",
        )
        for origin in rejected_origins:
            with self.subTest(origin=origin):
                self.write_claude_settings(
                    {
                        "ANTHROPIC_BASE_URL": origin,
                        "ANTHROPIC_AUTH_TOKEN": "ZAI-MARKER",
                    },
                )
                with patch.dict(
                    os.environ, {"Z_AI_API_KEY": "AMBIENT-MARKER"}, clear=False
                ):
                    gateway, executor = self.gateway(expected_zai_token="ZAI-MARKER")

                    result = gateway.fetch("claude")

                self.assertIsInstance(result, indicator.RuntimeResult)
                record = executor.quota_calls[0]
                self.assertEqual(record["argv"][3], "claude")
                self.assertFalse(record["zai_present"])

    def test_bdd_z03_absent_or_non_zai_claude_settings_uses_legacy_provider(
        self,
    ) -> None:
        settings_values = (
            None,
            {"env": {}},
            {"env": {"ANTHROPIC_BASE_URL": "https://example.invalid"}},
        )
        for value in settings_values:
            with self.subTest(value=value):
                if self.claude_settings.exists():
                    self.claude_settings.unlink()
                if value is not None:
                    self.claude_settings.parent.mkdir(parents=True, exist_ok=True)
                    self.claude_settings.write_bytes(_canonical_json(value))
                gateway, executor = self.gateway()

                result = gateway.fetch("claude")

                self.assertIsInstance(result, indicator.RuntimeResult)
                self.assertEqual(result.runtime, "claude")
                self.assertEqual(result.source, "claude")
                self.assertEqual(executor.quota_calls[0]["argv"][3], "claude")

    def test_bdd_z04_trimmed_file_token_reaches_only_zai_child_environment(
        self,
    ) -> None:
        marker = "VALIDATED-ZAI-MARKER"
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai/path",
                "ANTHROPIC_AUTH_TOKEN": f" \t{marker}\n ",
            },
        )
        gateway, executor = self.gateway(expected_zai_token=marker)

        result = gateway.fetch("claude")

        self.assertIsInstance(result, indicator.RuntimeResult)
        record = executor.quota_calls[0]
        self.assertEqual(record["runtime"], "claude")
        self.assertTrue(record["zai_token_matches"])
        self.assertNotIn(marker, " ".join(record["argv"]))

    def test_bdd_z05_invalid_settings_or_zai_token_fails_without_subprocess(
        self,
    ) -> None:
        invalid_files = (
            b"{",
            b"\xff",
            b"[]",
            b" " * (64 * 1024 + 1),
        )
        for index, contents in enumerate(invalid_files):
            with self.subTest(file_case=index):
                self.claude_settings.parent.mkdir(parents=True, exist_ok=True)
                self.claude_settings.write_bytes(contents)
                gateway, executor = self.gateway()

                result = gateway.fetch("claude")

                self.assert_runtime_failure(result, "claude")
                self.assertEqual(executor.calls, [])

        if self.claude_settings.exists():
            self.claude_settings.unlink()
        self.claude_settings.mkdir(parents=True)
        gateway, executor = self.gateway()
        self.assert_runtime_failure(gateway.fetch("claude"), "claude")
        self.assertEqual(executor.calls, [])

        self.claude_settings.rmdir()
        invalid_tokens = (None, 7, "", " \t\n", "secret\x00suffix", "\ud800")
        for token in invalid_tokens:
            with self.subTest(token=token):
                environment: dict[str, object] = {
                    "ANTHROPIC_BASE_URL": "https://api.z.ai"
                }
                if token is not None:
                    environment["ANTHROPIC_AUTH_TOKEN"] = token
                self.write_claude_settings(environment)
                gateway, executor = self.gateway()

                result = gateway.fetch("claude")

                self.assert_runtime_failure(result, "claude")
                self.assertEqual(executor.calls, [])

    def test_bdd_z05_non_regular_settings_never_block_refresh_shutdown(self) -> None:
        self.claude_settings.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self.claude_settings)
        gateway, executor = self.gateway()
        result_holder: list[object] = []
        finished = threading.Event()

        def fetch() -> None:
            try:
                result_holder.append(gateway.fetch("claude"))
            finally:
                finished.set()

        worker = threading.Thread(target=fetch, daemon=False)
        started = time.monotonic()
        worker.start()
        self.assertTrue(finished.wait(0.5))
        gateway.close()
        worker.join(timeout=0.5)

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result_holder), 1)
        self.assert_runtime_failure(result_holder[0], "claude")
        self.assertEqual(executor.calls, [])
        self.assertTrue(self.claude_settings.exists())

    def test_bdd_z06_command_contract_maps_physical_zai_to_logical_claude(self) -> None:
        marker = "ZAI-MARKER"
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": marker,
            },
        )
        payload = [
            {
                "provider": "zai",
                "usage": {
                    "primary": {"usedPercent": 8, "windowMinutes": 300},
                    "secondary": {"usedPercent": 91, "windowMinutes": 10080},
                },
            },
        ]
        gateway, executor = self.gateway(
            payloads={"zai": payload},
            expected_zai_token=marker,
            source="oauth",
        )

        result = gateway.fetch("claude")

        self.assertIsInstance(result, indicator.RuntimeResult)
        self.assertEqual(result.runtime, "claude")
        self.assertEqual(result.source, "zai")
        self.assertEqual(
            executor.quota_calls[0]["argv"],
            (
                str(self.binary),
                "usage",
                "--provider",
                "zai",
                "--json-only",
                "--no-color",
            ),
        )

    def test_bdd_z06_real_gateway_uses_cli_without_wrapper_network_transport(
        self,
    ) -> None:
        marker = "ZAI-TEST-MARKER"
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai/path",
                "ANTHROPIC_AUTH_TOKEN": marker,
            },
        )
        binary = self.root / "strict zai fake ; literal" / "codexbar"
        ledger = self.root / "zai-cli-ledger.jsonl"
        _write_strict_codexbar(binary)
        executor = indicator.ProcessExecutor(
            executable=binary,
            supervisor_path=MODULE_PATH,
        )
        self.addCleanup(executor.close)
        gateway = indicator.ProviderGateway(
            executor,
            claude_settings_path=self.claude_settings,
            source="oauth",
        )

        with (
            patch.dict(
                os.environ,
                {"CODEXBAR_FAKE_LEDGER": str(ledger)},
                clear=False,
            ),
            patch(
                "socket.socket",
                side_effect=AssertionError("wrapper socket is forbidden"),
            ),
        ):
            result = gateway.fetch("claude")

        self.assertIsInstance(result, indicator.RuntimeResult)
        self.assertEqual(result.runtime, "claude")
        self.assertEqual(result.source, "zai")
        self.assertEqual(
            json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1]),
            ["usage", "--provider", "zai", "--json-only", "--no-color"],
        )

    def test_bdd_z06_runtime_has_no_custom_http_or_dynamic_curl_path(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported_modules: set[str] = set()
        curl_constants: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
                imported_modules.update(
                    f"{node.module}.{alias.name}" for alias in node.names
                )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                imported_modules.add(node.args[0].value)
            elif isinstance(node, ast.Constant) and node.value == "curl":
                curl_constants.add(node.value)

        self.assertTrue(
            imported_modules.isdisjoint(
                {"urllib.request", "requests", "httpx", "socket"}
            ),
            imported_modules,
        )
        self.assertEqual(curl_constants, set())

    def test_bdd_z07_classifies_windows_by_duration_across_slots_and_order(
        self,
    ) -> None:
        five_hour = {"usedPercent": 11, "windowMinutes": 300}
        period = {"usedPercent": 89, "windowMinutes": 10080}
        mcp = {"usedPercent": 55, "windowMinutes": 43200, "resetDescription": "MCP"}
        locations = ("primary", "secondary", "tertiary", "extraRateWindows")
        for five_location, period_location in itertools.permutations(locations, 2):
            with self.subTest(five=five_location, period=period_location):
                self.write_claude_settings(
                    {
                        "ANTHROPIC_BASE_URL": "https://api.z.ai",
                        "ANTHROPIC_AUTH_TOKEN": "ZAI-MARKER",
                    },
                )
                usage: dict[str, object] = {}
                extras: list[dict[str, object]] = []
                for location, window in (
                    (five_location, five_hour),
                    (period_location, period),
                ):
                    if location == "extraRateWindows":
                        extras.append(
                            {"id": location, "title": location, "window": window}
                        )
                    else:
                        usage[location] = window
                remaining = next(
                    location
                    for location in locations
                    if location not in {five_location, period_location}
                )
                if remaining == "extraRateWindows":
                    extras.append({"id": "mcp", "title": "MCP", "window": mcp})
                else:
                    usage[remaining] = mcp
                if extras:
                    usage["extraRateWindows"] = extras
                payload = [{"provider": "zai", "usage": usage}]
                gateway, _executor = self.gateway(payloads={"zai": payload})

                result = gateway.fetch("claude")

                self.assertIsInstance(result, indicator.RuntimeResult)
                self.assert_window(
                    result.usage.primary, percent=11, minutes=300, reset=None
                )
                self.assert_window(
                    result.usage.secondary, percent=89, minutes=10080, reset=None
                )

    def test_bdd_z07_rejects_non_integer_or_non_finite_window_minutes(self) -> None:
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": "ZAI-MARKER",
            },
        )
        invalid_minutes = (True, "300", 300.0, float("nan"), float("inf"), None)
        for value in invalid_minutes:
            with self.subTest(window_minutes=value):
                payload = [
                    {
                        "provider": "zai",
                        "usage": {
                            "primary": {"usedPercent": 20, "windowMinutes": value},
                        },
                    },
                ]
                gateway, _executor = self.gateway(payloads={"zai": payload})

                result = gateway.fetch("claude")

                self.assert_runtime_failure(result, "claude")
                expected_kind = (
                    "parse"
                    if isinstance(value, float) and not math.isfinite(value)
                    else "schema"
                )
                self.assertEqual(result.kind, expected_kind)

    def test_bdd_z07_null_window_slots_are_absent(self) -> None:
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": "ZAI-MARKER",
            },
        )
        payload = [
            {
                "provider": "zai",
                "usage": {
                    "primary": None,
                    "secondary": {
                        "usedPercent": 73,
                        "windowMinutes": 10080,
                    },
                    "tertiary": None,
                },
            }
        ]
        gateway, _executor = self.gateway(payloads={"zai": payload})

        result = gateway.fetch("claude")

        self.assertIsInstance(result, indicator.RuntimeResult)
        self.assertIsNone(result.usage.primary)
        self.assert_window(
            result.usage.secondary, percent=73, minutes=10080, reset=None
        )

    def test_bdd_z07_deduplicates_identical_horizon_and_rejects_conflicts(self) -> None:
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": "ZAI-MARKER",
            },
        )
        identical = {"usedPercent": 20, "windowMinutes": 300}
        deduplicated_payload = [
            {
                "provider": "zai",
                "usage": {"primary": identical, "tertiary": dict(identical)},
            },
        ]
        gateway, _executor = self.gateway(payloads={"zai": deduplicated_payload})
        result = gateway.fetch("claude")
        self.assertIsInstance(result, indicator.RuntimeResult)
        self.assert_window(result.usage.primary, percent=20, minutes=300, reset=None)
        self.assertIsNone(result.usage.secondary)

        conflicting_payload = [
            {
                "provider": "zai",
                "usage": {
                    "primary": identical,
                    "tertiary": {"usedPercent": 21, "windowMinutes": 300},
                },
            },
        ]
        gateway, _executor = self.gateway(payloads={"zai": conflicting_payload})
        self.assert_runtime_failure(gateway.fetch("claude"), "claude")

        ignored_prefix = [{"window": {"windowMinutes": 60}} for _index in range(65)]
        late_payload = [
            {
                "provider": "zai",
                "usage": {
                    "extraRateWindows": [
                        *ignored_prefix,
                        {"window": {"usedPercent": 33, "windowMinutes": 300}},
                    ]
                },
            }
        ]
        gateway, _executor = self.gateway(payloads={"zai": late_payload})
        late_result = gateway.fetch("claude")
        self.assertIsInstance(late_result, indicator.RuntimeResult)
        self.assert_window(
            late_result.usage.primary, percent=33, minutes=300, reset=None
        )

        late_conflict_payload = [
            {
                "provider": "zai",
                "usage": {
                    "primary": identical,
                    "extraRateWindows": [
                        *ignored_prefix,
                        {"window": {"usedPercent": 34, "windowMinutes": 300}},
                    ],
                },
            }
        ]
        gateway, _executor = self.gateway(payloads={"zai": late_conflict_payload})
        self.assert_runtime_failure(gateway.fetch("claude"), "claude")

    def test_bdd_z08_missing_reset_keeps_recognized_quota(self) -> None:
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": "ZAI-MARKER",
            },
        )
        payload = [
            {
                "provider": "zai",
                "usage": {"primary": {"usedPercent": 42, "windowMinutes": 300}},
            },
        ]
        gateway, _executor = self.gateway(payloads={"zai": payload})

        result = gateway.fetch("claude")

        self.assertIsInstance(result, indicator.RuntimeResult)
        self.assert_window(result.usage.primary, percent=42, minutes=300, reset=None)
        self.assertIsNone(result.usage.secondary)

    def test_bdd_z08_zai_prefers_authoritative_reset_timestamp(self) -> None:
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": "ZAI-MARKER",
            },
        )
        payload = [
            {
                "provider": "zai",
                "usage": {
                    "primary": {
                        "usedPercent": 42,
                        "windowMinutes": 300,
                        "resetDescription": "5 hours window",
                    },
                    "secondary": {
                        "usedPercent": 73,
                        "windowMinutes": 10080,
                        "resetDescription": "1 week window",
                        "resetsAt": "2030-01-02T03:04:05Z",
                    },
                },
            },
        ]
        gateway, _executor = self.gateway(payloads={"zai": payload})

        result = gateway.fetch("claude")

        self.assertIsInstance(result, indicator.RuntimeResult)
        self.assert_window(
            result.usage.primary,
            percent=42,
            minutes=300,
            reset="5 hours window",
        )
        self.assertEqual(result.usage.secondary.percent, 73)
        self.assertEqual(result.usage.secondary.window_minutes, 10080)
        self.assertEqual(
            result.usage.secondary.reset_at,
            datetime.fromisoformat("2030-01-02T03:04:05+00:00"),
        )
        self.assertIsNone(result.usage.secondary.reset_text)

    def test_bdd_z09_mcp_and_unrecognized_windows_do_not_cross_gateway(self) -> None:
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": "ZAI-MARKER",
            },
        )
        payload = [
            {
                "provider": "zai",
                "usage": {
                    "primary": {"usedPercent": 4, "windowMinutes": 300},
                    "secondary": {
                        "usedPercent": 77,
                        "windowMinutes": 43200,
                        "resetDescription": "MCP TIME monthly",
                    },
                    "extraRateWindows": [
                        {
                            "title": "MCP quota",
                            "window": {"usedPercent": 66, "windowMinutes": 1440},
                        },
                    ],
                },
            },
        ]
        gateway, _executor = self.gateway(payloads={"zai": payload})

        result = gateway.fetch("claude")

        self.assertIsInstance(result, indicator.RuntimeResult)
        self.assert_window(result.usage.primary, percent=4, minutes=300, reset=None)
        self.assertIsNone(result.usage.secondary)
        self.assertEqual(result.usage.extras, ())
        exported = repr(result).lower()
        self.assertNotRegex(exported, r"\bmcp\b")
        self.assertNotRegex(exported, r"\bmonthly\b")
        self.assertNotRegex(exported, r"\btime\b")

    def test_bdd_z10_gateway_local_failure_is_scoped_to_logical_claude(self) -> None:
        self.write_claude_settings(
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
                "ANTHROPIC_AUTH_TOKEN": "ZAI-MARKER",
            },
        )
        failure = indicator.ProcessFailure(
            kind="timeout", sanitized_message="provider timeout"
        )
        gateway, executor = self.gateway(failures={"zai": failure})

        result = gateway.fetch("claude")

        self.assert_runtime_failure(result, "claude")
        self.assertEqual(result.kind, "timeout")
        self.assertEqual(result.message, "provider timeout")
        self.assertEqual([call["runtime"] for call in executor.quota_calls], ["claude"])


if __name__ == "__main__":
    unittest.main()
