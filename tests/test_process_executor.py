from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import MODULE_PATH, load_indicator

indicator = load_indicator("codexbar_process_tests")


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path.name}")
        time.sleep(0.01)


def _wait_for_process_exit(pid: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while Path(f"/proc/{pid}").exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"process {pid} survived")
        time.sleep(0.01)


def _parent_pid(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
        if line.startswith("PPid:"):
            return int(line.split()[1])
    raise AssertionError(f"process {pid} has no parent")


def _terminate_popen_group(process: subprocess.Popen[object]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _terminate_owned_groups_from_pid_file(path: Path, marker: str) -> None:
    if not path.is_file():
        return
    for raw_pid in path.read_text(encoding="utf-8").split():
        try:
            pid = int(raw_pid)
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            pgid = os.getpgid(pid)
        except (FileNotFoundError, ProcessLookupError, ValueError):
            continue
        if marker.encode() not in cmdline:
            continue
        try:
            leader_cmdline = Path(f"/proc/{pgid}/cmdline").read_bytes()
            if pgid != os.getpgrp() and marker.encode() in leader_cmdline:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError):
            pass


class ProcessExecutorRedTests(unittest.TestCase):
    def make_executor(self, **overrides):
        options = {
            "supervisor_path": MODULE_PATH,
            "stdout_cap": 1024,
            "stderr_cap": 512,
            "read_chunk": 128,
        }
        options.update(overrides)
        return indicator.ProcessExecutor(**options)

    def test_bdd_p12_complete_child_environment_is_bounded_and_recovers(self) -> None:
        executor = self.make_executor()
        request = indicator.ProcessRequest(
            runtime="codex",
            argv=(sys.executable, "-c", "print('must-not-run')"),
            env_overrides={},
            timeout=5.0,
        )
        try:
            with (
                patch.dict(
                    os.environ,
                    {"OVERSIZED_PROVIDER_ENV": "x" * indicator.MAX_CHILD_ENV_BYTES},
                    clear=False,
                ),
                self.assertRaises(indicator.ProcessFailure) as caught,
            ):
                executor.run(request)
            self.assertEqual(caught.exception.kind, "configuration")
            with executor._lock:
                self.assertEqual(executor._active, {})
                self.assertEqual(executor._pending, {})

            for overrides in (
                {"BAD_TOKEN": "\ud800"},
                {"BAD_\ud800_TOKEN": "value"},
            ):
                with (
                    self.subTest(overrides=repr(overrides)),
                    self.assertRaises(indicator.ProcessFailure) as caught,
                ):
                    executor.run(
                        indicator.ProcessRequest(
                            runtime="codex",
                            argv=(sys.executable, "-c", "print('must-not-run')"),
                            env_overrides=overrides,
                            timeout=5.0,
                        )
                    )
                self.assertEqual(caught.exception.kind, "configuration")
                self.assertNotIn("Traceback", caught.exception.sanitized_message)

            recovered = executor.run(
                indicator.ProcessRequest(
                    runtime="codex",
                    argv=(sys.executable, "-c", "print('recovered')"),
                    env_overrides={},
                    timeout=5.0,
                )
            )
            self.assertEqual(recovered.stdout, b"recovered\n")
        finally:
            executor.close()

    def test_bdd_p12_short_token_collision_preserves_noncredential_environment(
        self,
    ) -> None:
        executor = self.make_executor()
        parent_before = dict(os.environ)
        program = (
            "import json, os; "
            "print(json.dumps({"
            "'path': os.environ.get('PATH'), "
            "'plain': os.environ.get('COLLIDING_VALUE'), "
            "'anthropic': 'ANTHROPIC_AUTH_TOKEN' in os.environ, "
            "'wrapped': 'WRAPPED_API_TOKEN' in os.environ, "
            "'zai': os.environ.get('Z_AI_API_KEY') == 'bin'"
            "}))"
        )
        try:
            with patch.dict(
                os.environ,
                {
                    "PATH": "/custom/bin:/usr/bin",
                    "COLLIDING_VALUE": "combine-value",
                    "ANTHROPIC_AUTH_TOKEN": "bin",
                    "WRAPPED_API_TOKEN": "prefix-bin-suffix",
                },
                clear=False,
            ):
                expected_parent = dict(os.environ)
                result = executor.run(
                    indicator.ProcessRequest(
                        runtime="claude",
                        argv=(sys.executable, "-c", program),
                        env_overrides={"Z_AI_API_KEY": "bin"},
                        timeout=5.0,
                    )
                )
                self.assertEqual(os.environ, expected_parent)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["path"], "/custom/bin:/usr/bin")
            self.assertEqual(payload["plain"], "combine-value")
            self.assertFalse(payload["anthropic"])
            self.assertFalse(payload["wrapped"])
            self.assertTrue(payload["zai"])
        finally:
            executor.close()
        self.assertEqual(os.environ, parent_before)

    def test_bdd_p08_streaming_stdout_limit_cancels_before_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            pid_file = Path(raw_tmp) / "pid"
            child = Path(raw_tmp) / "stream.py"
            _write_executable(
                child,
                """#!/usr/bin/env python3
import os, sys
with open(sys.argv[1], 'w', encoding='utf-8') as stream:
    stream.write(str(os.getpid()))
    stream.flush()
    os.fsync(stream.fileno())
while True:
    os.write(1, b'x' * 256)
""",
            )
            executor = self.make_executor()
            try:
                request = indicator.ProcessRequest(
                    runtime="codex",
                    argv=(sys.executable, str(child), str(pid_file)),
                    env_overrides={},
                    timeout=10.0,
                )

                started = time.monotonic()
                read_sizes: list[int] = []
                retained_high_water: list[int] = []
                real_read = os.read

                class TrackingBytearray(bytearray):
                    def extend(self, value: object) -> None:
                        super().extend(value)
                        retained_high_water.append(len(self))

                def bounded_read(file_descriptor: int, size: int) -> bytes:
                    if pid_file.exists():
                        read_sizes.append(size)
                    return real_read(file_descriptor, size)

                with (
                    patch.object(
                        indicator, "bytearray", TrackingBytearray, create=True
                    ),
                    patch.object(indicator.os, "read", side_effect=bounded_read),
                    self.assertRaises(indicator.ProcessFailure) as caught,
                ):
                    executor.run(request)
                elapsed = time.monotonic() - started

                self.assertEqual(caught.exception.kind, "output_limit")
                self.assertTrue(read_sizes)
                self.assertLessEqual(max(read_sizes), 128)
                self.assertTrue(retained_high_water)
                self.assertLessEqual(max(retained_high_water), 1024)
                self.assertLessEqual(len(caught.exception.sanitized_message), 512)
                self.assertLess(
                    elapsed, 5, "output overflow was not cancelled promptly"
                )
                _wait_for_file(pid_file)
                _wait_for_process_exit(int(pid_file.read_text(encoding="utf-8")))
            finally:
                try:
                    executor.close()
                finally:
                    _terminate_owned_groups_from_pid_file(pid_file, str(child))

    def test_bdd_p08_streaming_stderr_limit_is_independent(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            pid_file = Path(raw_tmp) / "pid"
            child = Path(raw_tmp) / "stream.py"
            _write_executable(
                child,
                """#!/usr/bin/env python3
import os, sys
with open(sys.argv[1], 'w', encoding='utf-8') as stream:
    stream.write(str(os.getpid()))
    stream.flush()
    os.fsync(stream.fileno())
while True:
    os.write(2, b'e' * 256)
""",
            )
            executor = self.make_executor()
            try:
                request = indicator.ProcessRequest(
                    runtime="grok",
                    argv=(sys.executable, str(child), str(pid_file)),
                    env_overrides={},
                    timeout=10.0,
                )

                started = time.monotonic()
                with self.assertRaises(indicator.ProcessFailure) as caught:
                    executor.run(request)
                elapsed = time.monotonic() - started

                self.assertEqual(caught.exception.kind, "output_limit")
                self.assertLessEqual(len(caught.exception.sanitized_message), 512)
                self.assertLess(
                    elapsed, 5, "output overflow was not cancelled promptly"
                )
                _wait_for_file(pid_file)
                _wait_for_process_exit(int(pid_file.read_text(encoding="utf-8")))
            finally:
                try:
                    executor.close()
                finally:
                    _terminate_owned_groups_from_pid_file(pid_file, str(child))

    def test_bdd_p05_real_process_failures_never_become_usage_success(self) -> None:
        cases = ("nonzero-with-valid-json", "signal", "empty", "invalid-utf8")
        for mode in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                ledger = tmp / "ledger.jsonl"
                child = tmp / f"codexbar-{mode}"
                _write_executable(
                    child,
                    f"""#!/usr/bin/env python3
import json, os, signal, sys
args = sys.argv[1:]
with open(os.environ['CODEXBAR_FAKE_LEDGER'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(args) + '\\n')
if args == ['usage', '--help']:
    print('--provider codex|grok|claude|zai --json-only --no-color')
    raise SystemExit(0)
if args != ['usage', '--provider', 'codex', '--json-only', '--no-color']:
    raise SystemExit(64)
mode = {mode!r}
if mode == 'nonzero-with-valid-json':
    print('[{{"provider":"codex","usage":{{"primary":{{"usedPercent":9}}}}}}]')
    raise SystemExit(7)
if mode == 'signal':
    os.kill(os.getpid(), signal.SIGTERM)
if mode == 'invalid-utf8':
    os.write(1, bytes([255]))
""",
                )
                executor = indicator.ProcessExecutor(
                    executable=child,
                    supervisor_path=MODULE_PATH,
                )
                self.addCleanup(executor.close)
                gateway = indicator.ProviderGateway(
                    executor,
                    claude_settings_path=tmp / "missing-claude-settings.json",
                    source="",
                )

                try:
                    with patch.dict(
                        os.environ,
                        {"CODEXBAR_FAKE_LEDGER": str(ledger)},
                        clear=False,
                    ):
                        result = gateway.fetch("codex")

                    self.assertIsInstance(result, indicator.RuntimeFailure)
                    self.assertEqual(result.runtime, "codex")
                    self.assertLessEqual(len(result.message), 512)
                    self.assertEqual(
                        json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1]),
                        [
                            "usage",
                            "--provider",
                            "codex",
                            "--json-only",
                            "--no-color",
                        ],
                    )
                finally:
                    executor.close()

    def test_bdd_p11_bare_ambient_credential_is_redacted_from_stderr(self) -> None:
        marker = "BARE-AMBIENT-CREDENTIAL-MARKER-9f6a"
        database_password = "SENSITIVE-DB-PASSWORD-28f1"
        database_url = f"postgresql://app:{database_password}@localhost/codexbar-test"
        common_secrets = {
            "PGPASSWORD": "PG-PASS-LEAK-29c1",
            "MYSQL_PWD": "MYSQL-PASS-LEAK-82e4",
            "SSH_PRIVATE_KEY": "PRIVATE-KEY-LEAK-71d8",
        }
        executor = self.make_executor()
        try:
            with (
                patch.dict(
                    os.environ,
                    {
                        "OPENAI_API_KEY": marker,
                        "DATABASE_URL": database_url,
                        **common_secrets,
                    },
                    clear=False,
                ),
                self.assertRaises(indicator.ProcessFailure) as caught,
            ):
                executor.run(
                    indicator.ProcessRequest(
                        runtime="codex",
                        argv=(
                            sys.executable,
                            "-c",
                            "import os,sys; names=['OPENAI_API_KEY','DATABASE_URL','PGPASSWORD','MYSQL_PWD','SSH_PRIVATE_KEY']; sys.stderr.write(' '.join(os.environ[name] for name in names) + ' ' + os.environ['DATABASE_URL'].split(':')[2].split('@')[0]); sys.exit(9)",
                        ),
                        env_overrides={},
                        timeout=5.0,
                    )
                )
            self.assertEqual(caught.exception.kind, "exit")
            self.assertNotIn(marker, caught.exception.sanitized_message)
            self.assertNotIn(database_url, caught.exception.sanitized_message)
            self.assertNotIn(database_password, caught.exception.sanitized_message)
            for secret in common_secrets.values():
                self.assertNotIn(secret, caught.exception.sanitized_message)
            self.assertEqual(
                caught.exception.sanitized_message,
                "[redacted] [redacted] [redacted] [redacted] [redacted] [redacted]",
            )
        finally:
            executor.close()

    def test_bdd_p09_timeout_reaps_child_and_grandchild(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            pids = tmp / "pids"
            child = tmp / "tree.py"
            _write_executable(
                child,
                """#!/usr/bin/env python3
import os, subprocess, sys, time
grandchild = subprocess.Popen([
    sys.executable, '-c', 'import time; time.sleep(60)', __file__,
], start_new_session=True)
with open(sys.argv[1], 'w', encoding='utf-8') as stream:
    stream.write(f'{os.getpid()} {grandchild.pid}')
    stream.flush()
    os.fsync(stream.fileno())
time.sleep(60)
""",
            )
            executor = self.make_executor()
            try:
                request = indicator.ProcessRequest(
                    runtime="claude",
                    argv=(sys.executable, str(child), str(pids)),
                    env_overrides={},
                    timeout=2.0,
                )

                with self.assertRaises(indicator.ProcessFailure) as caught:
                    executor.run(request)

                self.assertEqual(caught.exception.kind, "timeout")
                _wait_for_file(pids)
                for raw_pid in pids.read_text(encoding="utf-8").split():
                    _wait_for_process_exit(int(raw_pid))
            finally:
                try:
                    executor.close()
                finally:
                    _terminate_owned_groups_from_pid_file(pids, str(child))

    def test_bdd_p10_close_cancels_active_invocation_and_rejects_new_work(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            ready = tmp / "ready"
            child = tmp / "block.py"
            _write_executable(
                child,
                """#!/usr/bin/env python3
import os, sys, time
with open(sys.argv[1], 'w', encoding='utf-8') as stream:
    stream.write(str(os.getpid()))
    stream.flush()
    os.fsync(stream.fileno())
time.sleep(60)
""",
            )
            executor = self.make_executor()
            try:
                request = indicator.ProcessRequest(
                    runtime="codex",
                    argv=(sys.executable, str(child), str(ready)),
                    env_overrides={},
                    timeout=30.0,
                )
                failures: list[BaseException] = []

                def run() -> None:
                    try:
                        executor.run(request)
                    except Exception as exc:  # noqa: BLE001 - captured for test thread
                        failures.append(exc)

                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                _wait_for_file(ready)
                executor.close()
                worker.join(timeout=5)

                self.assertFalse(worker.is_alive())
                self.assertEqual(len(failures), 1)
                self.assertIsInstance(failures[0], indicator.ProcessFailure)
                self.assertEqual(failures[0].kind, "cancelled")
                _wait_for_process_exit(int(ready.read_text(encoding="utf-8")))
                with self.assertRaises(indicator.ProcessFailure) as caught:
                    executor.run(request)
                self.assertEqual(caught.exception.kind, "closed")
            finally:
                try:
                    executor.close()
                finally:
                    _terminate_owned_groups_from_pid_file(ready, str(child))

    def test_bdd_p10_selector_setup_failure_reaps_and_releases_reservation(
        self,
    ) -> None:
        executor = self.make_executor()
        spawned: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def record_spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            return process

        request = indicator.ProcessRequest(
            runtime="codex",
            argv=(sys.executable, "-c", "import time; time.sleep(60)"),
            env_overrides={},
            timeout=30.0,
        )
        try:
            with (
                patch.object(indicator.subprocess, "Popen", side_effect=record_spawn),
                patch.object(
                    indicator.selectors,
                    "DefaultSelector",
                    side_effect=OSError("injected selector setup failure"),
                ),
                self.assertRaises(indicator.ProcessFailure) as caught,
            ):
                executor.run(request)

            self.assertEqual(caught.exception.kind, "io")
            self.assertEqual(len(spawned), 1)
            spawned[0].wait(timeout=5)
            _wait_for_process_exit(spawned[0].pid)

            recovered = executor.run(
                indicator.ProcessRequest(
                    runtime="codex",
                    argv=(sys.executable, "-c", "print('recovered')"),
                    env_overrides={},
                    timeout=5.0,
                )
            )
            self.assertEqual(recovered.stdout, b"recovered\n")

            with self.assertRaises(indicator.ProcessFailure) as invalid_env:
                executor.run(
                    indicator.ProcessRequest(
                        runtime="codex",
                        argv=(sys.executable, "-c", "print('must-not-run')"),
                        env_overrides={"INVALID_VALUE": "nul\x00suffix"},
                        timeout=5.0,
                    )
                )
            self.assertEqual(invalid_env.exception.kind, "configuration")

            recovered_again = executor.run(
                indicator.ProcessRequest(
                    runtime="codex",
                    argv=(sys.executable, "-c", "print('recovered-again')"),
                    env_overrides={},
                    timeout=5.0,
                )
            )
            self.assertEqual(recovered_again.stdout, b"recovered-again\n")
        finally:
            executor.close()
            for process in spawned:
                _terminate_popen_group(process)

    def test_bdd_p10_cancel_and_close_do_not_wait_for_blocked_spawn(self) -> None:
        real_popen = subprocess.Popen
        for action in ("cancel", "close"):
            with self.subTest(action=action):
                executor = self.make_executor()
                spawn_entered = threading.Event()
                release_spawn = threading.Event()
                spawned: list[subprocess.Popen[bytes]] = []
                failures: list[BaseException] = []

                def gated_popen(
                    *args,
                    _spawn_entered=spawn_entered,
                    _release_spawn=release_spawn,
                    _spawned=spawned,
                    **kwargs,
                ):
                    _spawn_entered.set()
                    if not _release_spawn.wait(5):
                        raise AssertionError("spawn barrier was not released")
                    process = real_popen(*args, **kwargs)
                    _spawned.append(process)
                    return process

                def invoke(_executor=executor, _failures=failures) -> None:
                    try:
                        _executor.run(
                            indicator.ProcessRequest(
                                runtime="codex",
                                argv=(sys.executable, "-c", "print('late spawn')"),
                                env_overrides={},
                                timeout=5.0,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - retained for assertion
                        _failures.append(exc)

                worker = threading.Thread(target=invoke, daemon=True)
                try:
                    with patch.object(
                        indicator.subprocess, "Popen", side_effect=gated_popen
                    ):
                        worker.start()
                        self.assertTrue(spawn_entered.wait(3))
                        started = time.monotonic()
                        if action == "cancel":
                            executor.cancel("codex")
                        else:
                            executor.close()
                        self.assertLess(time.monotonic() - started, 0.25)
                        self.assertTrue(worker.is_alive())
                        release_spawn.set()
                        worker.join(timeout=5)

                    self.assertFalse(worker.is_alive())
                    self.assertEqual(len(failures), 1)
                    self.assertIsInstance(failures[0], indicator.ProcessFailure)
                    self.assertEqual(failures[0].kind, "cancelled")
                    for process in spawned:
                        process.wait(timeout=5)
                        _wait_for_process_exit(process.pid)
                finally:
                    release_spawn.set()
                    executor.close()
                    worker.join(timeout=5)
                    for process in spawned:
                        _terminate_popen_group(process)

    def test_bdd_p10_close_cancels_three_active_runtimes_without_serial_waits(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            child = tmp / "block.py"
            _write_executable(
                child,
                """#!/usr/bin/env python3
import os, sys, time
with open(sys.argv[1], 'w', encoding='utf-8') as stream:
    stream.write(str(os.getpid()))
    stream.flush()
    os.fsync(stream.fileno())
time.sleep(60)
""",
            )
            executor = self.make_executor()
            pid_files = {
                runtime: tmp / runtime for runtime in ("codex", "grok", "claude")
            }
            failures: list[BaseException] = []

            def invoke(runtime: str) -> None:
                try:
                    executor.run(
                        indicator.ProcessRequest(
                            runtime=runtime,
                            argv=(sys.executable, str(child), str(pid_files[runtime])),
                            env_overrides={},
                            timeout=30.0,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - retained for test threads
                    failures.append(exc)

            workers = [
                threading.Thread(target=invoke, args=(runtime,), daemon=True)
                for runtime in pid_files
            ]
            try:
                for worker in workers:
                    worker.start()
                for pid_file in pid_files.values():
                    _wait_for_file(pid_file)

                with executor._lock:
                    supervisor_pids = tuple(
                        handle.process.pid
                        for handle in executor._active.values()
                        if handle.process is not None
                    )
                self.assertEqual(len(supervisor_pids), 3)
                for supervisor_pid in supervisor_pids:
                    os.kill(supervisor_pid, signal.SIGSTOP)

                close_finished = threading.Event()

                def close_executor() -> None:
                    executor.close()
                    close_finished.set()

                closer = threading.Thread(target=close_executor, daemon=True)
                closer.start()
                self.assertTrue(
                    close_finished.wait(1.5),
                    "close serialized per-runtime termination deadlines",
                )
                self.assertTrue(all(worker.is_alive() for worker in workers))
                for worker in workers:
                    worker.join(timeout=7)
                closer.join(timeout=1)

                self.assertTrue(all(not worker.is_alive() for worker in workers))
                self.assertEqual(len(failures), 3)
                self.assertTrue(
                    all(
                        isinstance(failure, indicator.ProcessFailure)
                        and failure.kind == "cancelled"
                        for failure in failures
                    )
                )
                for pid_file in pid_files.values():
                    _wait_for_process_exit(int(pid_file.read_text(encoding="utf-8")))
                for supervisor_pid in supervisor_pids:
                    _wait_for_process_exit(supervisor_pid)
            finally:
                executor.close()
                for pid_file in pid_files.values():
                    _terminate_owned_groups_from_pid_file(pid_file, str(child))

    def test_bdd_p10_hard_deadline_kills_stopped_supervisor_and_descendants(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            ready = tmp / "provider-pid"
            provider = tmp / "stopped-supervisor-provider.py"
            _write_executable(
                provider,
                """#!/usr/bin/env python3
import os, subprocess, sys, threading, time
spawned = []
def spawn_detached_from_worker():
    spawned.append(subprocess.Popen([
        sys.executable, '-c', 'import time; time.sleep(60)', __file__,
    ], start_new_session=True))
worker = threading.Thread(target=spawn_detached_from_worker)
worker.start()
worker.join()
with open(sys.argv[1], 'w', encoding='utf-8') as stream:
    stream.write(f'{os.getpid()} {spawned[0].pid}')
    stream.flush()
    os.fsync(stream.fileno())
time.sleep(60)
""",
            )
            executor = self.make_executor()
            failures: list[BaseException] = []

            def invoke() -> None:
                try:
                    executor.run(
                        indicator.ProcessRequest(
                            runtime="codex",
                            argv=(sys.executable, str(provider), str(ready)),
                            env_overrides={},
                            timeout=0.2,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - retained for test thread
                    failures.append(exc)

            worker = threading.Thread(target=invoke, daemon=True)
            worker.start()
            try:
                _wait_for_file(ready)
                with executor._lock:
                    handle = executor._active["codex"]
                    self.assertIsNotNone(handle.process)
                    supervisor_pid = handle.process.pid
                os.kill(supervisor_pid, signal.SIGSTOP)

                worker.join(timeout=6)

                self.assertFalse(worker.is_alive())
                self.assertEqual(len(failures), 1)
                self.assertIsInstance(failures[0], indicator.ProcessFailure)
                self.assertEqual(failures[0].kind, "timeout")
                _wait_for_process_exit(supervisor_pid)
                for raw_pid in ready.read_text(encoding="utf-8").split():
                    _wait_for_process_exit(int(raw_pid))
            finally:
                executor.close()
                _terminate_owned_groups_from_pid_file(ready, str(provider))

    def test_bdd_p10_replacement_waits_for_cancelled_runtime_to_terminate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            old_ready = tmp / "old-ready"
            replacement_ran = tmp / "replacement-ran"
            blocker = tmp / "blocker.py"
            replacement = tmp / "replacement.py"
            _write_executable(
                blocker,
                """#!/usr/bin/env python3
import os, sys, time
with open(sys.argv[1], 'w', encoding='utf-8') as stream:
    stream.write(str(os.getpid()))
    stream.flush()
    os.fsync(stream.fileno())
time.sleep(60)
""",
            )
            _write_executable(
                replacement,
                """#!/usr/bin/env python3
from pathlib import Path
import sys
Path(sys.argv[1]).write_text('ran', encoding='utf-8')
print('replacement')
""",
            )
            executor = self.make_executor()
            old_failures: list[BaseException] = []
            replacement_results: list[object] = []
            replacement_failures: list[BaseException] = []

            def run_old() -> None:
                try:
                    executor.run(
                        indicator.ProcessRequest(
                            runtime="codex",
                            argv=(sys.executable, str(blocker), str(old_ready)),
                            env_overrides={},
                            timeout=30.0,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - retained for test thread
                    old_failures.append(exc)

            old_worker = threading.Thread(target=run_old, daemon=True)
            old_worker.start()
            try:
                _wait_for_file(old_ready)
                with executor._lock:
                    old_handle = executor._active["codex"]
                    self.assertIsNotNone(old_handle.process)
                    old_supervisor_pid = old_handle.process.pid
                os.kill(old_supervisor_pid, signal.SIGSTOP)
                executor.cancel("codex")

                replacement_handle = executor.reserve("codex")
                self.assertFalse(replacement_handle.ready.is_set())

                def run_replacement() -> None:
                    try:
                        result = executor.run(
                            indicator.ProcessRequest(
                                runtime="codex",
                                argv=(
                                    sys.executable,
                                    str(replacement),
                                    str(replacement_ran),
                                ),
                                env_overrides={},
                                timeout=5.0,
                            ),
                            replacement_handle,
                        )
                    except Exception as exc:  # noqa: BLE001 - retained for assertion
                        replacement_failures.append(exc)
                    else:
                        replacement_results.append(result)

                replacement_worker = threading.Thread(
                    target=run_replacement, daemon=True
                )
                replacement_worker.start()
                self.assertFalse(replacement_ran.exists())

                old_worker.join(timeout=7)
                replacement_worker.join(timeout=7)

                self.assertFalse(old_worker.is_alive())
                self.assertFalse(replacement_worker.is_alive())
                self.assertEqual(len(old_failures), 1)
                self.assertIsInstance(old_failures[0], indicator.ProcessFailure)
                self.assertEqual(old_failures[0].kind, "cancelled")
                self.assertEqual(replacement_failures, [])
                self.assertEqual(len(replacement_results), 1)
                self.assertEqual(replacement_results[0].stdout, b"replacement\n")
                self.assertEqual(replacement_ran.read_text(encoding="utf-8"), "ran")
                _wait_for_process_exit(old_supervisor_pid)
                _wait_for_process_exit(int(old_ready.read_text(encoding="utf-8")))
            finally:
                executor.close()
                _terminate_owned_groups_from_pid_file(old_ready, str(blocker))

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "PR_SET_PDEATHSIG is Linux-specific"
    )
    def test_bdd_l04_parent_sigkill_supervisor_reaps_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            tree_pids = tmp / "tree-pids"
            supervisor_pid = tmp / "supervisor-pid"
            tree = tmp / "tree.py"
            parent = tmp / "parent.py"
            _write_executable(
                tree,
                """#!/usr/bin/env python3
import os, subprocess, sys, time
grandchild = subprocess.Popen([
    sys.executable, '-c', 'import time; time.sleep(60)', __file__,
])
with open(sys.argv[1], 'w', encoding='utf-8') as stream:
    stream.write(f'{os.getpid()} {grandchild.pid}')
    stream.flush()
    os.fsync(stream.fileno())
time.sleep(60)
""",
            )
            _write_executable(
                parent,
                """#!/usr/bin/env python3
import os, subprocess, sys, time
proc = subprocess.Popen([
    sys.executable, sys.argv[1], '--child-supervisor',
    '--expected-parent-pid', str(os.getpid()), '--',
    sys.executable, sys.argv[2], sys.argv[3],
], start_new_session=True)
with open(sys.argv[4], 'w', encoding='utf-8') as stream:
    stream.write(str(proc.pid))
    stream.flush()
time.sleep(60)
""",
            )
            parent_proc = subprocess.Popen(
                [
                    sys.executable,
                    str(parent),
                    str(MODULE_PATH),
                    str(tree),
                    str(tree_pids),
                    str(supervisor_pid),
                ],
                start_new_session=True,
            )
            self.addCleanup(_terminate_popen_group, parent_proc)
            self.addCleanup(
                _terminate_owned_groups_from_pid_file,
                supervisor_pid,
                str(tree),
            )
            self.addCleanup(
                _terminate_owned_groups_from_pid_file,
                tree_pids,
                str(tree),
            )
            try:
                _wait_for_file(supervisor_pid)
                _wait_for_file(tree_pids)
                os.kill(int(supervisor_pid.read_text(encoding="utf-8")), signal.SIGSTOP)
                os.kill(parent_proc.pid, signal.SIGKILL)
                parent_proc.wait(timeout=5)
                _wait_for_process_exit(int(supervisor_pid.read_text(encoding="utf-8")))
                for raw_pid in tree_pids.read_text(encoding="utf-8").split():
                    _wait_for_process_exit(int(raw_pid))
            finally:
                _terminate_popen_group(parent_proc)
                _terminate_owned_groups_from_pid_file(supervisor_pid, str(tree))
                _terminate_owned_groups_from_pid_file(tree_pids, str(tree))

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "parent identity binding is Linux-specific"
    )
    def test_bdd_l04_reparented_supervisor_cannot_start_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            provider_started = tmp / "provider-started"
            wrapper_ready = tmp / "wrapper-ready"
            provider = tmp / "provider.py"
            wrapper = tmp / "wrapper.py"
            parent = tmp / "parent.py"
            _write_executable(
                provider,
                """#!/usr/bin/env python3
from pathlib import Path
import sys, time
Path(sys.argv[1]).write_text('started', encoding='utf-8')
time.sleep(60)
""",
            )
            _write_executable(
                wrapper,
                """#!/usr/bin/env python3
import os, signal, sys
from pathlib import Path
expected_parent = os.getppid()
Path(sys.argv[4]).write_text(
    f'{os.getpid()} {expected_parent}', encoding='utf-8'
)
os.kill(os.getpid(), signal.SIGSTOP)
os.execv(sys.executable, [
    sys.executable, sys.argv[1], '--child-supervisor',
    '--expected-parent-pid', str(expected_parent), '--',
    sys.executable, sys.argv[2], sys.argv[3],
])
""",
            )
            _write_executable(
                parent,
                """#!/usr/bin/env python3
import subprocess, sys, time
subprocess.Popen([
    sys.executable, sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5],
], start_new_session=True)
time.sleep(60)
""",
            )
            parent_proc = subprocess.Popen(
                [
                    sys.executable,
                    str(parent),
                    str(wrapper),
                    str(MODULE_PATH),
                    str(provider),
                    str(provider_started),
                    str(wrapper_ready),
                ],
                start_new_session=True,
            )
            wrapper_pid: int | None = None
            try:
                _wait_for_file(wrapper_ready)
                wrapper_pid, expected_parent = (
                    int(value)
                    for value in wrapper_ready.read_text(encoding="utf-8").split()
                )
                self.assertEqual(expected_parent, parent_proc.pid)
                os.kill(parent_proc.pid, signal.SIGKILL)
                parent_proc.wait(timeout=5)
                self.assertNotEqual(_parent_pid(wrapper_pid), expected_parent)

                os.kill(wrapper_pid, signal.SIGCONT)
                _wait_for_process_exit(wrapper_pid)

                self.assertFalse(provider_started.exists())
            finally:
                _terminate_popen_group(parent_proc)
                if wrapper_pid is not None:
                    try:
                        os.killpg(wrapper_pid, signal.SIGKILL)
                    except OSError:
                        pass


if __name__ == "__main__":
    unittest.main()
