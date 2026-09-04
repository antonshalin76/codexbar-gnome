from __future__ import annotations

import itertools
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter, deque
from pathlib import Path
from unittest.mock import patch

from tests.support import MODULE_PATH, load_indicator

indicator = load_indicator("codexbar_refresh_view_tests")
RUNTIMES = ("codex", "grok", "claude")


def _settings(**modes: str) -> dict[str, object]:
    runtimes: dict[str, dict[str, bool]] = {}
    for runtime in RUNTIMES:
        mode = modes.get(runtime, "off")
        runtimes[runtime] = {
            "poll": mode != "off",
            "autoRefresh": mode == "auto",
        }
    return {"runtimes": runtimes}


def _usage(percent: int, *, secondary: int | None = None):
    return indicator.Usage(
        primary=indicator.UsageWindow(percent=percent, window_minutes=300),
        secondary=(
            indicator.UsageWindow(percent=secondary, window_minutes=10080)
            if secondary is not None
            else None
        ),
    )


def _success(runtime: str, percent: int, *, secondary: int | None = None):
    return indicator.RuntimeResult(
        runtime=runtime,
        source="zai" if runtime == "claude" else runtime,
        usage=_usage(percent, secondary=secondary),
    )


def _failure(
    runtime: str,
    message: str = "provider unavailable",
    *,
    kind: str = "provider",
):
    return indicator.RuntimeFailure(runtime=runtime, kind=kind, message=message)


def _runtime(snapshot, runtime: str):
    return next(item for item in snapshot.runtimes if item.runtime == runtime)


class SnapshotSink:
    def __init__(self) -> None:
        self.snapshots: list[object] = []
        self._condition = threading.Condition()

    def __call__(self, snapshot) -> None:
        with self._condition:
            self.snapshots.append(snapshot)
            self._condition.notify_all()

    def wait_for(self, predicate, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for snapshot in reversed(self.snapshots):
                    try:
                        if predicate(snapshot):
                            return snapshot
                    except StopIteration:
                        continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("timed out waiting for coordinator snapshot")
                self._condition.wait(remaining)


class BlockingOutcome:
    def __init__(self, outcome, *, release_on_cancel: bool = True) -> None:
        self.outcome = outcome
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.release_on_cancel = release_on_cancel

    def run(self):
        self.entered.set()
        if not self.release.wait(3):
            raise AssertionError("test did not release blocked provider")
        try:
            return self.outcome
        finally:
            self.finished.set()

    def cancel(self) -> None:
        if self.release_on_cancel:
            self.release.set()


class ScriptedGateway:
    def __init__(self, **scripts) -> None:
        self.scripts = {runtime: deque(values) for runtime, values in scripts.items()}
        self.calls: list[str] = []
        self.fetch_attempts: list[str] = []
        self.cancelled: list[str] = []
        self.closed = False
        self.active = Counter()
        self.max_active = Counter()
        self._active_gates: dict[str, BlockingOutcome] = {}
        self._reservations: dict[str, dict[str, bool]] = {}
        self._condition = threading.Condition()

    def reserve(self, runtime: str) -> dict[str, bool]:
        with self._condition:
            reservation = {"cancelled": False}
            self._reservations[runtime] = reservation
            return reservation

    def fetch(self, runtime: str, reservation: dict[str, bool]):
        with self._condition:
            self.fetch_attempts.append(runtime)
            if reservation.get("cancelled"):
                if self._reservations.get(runtime) is reservation:
                    self._reservations.pop(runtime, None)
                return indicator.RuntimeFailure(
                    runtime, "cancelled", "provider command was cancelled"
                )
            self.calls.append(runtime)
            self.active[runtime] += 1
            self.max_active[runtime] = max(
                self.max_active[runtime], self.active[runtime]
            )
            outcome = self.scripts[runtime].popleft()
            if isinstance(outcome, BlockingOutcome):
                self._active_gates[runtime] = outcome
            self._condition.notify_all()
        try:
            return outcome.run() if isinstance(outcome, BlockingOutcome) else outcome
        finally:
            with self._condition:
                self.active[runtime] -= 1
                self._active_gates.pop(runtime, None)
                if self._reservations.get(runtime) is reservation:
                    self._reservations.pop(runtime, None)
                self._condition.notify_all()

    def cancel(self, runtime: str) -> None:
        with self._condition:
            self.cancelled.append(runtime)
            reservation = self._reservations.pop(runtime, None)
            if reservation is not None:
                reservation["cancelled"] = True
            gate = self._active_gates.get(runtime)
        if gate is not None:
            gate.cancel()

    def release(self, reservation: object) -> None:
        with self._condition:
            for runtime, current in tuple(self._reservations.items()):
                if current is reservation:
                    current["cancelled"] = True
                    self._reservations.pop(runtime, None)
                    break

    def close(self) -> None:
        self.closed = True
        with self._condition:
            gates = tuple(self._active_gates.values())
            for reservation in self._reservations.values():
                reservation["cancelled"] = True
        for gate in gates:
            gate.cancel()

    def wait_for_calls(self, count: int, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.calls) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"expected {count} calls, observed {self.calls!r}"
                    )
                self._condition.wait(remaining)


class ManualSubmitter:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, object]] = []

    def __call__(self, runtime: str, job) -> None:
        self.jobs.append((runtime, job))

    def run(self, runtime: str | None = None) -> None:
        if runtime is None:
            index = 0
        else:
            index = next(
                index
                for index, (queued_runtime, _) in enumerate(self.jobs)
                if queued_runtime == runtime
            )
        _, job = self.jobs.pop(index)
        job()


class ManualCallbackQueue:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def __call__(self, callback) -> None:
        self.callbacks.append(callback)

    def run_all(self) -> None:
        while self.callbacks:
            callback = self.callbacks.pop(0)
            callback()


class AcceptingSettingsStore:
    def __init__(self) -> None:
        self.saved: list[dict[str, object]] = []

    def save(self, settings: dict[str, object]) -> None:
        self.saved.append(json.loads(json.dumps(settings)))


class RefreshCoordinatorRedTests(unittest.TestCase):
    def test_bdd_s05b_fail_closed_settings_surface_without_provider_call(self) -> None:
        gateway = ScriptedGateway()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(),
            global_error="Settings error: malformed configuration",
        )
        try:
            self.assertEqual(
                coordinator.snapshot(),
                indicator.ViewSnapshot(
                    runtimes=(),
                    global_error="Settings error: malformed configuration",
                ),
            )
            coordinator.request_manual()
            coordinator.request_auto()
            self.assertEqual(gateway.calls, [])
        finally:
            coordinator.close()

    def test_bdd_s05b_failed_load_cannot_be_overwritten_by_a_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            config = Path(raw_tmp) / "config.json"
            original = b'{"runtimes":'
            config.write_bytes(original)
            store = indicator.SettingsStore(config)
            loaded = store.load()
            gateway = ScriptedGateway()
            coordinator = indicator.RefreshCoordinator(
                gateway,
                loaded.settings,
                settings_store=store,
                global_error=(
                    f"Settings error: {loaded.failure.message}"
                    if loaded.failure
                    else None
                ),
            )
            candidate = _settings(codex="manual")
            try:
                self.assertFalse(coordinator.update_settings(candidate))
                self.assertEqual(config.read_bytes(), original)
                self.assertEqual(coordinator.snapshot().runtimes, ())
                self.assertLessEqual(len(coordinator.snapshot().global_error), 512)

                externally_fixed = _settings(grok="auto")
                config.write_text(json.dumps(externally_fixed), encoding="utf-8")
                self.assertFalse(coordinator.update_settings(candidate))
                self.assertEqual(
                    json.loads(config.read_text(encoding="utf-8")), externally_fixed
                )
                reloaded = indicator.SettingsStore(config).load()
                self.assertIsNone(reloaded.failure)
                self.assertEqual(reloaded.settings, externally_fixed)
            finally:
                coordinator.close()

    def test_bdd_s03_toggle_command_rejects_non_boolean_without_persisting(
        self,
    ) -> None:
        gateway = ScriptedGateway()
        store = AcceptingSettingsStore()
        coordinator = indicator.RefreshCoordinator(
            gateway, _settings(), settings_store=store
        )
        try:
            for invalid in (None, 0, 1, "false", [], {}):
                with self.subTest(value=invalid):
                    self.assertIsNone(
                        coordinator.update_setting("codex", "poll", invalid)
                    )
            self.assertEqual(store.saved, [])
            self.assertEqual(coordinator.snapshot().runtimes, ())
            self.assertEqual(gateway.calls, [])
            self.assertIn("must be a boolean", coordinator.snapshot().global_error)
        finally:
            coordinator.close()

    def test_bdd_s08_close_does_not_wait_for_blocked_durable_save(self) -> None:
        save_entered = threading.Event()
        release_save = threading.Event()

        class BlockingStore:
            def save(self, _settings: object) -> None:
                save_entered.set()
                if not release_save.wait(5):
                    raise AssertionError("settings save barrier was not released")

        gateway = ScriptedGateway()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(),
            settings_store=BlockingStore(),
        )
        results: list[bool] = []
        failures: list[BaseException] = []

        def update() -> None:
            try:
                results.append(coordinator.update_settings(_settings(codex="manual")))
            except Exception as exc:  # noqa: BLE001 - retained for assertion
                failures.append(exc)

        worker = threading.Thread(target=update, daemon=True)
        try:
            worker.start()
            self.assertTrue(save_entered.wait(3))
            started = time.monotonic()
            coordinator.close()
            self.assertLess(time.monotonic() - started, 0.25)
            self.assertTrue(gateway.closed)

            release_save.set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(results, [False])
        finally:
            release_save.set()
            coordinator.close()
            worker.join(timeout=3)

    def test_bdd_s08_committed_toggle_survives_cancel_cleanup_failure(self) -> None:
        class RaisingCancelGateway(ScriptedGateway):
            def cancel(self, runtime: str) -> None:
                super().cancel(runtime)
                raise PermissionError("injected cancellation failure")

        gateway = RaisingCancelGateway(codex=[_success("codex", 19)])
        store = AcceptingSettingsStore()
        submitter = ManualSubmitter()
        holder: dict[str, object] = {}
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            settings_store=store,
            on_snapshot=lambda snapshot: holder["view"].render(snapshot),
            worker_submit=submitter,
        )
        view = indicator.IndicatorView(
            FakePanelIndicator(),
            settings=_settings(codex="manual"),
            on_setting_toggle=lambda runtime, key, enabled, complete: complete(
                coordinator.update_setting(runtime, key, enabled)
            ),
        )
        holder["view"] = view
        try:
            coordinator.request_manual()
            poll_codex = next(
                item
                for item in view.menu.get_children()
                if isinstance(item, indicator.Gtk.CheckMenuItem)
                and item.get_label() == "Poll Codex"
            )

            poll_codex.set_active(False)

            self.assertFalse(poll_codex.get_active())
            self.assertEqual(store.saved, [_settings()])
            snapshot = coordinator.snapshot()
            self.assertEqual(snapshot.runtimes, ())
            self.assertEqual(
                snapshot.global_error,
                "Cleanup error: provider cancellation failed: PermissionError",
            )
            self.assertIn("CodexBar: ERROR:", "\n".join(_visible_menu_labels(view)))
            self.assertEqual(gateway.calls, [])
        finally:
            view.close()
            coordinator.close()

    def test_bdd_r11_close_during_committed_cancel_suppresses_late_snapshot(
        self,
    ) -> None:
        active = BlockingOutcome(_success("codex", 20))
        cancel_entered = threading.Event()
        cancel_release = threading.Event()

        class BlockingCancelGateway(ScriptedGateway):
            def cancel(self, runtime: str) -> None:
                super().cancel(runtime)
                cancel_entered.set()
                if not cancel_release.wait(3):
                    raise AssertionError("cancel barrier was not released")

        gateway = BlockingCancelGateway(codex=[active])
        sink = SnapshotSink()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            settings_store=AcceptingSettingsStore(),
            on_snapshot=sink,
        )
        results: list[bool] = []
        worker = threading.Thread(
            target=lambda: results.append(coordinator.update_settings(_settings())),
            daemon=True,
        )
        try:
            coordinator.request_manual()
            self.assertTrue(active.entered.wait(3))
            worker.start()
            self.assertTrue(cancel_entered.wait(3))
            published_before_close = tuple(sink.snapshots)
            coordinator.close()
            cancel_release.set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results, [True])
            self.assertEqual(tuple(sink.snapshots), published_before_close)
        finally:
            cancel_release.set()
            active.release.set()
            coordinator.close()
            worker.join(timeout=3)

    def test_bdd_r11_close_between_request_decision_and_dispatch_is_final(
        self,
    ) -> None:
        gateway = ScriptedGateway(codex=[_success("codex", 21)])
        submitter = ManualSubmitter()
        sink = SnapshotSink()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            worker_submit=submitter,
            on_snapshot=sink,
        )
        request_thread = threading.Thread(
            target=lambda: coordinator.request_manual(), daemon=True
        )
        with coordinator._dispatch_lock:
            request_thread.start()
            deadline = time.monotonic() + 3
            while not _runtime(coordinator.snapshot(), "codex").refreshing:
                if time.monotonic() >= deadline:
                    self.fail("request did not reach its dispatch boundary")
                threading.Event().wait(0.01)
            coordinator.close()
        request_thread.join(timeout=3)
        self.assertFalse(request_thread.is_alive())
        self.assertEqual(submitter.jobs, [])
        self.assertEqual(sink.snapshots, [])
        self.assertTrue(gateway.closed)

    def test_bdd_r01_complete_runtime_state_transitions(self) -> None:
        first = BlockingOutcome(_success("codex", 10))
        fail = BlockingOutcome(_failure("codex", "offline"))
        recover = BlockingOutcome(_success("codex", 22))
        gateway = ScriptedGateway(codex=[first, fail, recover])
        sink = SnapshotSink()
        coordinator = indicator.RefreshCoordinator(
            gateway, _settings(codex="manual"), on_snapshot=sink
        )
        try:
            initial = coordinator.snapshot()
            self.assertEqual(
                (
                    _runtime(initial, "codex").state,
                    _runtime(initial, "codex").usage,
                    _runtime(initial, "codex").error,
                    _runtime(initial, "codex").refreshing,
                ),
                ("pending", None, None, False),
            )

            coordinator.request(("codex",))
            self.assertTrue(first.entered.wait(3))
            pending = sink.wait_for(lambda value: _runtime(value, "codex").refreshing)
            self.assertEqual(_runtime(pending, "codex").state, "pending")
            first.release.set()
            good = sink.wait_for(
                lambda value: (
                    _runtime(value, "codex").state == "good"
                    and not _runtime(value, "codex").refreshing
                )
            )
            self.assertEqual(_runtime(good, "codex").usage, _usage(10))

            coordinator.request(("codex",))
            self.assertTrue(fail.entered.wait(3))
            refreshing_good = sink.wait_for(
                lambda value: (
                    _runtime(value, "codex").state == "good"
                    and _runtime(value, "codex").refreshing
                )
            )
            self.assertEqual(_runtime(refreshing_good, "codex").usage, _usage(10))
            fail.release.set()
            stale = sink.wait_for(
                lambda value: (
                    _runtime(value, "codex").state == "stale"
                    and not _runtime(value, "codex").refreshing
                )
            )
            self.assertEqual(_runtime(stale, "codex").usage, _usage(10))
            self.assertEqual(_runtime(stale, "codex").error, "offline")

            coordinator.request(("codex",))
            self.assertTrue(recover.entered.wait(3))
            refreshing_stale = sink.wait_for(
                lambda value: (
                    _runtime(value, "codex").state == "stale"
                    and _runtime(value, "codex").refreshing
                )
            )
            self.assertEqual(_runtime(refreshing_stale, "codex").error, "offline")
            recover.release.set()
            recovered = sink.wait_for(
                lambda value: (
                    _runtime(value, "codex").state == "good"
                    and _runtime(value, "codex").usage == _usage(22)
                )
            )
            self.assertIsNone(_runtime(recovered, "codex").error)
        finally:
            coordinator.close()

        failure_gateway = ScriptedGateway(
            codex=[_failure("codex", "bad config"), _success("codex", 7)]
        )
        failure_sink = SnapshotSink()
        coordinator = indicator.RefreshCoordinator(
            failure_gateway, _settings(codex="manual"), on_snapshot=failure_sink
        )
        try:
            coordinator.request(("codex",))
            failed = failure_sink.wait_for(
                lambda value: (
                    _runtime(value, "codex").state == "error"
                    and not _runtime(value, "codex").refreshing
                )
            )
            self.assertIsNone(_runtime(failed, "codex").usage)
            self.assertEqual(_runtime(failed, "codex").error, "bad config")
            coordinator.request(("codex",))
            refreshing_error = failure_sink.wait_for(
                lambda value: (
                    _runtime(value, "codex").state == "error"
                    and _runtime(value, "codex").refreshing
                )
            )
            self.assertEqual(_runtime(refreshing_error, "codex").error, "bad config")
            recovered = failure_sink.wait_for(
                lambda value: (
                    _runtime(value, "codex").state == "good"
                    and not _runtime(value, "codex").refreshing
                )
            )
            self.assertEqual(
                (
                    _runtime(recovered, "codex").state,
                    _runtime(recovered, "codex").usage,
                    _runtime(recovered, "codex").error,
                    _runtime(recovered, "codex").refreshing,
                ),
                ("good", _usage(7), None, False),
            )
        finally:
            coordinator.close()

    def test_bdd_r02_parallel_lanes_publish_fast_result_first(self) -> None:
        blocked = BlockingOutcome(_success("codex", 10))
        gateway = ScriptedGateway(
            codex=[blocked],
            grok=[_success("grok", 20)],
        )
        sink = SnapshotSink()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual", grok="manual"),
            on_snapshot=sink,
        )
        try:
            coordinator.request(("codex", "grok"))
            self.assertTrue(blocked.entered.wait(3))
            merged = sink.wait_for(
                lambda value: _runtime(value, "grok").state == "good"
            )
            self.assertEqual(_runtime(merged, "codex").state, "pending")
            self.assertTrue(_runtime(merged, "codex").refreshing)
        finally:
            blocked.release.set()
            coordinator.close()

    def test_bdd_r03_completion_merges_only_one_runtime(self) -> None:
        submitter = ManualSubmitter()
        gateway = ScriptedGateway(
            codex=[_success("codex", 10), _success("codex", 11)],
            grok=[_success("grok", 20)],
        )
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual", grok="manual"),
            worker_submit=submitter,
        )
        try:
            coordinator.request(("codex", "grok"))
            submitter.run()
            submitter.run()
            before = coordinator.snapshot()
            grok_before = _runtime(before, "grok")
            coordinator.request(("codex",))
            submitter.run()
            after = coordinator.snapshot()
            self.assertEqual(_runtime(after, "grok"), grok_before)
            self.assertEqual(_runtime(after, "codex").usage, _usage(11))
        finally:
            coordinator.close()

    def test_bdd_r03_delayed_publication_cannot_resurrect_stale_snapshot(
        self,
    ) -> None:
        submitter = ManualSubmitter()
        sink = SnapshotSink()
        coordinator = indicator.RefreshCoordinator(
            ScriptedGateway(grok=[_success("grok", 33)]),
            _settings(grok="manual"),
            settings_store=AcceptingSettingsStore(),
            worker_submit=submitter,
            on_snapshot=sink,
        )
        coordinator.request(("grok",))
        with coordinator._lock:
            lane = coordinator._lanes["grok"]
            generation = lane.generation
            token = lane.active_token
        self.assertIsNotNone(token)
        results: list[bool] = []
        update_thread = threading.Thread(
            target=lambda: results.append(
                coordinator.update_settings(_settings(grok="manual", claude="manual"))
            ),
            daemon=True,
        )
        try:
            with coordinator._dispatch_lock:
                update_thread.start()
                deadline = time.monotonic() + 3
                while not any(
                    item.runtime == "claude" for item in coordinator.snapshot().runtimes
                ):
                    if time.monotonic() >= deadline:
                        self.fail("settings update did not reach publication boundary")
                    threading.Event().wait(0.01)
                coordinator._complete("grok", generation, token, _success("grok", 33))
            update_thread.join(timeout=3)
            self.assertFalse(update_thread.is_alive())
            self.assertEqual(results, [True])
            current = coordinator.snapshot()
            self.assertEqual(sink.snapshots[-1], current)
            self.assertEqual(_runtime(current, "grok").state, "good")
            self.assertEqual(_runtime(current, "claude").state, "pending")
        finally:
            coordinator.close()
            update_thread.join(timeout=3)

    def test_bdd_r04_manual_only_data_survives_auto_refresh(self) -> None:
        submitter = ManualSubmitter()
        gateway = ScriptedGateway(
            codex=[_success("codex", 10), _success("codex", 11)],
            claude=[_success("claude", 80, secondary=90)],
        )
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="auto", claude="manual"),
            worker_submit=submitter,
        )
        try:
            coordinator.request_manual()
            while submitter.jobs:
                submitter.run()
            claude_before = _runtime(coordinator.snapshot(), "claude")
            coordinator.request_auto()
            while submitter.jobs:
                submitter.run()
            after = coordinator.snapshot()
            self.assertEqual(_runtime(after, "claude"), claude_before)
            self.assertEqual(_runtime(after, "codex").usage, _usage(11))
        finally:
            coordinator.close()

    def test_bdd_r05_empty_auto_set_is_an_exact_no_op(self) -> None:
        submitter = ManualSubmitter()
        gateway = ScriptedGateway(codex=[_success("codex", 10)])
        sink = SnapshotSink()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            worker_submit=submitter,
            on_snapshot=sink,
        )
        try:
            coordinator.request_manual()
            submitter.run()
            before = coordinator.snapshot()
            published = tuple(sink.snapshots)
            calls = tuple(gateway.calls)
            coordinator.request_auto()
            self.assertEqual(coordinator.snapshot(), before)
            self.assertEqual(tuple(sink.snapshots), published)
            self.assertEqual(tuple(gateway.calls), calls)
            self.assertEqual(submitter.jobs, [])
        finally:
            coordinator.close()

    def test_bdd_r06_single_flight_coalesces_to_one_rerun(self) -> None:
        first = BlockingOutcome(_success("codex", 10))
        gateway = ScriptedGateway(codex=[first, _success("codex", 20)])
        sink = SnapshotSink()
        store = AcceptingSettingsStore()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="auto"),
            settings_store=store,
            on_snapshot=sink,
        )
        try:
            coordinator.request(("codex",))
            self.assertTrue(first.entered.wait(3))
            before_coalescing = coordinator.snapshot()
            publications_before = tuple(sink.snapshots)
            coordinator.request(("codex",))
            coordinator.request(("codex",))
            self.assertEqual(coordinator.snapshot(), before_coalescing)
            self.assertEqual(tuple(sink.snapshots), publications_before)

            latest = _settings(codex="manual")
            self.assertTrue(coordinator.update_settings(latest))
            self.assertEqual(store.saved, [latest])
            coordinator.request(("codex",))
            first.release.set()
            gateway.wait_for_calls(2)
            final = sink.wait_for(
                lambda value: (
                    _runtime(value, "codex").usage == _usage(20)
                    and not _runtime(value, "codex").refreshing
                )
            )
            self.assertEqual(gateway.calls, ["codex", "codex"])
            self.assertEqual(gateway.max_active["codex"], 1)
            self.assertEqual(_runtime(final, "codex").state, "good")
            published_after_completion = tuple(sink.snapshots)
            coordinator.request_auto()
            self.assertEqual(gateway.calls, ["codex", "codex"])
            self.assertEqual(tuple(sink.snapshots), published_after_completion)
        finally:
            coordinator.close()

        auto_first = BlockingOutcome(_success("codex", 30))
        gateway = ScriptedGateway(codex=[auto_first, _success("codex", 40)])
        sink = SnapshotSink()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="auto"),
            settings_store=AcceptingSettingsStore(),
            on_snapshot=sink,
        )
        try:
            coordinator.request_auto()
            self.assertTrue(auto_first.entered.wait(3))
            coordinator.request_auto()
            self.assertTrue(coordinator.update_settings(_settings(codex="manual")))
            auto_first.release.set()
            sink.wait_for(
                lambda value: (
                    not _runtime(value, "codex").refreshing
                    and _runtime(value, "codex").usage == _usage(30)
                )
            )
            self.assertEqual(gateway.calls, ["codex"])

            coordinator.request_manual()
            sink.wait_for(
                lambda value: (
                    not _runtime(value, "codex").refreshing
                    and _runtime(value, "codex").usage == _usage(40)
                )
            )
            self.assertEqual(gateway.calls, ["codex", "codex"])
        finally:
            coordinator.close()

    def test_bdd_r07_disable_cancels_and_invalidates_inflight_result(self) -> None:
        old = BlockingOutcome(_success("codex", 99))
        gateway = ScriptedGateway(codex=[old])
        sink = SnapshotSink()
        store = AcceptingSettingsStore()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            settings_store=store,
            on_snapshot=sink,
        )
        try:
            coordinator.request(("codex",))
            self.assertTrue(old.entered.wait(3))
            self.assertTrue(coordinator.update_settings(_settings()))
            self.assertEqual(store.saved, [_settings()])
            self.assertEqual(gateway.cancelled, ["codex"])
            self.assertEqual(coordinator.snapshot().runtimes, ())
            self.assertTrue(old.finished.wait(3))
            self.assertEqual(coordinator.snapshot().runtimes, ())
            self.assertFalse(
                any(
                    item.runtime == "codex" and item.state == "good"
                    for snapshot in sink.snapshots
                    for item in snapshot.runtimes
                )
            )
        finally:
            coordinator.close()

        dispatch_entered = threading.Event()
        dispatch_release = threading.Event()
        submitter = ManualSubmitter()
        gateway = ScriptedGateway(codex=[_success("codex", 80)])

        class GatedDispatchCoordinator(indicator.RefreshCoordinator):
            def _submit_job(self, work: object) -> None:
                dispatch_entered.set()
                if not dispatch_release.wait(3):
                    raise AssertionError("dispatch admission barrier timed out")
                super()._submit_job(work)

        coordinator = GatedDispatchCoordinator(
            gateway,
            _settings(codex="manual"),
            settings_store=AcceptingSettingsStore(),
            worker_submit=submitter,
        )
        requester = threading.Thread(
            target=lambda: coordinator.request(("codex",)), daemon=True
        )
        try:
            requester.start()
            self.assertTrue(dispatch_entered.wait(3))
            self.assertTrue(coordinator.update_settings(_settings()))
            dispatch_release.set()
            requester.join(timeout=3)
            self.assertFalse(requester.is_alive())
            self.assertEqual(submitter.jobs, [])
            self.assertEqual(gateway.fetch_attempts, [])
            self.assertEqual(coordinator.snapshot().runtimes, ())
        finally:
            dispatch_release.set()
            requester.join(timeout=3)
            coordinator.close()

        submitter = ManualSubmitter()
        gateway = ScriptedGateway(codex=[_success("codex", 81)])
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            settings_store=AcceptingSettingsStore(),
            worker_submit=submitter,
        )
        try:
            coordinator.request_manual()
            self.assertEqual(len(submitter.jobs), 1)
            self.assertTrue(coordinator.update_settings(_settings()))
            submitter.run("codex")
            self.assertEqual(gateway.fetch_attempts, [])
            self.assertEqual(coordinator.snapshot().runtimes, ())
        finally:
            coordinator.close()

    def test_bdd_r07_disable_reaps_real_provider_child_and_grandchild(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            pid_file = root / "provider-pids"
            provider = root / "blocking-provider.py"
            provider.write_text(
                """#!/usr/bin/env python3
import os, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
with open(sys.argv[1], 'w', encoding='utf-8') as stream:
    stream.write(f'{os.getpid()} {grandchild.pid}')
    stream.flush()
    os.fsync(stream.fileno())
time.sleep(60)
""",
                encoding="utf-8",
            )
            provider.chmod(0o755)
            executor = indicator.ProcessExecutor(supervisor_path=MODULE_PATH)

            class RealCancellationGateway:
                def reserve(self, runtime: str) -> object:
                    return executor.reserve(runtime)

                def fetch(self, runtime: str, reservation: object):
                    try:
                        executor.run(
                            indicator.ProcessRequest(
                                runtime=runtime,
                                argv=(sys.executable, str(provider), str(pid_file)),
                                env_overrides={},
                                timeout=30,
                            ),
                            reservation,
                        )
                    except indicator.ProcessFailure as exc:
                        return indicator.RuntimeFailure(
                            runtime, exc.kind, exc.sanitized_message
                        )
                    raise AssertionError("blocking provider unexpectedly succeeded")

                def cancel(self, runtime: str) -> None:
                    executor.cancel(runtime)

                def close(self) -> None:
                    executor.close()

            coordinator = indicator.RefreshCoordinator(
                RealCancellationGateway(),
                _settings(codex="manual"),
                settings_store=AcceptingSettingsStore(),
            )
            try:
                coordinator.request_manual()
                deadline = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(pid_file.is_file())
                with executor._lock:
                    handle = executor._active["codex"]
                    self.assertIsNotNone(handle.process)
                    supervisor_pid = handle.process.pid
                os.kill(supervisor_pid, signal.SIGSTOP)

                started = time.monotonic()
                self.assertTrue(coordinator.update_settings(_settings()))
                self.assertLess(
                    time.monotonic() - started,
                    0.25,
                    "runtime disable blocked the UI-facing settings path",
                )

                for raw_pid in pid_file.read_text(encoding="utf-8").split():
                    _assert_pid_absent(int(raw_pid))
                _assert_pid_absent(supervisor_pid)
            finally:
                coordinator.close()

    def test_bdd_r07_disable_while_waiting_for_capability_never_starts_quota(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            ledger = root / "ledger.jsonl"
            ready = root / "capability-ready"
            release = root / "capability-release"
            binary = root / "codexbar"
            binary.write_text(
                """#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
with open(os.environ['RACE_LEDGER'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(args) + '\\n')
if args == ['usage', '--help']:
    pathlib.Path(os.environ['RACE_READY']).touch()
    deadline = time.monotonic() + 10
    while not pathlib.Path(os.environ['RACE_RELEASE']).exists():
        if time.monotonic() >= deadline:
            raise SystemExit(70)
        time.sleep(0.01)
    print('--provider codex|grok|claude|zai --json-only --no-color')
    raise SystemExit(0)
provider = args[args.index('--provider') + 1]
print(json.dumps([{
    'provider': provider,
    'usage': {'primary': {'usedPercent': 7}},
}]))
""",
                encoding="utf-8",
            )
            binary.chmod(0o755)
            executor = indicator.ProcessExecutor(
                executable=binary, supervisor_path=MODULE_PATH
            )
            provider_gateway = indicator.ProviderGateway(
                executor, root / "missing-claude-settings.json"
            )
            grok_fetch_entered = threading.Event()
            grok_fetch_finished = threading.Event()

            class ObservedGateway:
                def reserve(self, runtime: str):
                    return provider_gateway.reserve(runtime)

                def fetch(self, runtime: str, reservation: object):
                    if runtime == "grok":
                        grok_fetch_entered.set()
                    try:
                        return provider_gateway.fetch(runtime, reservation)
                    finally:
                        if runtime == "grok":
                            grok_fetch_finished.set()

                def cancel(self, runtime: str) -> None:
                    provider_gateway.cancel(runtime)

                def close(self) -> None:
                    provider_gateway.close()

            sink = SnapshotSink()
            coordinator = indicator.RefreshCoordinator(
                ObservedGateway(),
                _settings(codex="manual", grok="manual"),
                settings_store=AcceptingSettingsStore(),
                on_snapshot=sink,
            )
            environment = {
                "RACE_LEDGER": str(ledger),
                "RACE_READY": str(ready),
                "RACE_RELEASE": str(release),
            }
            try:
                with patch.dict(os.environ, environment, clear=False):
                    coordinator.request(("codex", "grok"))
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    self.assertTrue(grok_fetch_entered.wait(3))
                    self.assertTrue(
                        coordinator.update_settings(_settings(codex="manual"))
                    )
                    release.touch()
                    sink.wait_for(
                        lambda snapshot: (
                            len(snapshot.runtimes) == 1
                            and _runtime(snapshot, "codex").state == "good"
                            and not _runtime(snapshot, "codex").refreshing
                        ),
                        timeout=5,
                    )
                    self.assertTrue(grok_fetch_finished.wait(5))

                calls = [
                    json.loads(line)
                    for line in ledger.read_text(encoding="utf-8").splitlines()
                ]
                self.assertIn(["usage", "--help"], calls)
                self.assertIn(
                    [
                        "usage",
                        "--provider",
                        "codex",
                        "--json-only",
                        "--no-color",
                    ],
                    calls,
                )
                self.assertNotIn(
                    [
                        "usage",
                        "--provider",
                        "grok",
                        "--json-only",
                        "--no-color",
                    ],
                    calls,
                )
            finally:
                release.touch()
                coordinator.close()

    def test_bdd_r07_disabling_capability_owner_reaps_probe_without_quota(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            ledger = root / "ledger.jsonl"
            ready = root / "capability-ready"
            binary = root / "codexbar"
            binary.write_text(
                """#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
with open(os.environ['OWNER_LEDGER'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(args) + '\\n')
if args == ['usage', '--help']:
    pathlib.Path(os.environ['OWNER_READY']).write_text(str(os.getpid()), encoding='utf-8')
    time.sleep(60)
    raise SystemExit(70)
raise AssertionError('quota must not start after owner disable')
""",
                encoding="utf-8",
            )
            binary.chmod(0o755)
            executor = indicator.ProcessExecutor(
                executable=binary, supervisor_path=MODULE_PATH
            )
            provider_gateway = indicator.ProviderGateway(
                executor, root / "missing-claude-settings.json"
            )
            fetch_finished = threading.Event()

            class ObservedGateway:
                def reserve(self, runtime: str):
                    return provider_gateway.reserve(runtime)

                def fetch(self, runtime: str, reservation: object):
                    try:
                        return provider_gateway.fetch(runtime, reservation)
                    finally:
                        fetch_finished.set()

                def cancel(self, runtime: str) -> None:
                    provider_gateway.cancel(runtime)

                def close(self) -> None:
                    provider_gateway.close()

            coordinator = indicator.RefreshCoordinator(
                ObservedGateway(),
                _settings(codex="manual"),
                settings_store=AcceptingSettingsStore(),
            )
            environment = {
                "OWNER_LEDGER": str(ledger),
                "OWNER_READY": str(ready),
            }
            try:
                with patch.dict(os.environ, environment, clear=False):
                    coordinator.request(("codex",))
                    deadline = time.monotonic() + 5
                    while not ready.exists():
                        if time.monotonic() >= deadline:
                            self.fail("capability owner did not start")
                        threading.Event().wait(0.01)
                    provider_pid = int(ready.read_text(encoding="utf-8"))
                    with executor._lock:
                        handle = executor._active["capability:codex"]
                        self.assertIsNotNone(handle.process)
                        supervisor_pid = handle.process.pid

                    started = time.monotonic()
                    self.assertTrue(coordinator.update_settings(_settings()))
                    self.assertLess(time.monotonic() - started, 0.25)
                    self.assertTrue(fetch_finished.wait(5))
                    _assert_pid_absent(provider_pid)
                    _assert_pid_absent(supervisor_pid)

                calls = [
                    json.loads(line)
                    for line in ledger.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(calls, [["usage", "--help"]])
                self.assertEqual(coordinator.snapshot().runtimes, ())
            finally:
                coordinator.close()

    def test_bdd_r07_owner_disable_preserves_probe_for_enabled_waiter(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            ledger = root / "ledger.jsonl"
            ready = root / "capability-ready"
            release = root / "capability-release"
            binary = root / "codexbar"
            binary.write_text(
                """#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
with open(os.environ['SHARED_LEDGER'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(args) + '\\n')
if args == ['usage', '--help']:
    pathlib.Path(os.environ['SHARED_READY']).write_text(str(os.getpid()), encoding='utf-8')
    deadline = time.monotonic() + 10
    while not pathlib.Path(os.environ['SHARED_RELEASE']).exists():
        if time.monotonic() >= deadline:
            raise SystemExit(70)
        time.sleep(0.01)
    print('--provider codex|grok|claude|zai --json-only --no-color')
    raise SystemExit(0)
provider = args[args.index('--provider') + 1]
print(json.dumps([{
    'provider': provider,
    'usage': {'primary': {'usedPercent': 9}},
}]))
""",
                encoding="utf-8",
            )
            binary.chmod(0o755)
            executor = indicator.ProcessExecutor(
                executable=binary, supervisor_path=MODULE_PATH
            )
            gateway = indicator.ProviderGateway(
                executor, root / "missing-claude-settings.json"
            )
            sink = SnapshotSink()
            coordinator = indicator.RefreshCoordinator(
                gateway,
                _settings(codex="manual", grok="manual"),
                settings_store=AcceptingSettingsStore(),
                on_snapshot=sink,
            )
            environment = {
                "SHARED_LEDGER": str(ledger),
                "SHARED_READY": str(ready),
                "SHARED_RELEASE": str(release),
            }
            try:
                with patch.dict(os.environ, environment, clear=False):
                    coordinator.request(("codex",))
                    deadline = time.monotonic() + 5
                    while not ready.exists():
                        if time.monotonic() >= deadline:
                            self.fail("shared capability probe did not start")
                        threading.Event().wait(0.01)
                    provider_pid = int(ready.read_text(encoding="utf-8"))
                    with executor._lock:
                        handle = executor._active["capability:codex"]
                        self.assertIsNotNone(handle.process)
                        supervisor_pid = handle.process.pid

                    coordinator.request(("grok",))
                    deadline = time.monotonic() + 3
                    while True:
                        with gateway._capability_state_lock:
                            waiter_registered = "grok" in {
                                request.runtime
                                for request in gateway._capability_waiters.values()
                            }
                        if waiter_registered:
                            break
                        if time.monotonic() >= deadline:
                            self.fail("enabled capability waiter was not registered")
                        threading.Event().wait(0.01)

                    self.assertTrue(
                        coordinator.update_settings(_settings(grok="manual"))
                    )
                    self.assertTrue(Path(f"/proc/{provider_pid}").exists())
                    self.assertTrue(Path(f"/proc/{supervisor_pid}").exists())
                    release.touch()
                    final = sink.wait_for(
                        lambda snapshot: (
                            len(snapshot.runtimes) == 1
                            and _runtime(snapshot, "grok").state == "good"
                            and not _runtime(snapshot, "grok").refreshing
                        ),
                        timeout=5,
                    )
                    self.assertEqual(_runtime(final, "grok").usage.primary.percent, 9)

                calls = [
                    json.loads(line)
                    for line in ledger.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(calls.count(["usage", "--help"]), 1)
                self.assertNotIn(
                    [
                        "usage",
                        "--provider",
                        "codex",
                        "--json-only",
                        "--no-color",
                    ],
                    calls,
                )
                self.assertIn(
                    [
                        "usage",
                        "--provider",
                        "grok",
                        "--json-only",
                        "--no-color",
                    ],
                    calls,
                )
            finally:
                release.touch()
                coordinator.close()

    def test_bdd_r07_disable_during_claude_route_starts_no_subordinate_process(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            ledger = root / "unexpected-process"
            binary = root / "codexbar"
            binary.write_text(
                '#!/bin/sh\n: > "$PRE_ROUTE_LEDGER"\nexit 99\n',
                encoding="utf-8",
            )
            binary.chmod(0o755)
            executor = indicator.ProcessExecutor(
                executable=binary, supervisor_path=MODULE_PATH
            )
            provider_gateway = indicator.ProviderGateway(
                executor, root / "claude-settings.json"
            )
            route_entered = threading.Event()
            release_route = threading.Event()
            fetch_finished = threading.Event()

            def gated_route() -> tuple[str, None]:
                route_entered.set()
                if not release_route.wait(5):
                    raise AssertionError("Claude route barrier was not released")
                return "claude", None

            class ObservedGateway:
                def reserve(self, runtime: str):
                    return provider_gateway.reserve(runtime)

                def fetch(self, runtime: str, reservation: object):
                    try:
                        return provider_gateway.fetch(runtime, reservation)
                    finally:
                        fetch_finished.set()

                def cancel(self, runtime: str) -> None:
                    provider_gateway.cancel(runtime)

                def close(self) -> None:
                    provider_gateway.close()

            coordinator = indicator.RefreshCoordinator(
                ObservedGateway(),
                _settings(claude="manual"),
                settings_store=AcceptingSettingsStore(),
            )
            try:
                with (
                    patch.dict(
                        os.environ,
                        {"PRE_ROUTE_LEDGER": str(ledger)},
                        clear=False,
                    ),
                    patch.object(
                        provider_gateway, "_claude_route", side_effect=gated_route
                    ),
                ):
                    coordinator.request(("claude",))
                    self.assertTrue(route_entered.wait(3))
                    self.assertTrue(coordinator.update_settings(_settings()))
                    release_route.set()
                    self.assertTrue(fetch_finished.wait(3))

                self.assertFalse(ledger.exists())
                self.assertEqual(coordinator.snapshot().runtimes, ())
            finally:
                release_route.set()
                coordinator.close()

    def test_bdd_r08_reenable_capability_waiter_survives_old_request_cleanup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            binary = Path(raw_tmp) / "codexbar"
            binary.write_bytes(b"executable identity")
            binary.chmod(0o755)
            old_help_entered = threading.Event()
            release_old_help = threading.Event()

            class GatedCapabilityExecutor:
                executable = binary

                def __init__(self) -> None:
                    self.calls: list[tuple[str, tuple[str, ...]]] = []
                    self.help_count = 0
                    self.lock = threading.Lock()

                def reserve(self, _runtime: str) -> object:
                    return object()

                def release(self, _handle: object) -> None:
                    pass

                def cancel(self, _runtime: str) -> None:
                    pass

                def close(self) -> None:
                    release_old_help.set()

                def run(self, request: object, _handle: object | None = None):
                    argv = tuple(request.argv)
                    self.calls.append((request.runtime, argv[1:]))
                    if argv[1:] == ("usage", "--help"):
                        with self.lock:
                            self.help_count += 1
                            call_number = self.help_count
                        if call_number == 1:
                            old_help_entered.set()
                            if not release_old_help.wait(5):
                                raise AssertionError("old capability barrier timed out")
                            raise indicator.ProcessFailure(
                                "cancelled", "provider command was cancelled"
                            )
                        return indicator.ProcessResult(
                            0,
                            b"--provider codex|grok|claude|zai --json-only --no-color",
                            b"",
                        )
                    return indicator.ProcessResult(
                        0,
                        b'[{"provider":"codex","usage":{"primary":{"usedPercent":17}}}]',
                        b"",
                    )

            executor = GatedCapabilityExecutor()
            gateway = indicator.ProviderGateway(
                executor, Path(raw_tmp) / "missing-claude-settings.json"
            )
            sink = SnapshotSink()
            coordinator = indicator.RefreshCoordinator(
                gateway,
                _settings(codex="manual"),
                settings_store=AcceptingSettingsStore(),
                on_snapshot=sink,
            )
            try:
                coordinator.request(("codex",))
                self.assertTrue(old_help_entered.wait(3))
                self.assertTrue(coordinator.update_settings(_settings()))
                self.assertTrue(coordinator.update_settings(_settings(codex="manual")))
                coordinator.request(("codex",))

                deadline = time.monotonic() + 3
                while True:
                    with gateway._capability_state_lock:
                        current_waiters = tuple(gateway._capability_waiters.values())
                    if len(current_waiters) == 1 and not current_waiters[0].cancelled:
                        break
                    if time.monotonic() >= deadline:
                        self.fail("new capability waiter was not registered")
                    threading.Event().wait(0.01)

                release_old_help.set()
                completed = sink.wait_for(
                    lambda snapshot: (
                        _runtime(snapshot, "codex").state == "good"
                        and not _runtime(snapshot, "codex").refreshing
                    ),
                    timeout=5,
                )
                self.assertEqual(_runtime(completed, "codex").usage.primary.percent, 17)
                help_calls = [
                    call for call in executor.calls if call[1] == ("usage", "--help")
                ]
                quota_calls = [
                    call for call in executor.calls if "--provider" in call[1]
                ]
                self.assertEqual(len(help_calls), 2)
                self.assertEqual(len(quota_calls), 1)
            finally:
                release_old_help.set()
                coordinator.close()

    def test_bdd_r08_reenable_renders_only_new_generation(self) -> None:
        old = BlockingOutcome(_success("codex", 99), release_on_cancel=False)
        new = BlockingOutcome(_success("codex", 12))
        gateway = ScriptedGateway(codex=[old, new])
        sink = SnapshotSink()
        store = AcceptingSettingsStore()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            settings_store=store,
            on_snapshot=sink,
        )
        try:
            coordinator.request(("codex",))
            self.assertTrue(old.entered.wait(3))
            self.assertTrue(coordinator.update_settings(_settings()))
            self.assertTrue(coordinator.update_settings(_settings(codex="manual")))
            self.assertEqual(store.saved, [_settings(), _settings(codex="manual")])
            coordinator.request(("codex",))
            self.assertTrue(new.entered.wait(3))
            new.release.set()
            current = sink.wait_for(
                lambda value: _runtime(value, "codex").usage == _usage(12)
            )
            old.release.set()
            self.assertTrue(old.finished.wait(3))
            self.assertEqual(coordinator.snapshot(), current)
        finally:
            old.release.set()
            new.release.set()
            coordinator.close()

    def test_bdd_r09_older_failure_cannot_replace_new_success(self) -> None:
        old = BlockingOutcome(_failure("codex", "old failure"), release_on_cancel=False)
        gateway = ScriptedGateway(codex=[old, _success("codex", 31)])
        sink = SnapshotSink()
        store = AcceptingSettingsStore()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            settings_store=store,
            on_snapshot=sink,
        )
        try:
            coordinator.request(("codex",))
            self.assertTrue(old.entered.wait(3))
            self.assertTrue(coordinator.update_settings(_settings()))
            self.assertTrue(coordinator.update_settings(_settings(codex="manual")))
            self.assertEqual(store.saved, [_settings(), _settings(codex="manual")])
            coordinator.request(("codex",))
            current = sink.wait_for(
                lambda value: _runtime(value, "codex").usage == _usage(31)
            )
            old.release.set()
            self.assertTrue(old.finished.wait(3))
            self.assertEqual(coordinator.snapshot(), current)
            self.assertIsNone(_runtime(current, "codex").error)
        finally:
            old.release.set()
            coordinator.close()

    def test_bdd_r10_empty_failure_uses_error_then_retains_last_known_good(
        self,
    ) -> None:
        submitter = ManualSubmitter()
        gateway = ScriptedGateway(
            codex=[
                _failure("codex", "empty provider response", kind="empty"),
                _success("codex", 40),
                _failure("codex", "empty provider response", kind="empty"),
            ]
        )
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            worker_submit=submitter,
        )
        try:
            coordinator.request_manual()
            submitter.run("codex")
            without_good = _runtime(coordinator.snapshot(), "codex")
            self.assertEqual(without_good.state, "error")
            self.assertIsNone(without_good.usage)

            coordinator.request_manual()
            submitter.run("codex")
            self.assertEqual(_runtime(coordinator.snapshot(), "codex").state, "good")
            coordinator.request_manual()
            submitter.run("codex")
            stale = _runtime(coordinator.snapshot(), "codex")
            self.assertEqual(stale.state, "stale")
            self.assertEqual(stale.usage, _usage(40))
            self.assertEqual(stale.error, "empty provider response")
        finally:
            coordinator.close()

    def test_bdd_z10_zai_failure_isolated_from_parallel_successes(self) -> None:
        claude_failure = BlockingOutcome(
            _failure("claude", "Z.AI configuration error", kind="config")
        )
        gateway = ScriptedGateway(
            codex=[_success("codex", 10)],
            grok=[_success("grok", 20)],
            claude=[claude_failure],
        )
        sink = SnapshotSink()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual", grok="manual", claude="manual"),
            on_snapshot=sink,
        )
        try:
            coordinator.request_manual()
            self.assertTrue(claude_failure.entered.wait(3))
            successes = sink.wait_for(
                lambda value: (
                    _runtime(value, "codex").state == "good"
                    and _runtime(value, "grok").state == "good"
                )
            )
            self.assertEqual(_runtime(successes, "claude").state, "pending")
            self.assertIsNone(successes.global_error)
            claude_failure.release.set()
            failed = sink.wait_for(
                lambda value: _runtime(value, "claude").state == "error"
            )
            self.assertEqual(_runtime(failed, "codex").state, "good")
            self.assertEqual(_runtime(failed, "grok").state, "good")
            self.assertIsNone(_runtime(failed, "claude").usage)
            self.assertIsNone(failed.global_error)
        finally:
            claude_failure.release.set()
            coordinator.close()

        submitter = ManualSubmitter()
        gateway = ScriptedGateway(
            claude=[
                _success("claude", 40, secondary=50),
                _failure("claude", "Z.AI configuration error", kind="config"),
            ]
        )
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(claude="manual"),
            worker_submit=submitter,
        )
        try:
            coordinator.request_manual()
            submitter.run("claude")
            coordinator.request_manual()
            submitter.run("claude")
            stale = _runtime(coordinator.snapshot(), "claude")
            self.assertEqual(stale.state, "stale")
            self.assertEqual(stale.usage, _usage(40, secondary=50))
            self.assertEqual(stale.error, "Z.AI configuration error")
        finally:
            coordinator.close()

    def test_bdd_r11_shutdown_invalidates_callbacks_and_pending_rerun(self) -> None:
        submitter = ManualSubmitter()
        gateway = ScriptedGateway(codex=[_success("codex", 76)])
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            worker_submit=submitter,
        )
        coordinator.request_manual()
        self.assertEqual(len(submitter.jobs), 1)
        coordinator.close()
        submitter.run("codex")
        self.assertEqual(gateway.fetch_attempts, [])

        active = BlockingOutcome(_success("codex", 77))
        gateway = ScriptedGateway(codex=[active, _success("codex", 88)])
        sink = SnapshotSink()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            on_snapshot=sink,
        )
        coordinator.request(("codex",))
        self.assertTrue(active.entered.wait(3))
        coordinator.request(("codex",))
        published_before_close = tuple(sink.snapshots)
        coordinator.close()
        self.assertTrue(active.finished.wait(3))
        self.assertTrue(gateway.closed)
        self.assertEqual(gateway.cancelled, [])
        self.assertEqual(gateway.calls, ["codex"])
        self.assertEqual(tuple(sink.snapshots), published_before_close)

        submitter = ManualSubmitter()
        callback_queue = ManualCallbackQueue()
        gateway = ScriptedGateway(codex=[_success("codex", 91)])
        sink = SnapshotSink()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            worker_submit=submitter,
            callback_submit=callback_queue,
            on_snapshot=sink,
        )
        coordinator.request(("codex",))
        coordinator.request(("codex",))
        submitter.run("codex")
        self.assertNotEqual(callback_queue.callbacks, [])
        published_before_close = tuple(sink.snapshots)
        coordinator.close()
        callback_queue.run_all()
        while submitter.jobs:
            submitter.run()
        self.assertTrue(gateway.closed)
        self.assertEqual(gateway.calls, ["codex"])
        self.assertEqual(tuple(sink.snapshots), published_before_close)
        self.assertEqual(submitter.jobs, [])

        class FailOnceSubmitter:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, _runtime: str, job) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("injected worker submission failure")
                job()

        submitter = FailOnceSubmitter()
        gateway = ScriptedGateway(codex=[_success("codex", 92)])
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            worker_submit=submitter,
        )
        try:
            coordinator.request_manual()
            first = _runtime(coordinator.snapshot(), "codex")
            self.assertEqual(first.state, "error")
            self.assertFalse(first.refreshing)
            self.assertEqual(gateway.fetch_attempts, [])

            coordinator.request_manual()
            second = _runtime(coordinator.snapshot(), "codex")
            self.assertEqual(second.state, "good")
            self.assertEqual(second.usage, _usage(92))
            self.assertEqual(gateway.calls, ["codex"])
            self.assertEqual(gateway.fetch_attempts, ["codex"])
        finally:
            coordinator.close()

        class FailOnceCallbackSubmitter:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, callback) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("injected callback submission failure")
                callback()

        callback_submitter = FailOnceCallbackSubmitter()
        submitter = ManualSubmitter()
        gateway = ScriptedGateway(codex=[_success("codex", 93)])
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            worker_submit=submitter,
            callback_submit=callback_submitter,
        )
        try:
            coordinator.request_manual()
            submitter.run("codex")
            recovered = _runtime(coordinator.snapshot(), "codex")
            self.assertEqual(recovered.state, "good")
            self.assertEqual(recovered.usage, _usage(93))
            self.assertFalse(recovered.refreshing)
        finally:
            coordinator.close()

        class FailOnceSnapshotSink:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, _snapshot: object) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("injected snapshot callback failure")

        snapshot_sink = FailOnceSnapshotSink()
        submitter = ManualSubmitter()
        gateway = ScriptedGateway(codex=[_success("codex", 94)])
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual"),
            worker_submit=submitter,
            on_snapshot=snapshot_sink,
        )
        try:
            coordinator.request_manual()
            self.assertEqual(len(submitter.jobs), 1)
            submitter.run("codex")
            recovered = _runtime(coordinator.snapshot(), "codex")
            self.assertEqual(recovered.state, "good")
            self.assertEqual(recovered.usage, _usage(94))
            self.assertEqual(snapshot_sink.calls, 2)
        finally:
            coordinator.close()

    def test_bdd_r12_all_27_selection_modes(self) -> None:
        for mode_tuple in itertools.product(("off", "manual", "auto"), repeat=3):
            modes = dict(zip(RUNTIMES, mode_tuple))
            with self.subTest(modes=modes):
                submitter = ManualSubmitter()
                scripts = {
                    runtime: [_success(runtime, 10), _success(runtime, 20)]
                    for runtime, mode in modes.items()
                    if mode != "off"
                }
                gateway = ScriptedGateway(**scripts)
                coordinator = indicator.RefreshCoordinator(
                    gateway,
                    _settings(**modes),
                    worker_submit=submitter,
                )
                try:
                    expected_manual = {
                        runtime for runtime, mode in modes.items() if mode != "off"
                    }
                    expected_auto = {
                        runtime for runtime, mode in modes.items() if mode == "auto"
                    }
                    coordinator.request_manual()
                    while submitter.jobs:
                        submitter.run()
                    self.assertEqual(Counter(gateway.calls), Counter(expected_manual))

                    before_auto = Counter(gateway.calls)
                    coordinator.request_auto()
                    while submitter.jobs:
                        submitter.run()
                    self.assertEqual(
                        Counter(gateway.calls) - before_auto,
                        Counter(expected_auto),
                    )
                    snapshot = coordinator.snapshot()
                    self.assertEqual(
                        tuple(item.runtime for item in snapshot.runtimes),
                        tuple(
                            runtime for runtime in RUNTIMES if modes[runtime] != "off"
                        ),
                    )
                finally:
                    coordinator.close()

    def test_bdd_e04_provider_outage_recovery_has_no_hidden_retry(self) -> None:
        failures = (
            _failure("codex", "offline", kind="transport"),
            _failure("codex", "timeout", kind="timeout"),
            _failure("codex", "rate limited", kind="rate_limit"),
        )
        for failure in failures:
            with self.subTest(kind=failure.kind):
                submitter = ManualSubmitter()
                gateway = ScriptedGateway(
                    codex=[_success("codex", 10), failure, _success("codex", 20)]
                )
                coordinator = indicator.RefreshCoordinator(
                    gateway,
                    _settings(codex="manual"),
                    worker_submit=submitter,
                )
                try:
                    coordinator.request_manual()
                    submitter.run("codex")
                    coordinator.request_manual()
                    submitter.run("codex")
                    stale = _runtime(coordinator.snapshot(), "codex")
                    self.assertEqual(stale.state, "stale")
                    self.assertEqual(stale.usage, _usage(10))
                    self.assertEqual(stale.error, failure.message)
                    self.assertEqual(gateway.calls, ["codex", "codex"])
                    self.assertEqual(submitter.jobs, [])

                    coordinator.request_manual()
                    submitter.run("codex")
                    recovered = _runtime(coordinator.snapshot(), "codex")
                    self.assertEqual(recovered.state, "good")
                    self.assertEqual(recovered.usage, _usage(20))
                    self.assertIsNone(recovered.error)
                    self.assertEqual(gateway.calls, ["codex", "codex", "codex"])
                finally:
                    coordinator.close()


class FakePanelIndicator:
    def __init__(self) -> None:
        self.labels: list[tuple[str, str]] = []
        self.menu = None

    def set_label(self, label: str, guide: str) -> None:
        self.labels.append((label, guide))

    def set_menu(self, menu) -> None:
        self.menu = menu


def _snapshot(*runtimes, global_error=None):
    return indicator.ViewSnapshot(runtimes=tuple(runtimes), global_error=global_error)


def _runtime_snapshot(
    runtime: str,
    state: str,
    *,
    usage=None,
    error: str | None = None,
    refreshing: bool = False,
    source: str | None = None,
):
    return indicator.RuntimeSnapshot(
        runtime=runtime,
        state=state,
        usage=usage,
        error=error,
        refreshing=refreshing,
        source=source,
    )


def _visible_menu_labels(view) -> list[str]:
    labels: list[str] = []
    for child in view.menu.get_children():
        if child.get_visible() and hasattr(child, "get_label"):
            label = child.get_label()
            if label:
                labels.append(label)
    return labels


def _pump_gtk_until(event: threading.Event, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    context = indicator.GLib.MainContext.default()
    while not event.is_set():
        while context.pending():
            context.iteration(False)
        if event.is_set():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("timed out waiting for GTK callback")
        event.wait(min(remaining, 0.01))


def _dialog_text(dialog) -> str:
    pending = [dialog.get_content_area()]
    text_views = []
    while pending:
        widget = pending.pop()
        if isinstance(widget, indicator.Gtk.TextView):
            text_views.append(widget)
        if isinstance(widget, indicator.Gtk.Container):
            pending.extend(widget.get_children())
    if len(text_views) != 1:
        raise AssertionError(f"expected one details TextView, got {len(text_views)}")
    buffer = text_views[0].get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)


class IndicatorViewRedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        initialized, _ = indicator.Gtk.init_check([])
        if not initialized:
            raise AssertionError(
                "GTK projection tests require Xvfb; skipping them is not allowed"
            )

    def setUp(self) -> None:
        self.panel = FakePanelIndicator()
        self.view = indicator.IndicatorView(self.panel)

    def tearDown(self) -> None:
        self.view.close()
        while indicator.Gtk.events_pending():
            indicator.Gtk.main_iteration_do(False)

    def test_bdd_v01_runtime_rows_are_dynamic_and_fixed_order(self) -> None:
        self.view.render(
            _snapshot(
                _runtime_snapshot("claude", "pending"),
                _runtime_snapshot("grok", "error", error="offline"),
            )
        )
        labels = _visible_menu_labels(self.view)
        runtime_rows = [
            label
            for label in labels
            if label.startswith(("Codex:", "Grok:", "Claude:"))
        ]
        self.assertEqual(runtime_rows, ["Grok: ERROR: offline", "Claude: pending"])

        self.view.render(
            _snapshot(
                _runtime_snapshot("codex", "pending"),
                _runtime_snapshot("grok", "good", usage=_usage(12, secondary=34)),
                _runtime_snapshot("claude", "error", error="offline"),
            )
        )
        mixed_rows = _visible_menu_labels(self.view)[:4]
        self.assertEqual(mixed_rows[0], "Codex: pending")
        self.assertIn("Grok Session", mixed_rows[1])
        self.assertIn("Grok Week", mixed_rows[2])
        self.assertEqual(mixed_rows[3], "Claude: ERROR: offline")

        self.view.render(
            _snapshot(_runtime_snapshot("codex", "good", usage=indicator.Usage()))
        )
        labels = _visible_menu_labels(self.view)
        self.assertIn("Codex: no quota windows", labels)
        self.assertEqual(self.panel.labels[-1][0], "CxW --")

    def test_bdd_v02_panel_label_contract_and_error_marker(self) -> None:
        self.view.render(
            _snapshot(_runtime_snapshot("codex", "good", usage=_usage(1, secondary=37)))
        )
        self.assertEqual(self.panel.labels[-1][0], "CxW 37%")
        self.assertNotIn("!", self.panel.labels[-1][0])
        self.assertNotIn("GkS", self.panel.labels[-1][0])
        self.assertNotIn("Cl", self.panel.labels[-1][0])

        self.view.render(
            _snapshot(
                _runtime_snapshot("codex", "good", usage=_usage(1, secondary=37)),
                _runtime_snapshot("grok", "stale", usage=_usage(12), error="429"),
                _runtime_snapshot(
                    "claude", "good", usage=_usage(18, secondary=96), source="zai"
                ),
            )
        )
        self.assertEqual(self.panel.labels[-1][0], "CxW 37%  GkS 12%  Cl 18%/96% !")
        self.assertEqual(self.panel.labels[-1][1], "CxW 100%  GkS 100%  Cl 100%/100% !")

        self.view.render(_snapshot(_runtime_snapshot("grok", "error", error="offline")))
        self.assertEqual(self.panel.labels[-1][0], "GkS -- !")

    def test_bdd_v03_error_rows_survive_normal_row_cap(self) -> None:
        extras = tuple(
            indicator.UsageExtra(
                window=indicator.UsageWindow(percent=index, window_minutes=60),
                provider_title=f"extra-{index}",
            )
            for index in range(indicator.MAX_LIMIT_ROWS + 2)
        )
        usage = indicator.Usage(primary=_usage(1).primary, extras=extras)
        self.view.render(
            _snapshot(
                _runtime_snapshot("codex", "good", usage=usage),
                _runtime_snapshot("grok", "error", error="provider down"),
                _runtime_snapshot("claude", "error", error="configuration invalid"),
            )
        )
        labels = _visible_menu_labels(self.view)
        self.assertIn("Grok: ERROR: provider down", labels)
        self.assertIn("Claude: ERROR: configuration invalid", labels)

        self.view.render(
            _snapshot(
                _runtime_snapshot("codex", "error", error="x" * 1000),
                _runtime_snapshot("grok", "stale", error="y" * 1000),
                global_error="z" * 1000,
            )
        )
        visible_errors = [
            label
            for label in _visible_menu_labels(self.view)
            if "ERROR" in label or "STALE" in label
        ]
        self.assertEqual(len(visible_errors), 3)
        self.assertTrue(
            all(
                len(message) <= indicator.MAX_DIAGNOSTIC_CHARS
                for message in visible_errors
            )
        )
        self.assertTrue(
            all(
                len(line) <= indicator.MAX_DIAGNOSTIC_CHARS
                for line in _dialog_text(self.view.show_details()).splitlines()
            )
        )

        self.view.render(
            _snapshot(
                _runtime_snapshot(
                    "codex",
                    "good",
                    usage=indicator.Usage(extras=extras),
                ),
                _runtime_snapshot("grok", "good", usage=_usage(21)),
                _runtime_snapshot("claude", "good", usage=_usage(31), source="zai"),
            )
        )
        capped_rows = _visible_menu_labels(self.view)[: indicator.MAX_LIMIT_ROWS]
        self.assertEqual(len(capped_rows), indicator.MAX_LIMIT_ROWS)
        self.assertTrue(any("Grok Session" in row for row in capped_rows))
        self.assertTrue(any("Claude (Z.AI) 5h" in row for row in capped_rows))

    def test_bdd_v04_malformed_optional_reset_is_omitted_without_loop_failure(
        self,
    ) -> None:
        class MalformedDisplayField:
            def __str__(self) -> str:
                return "MALFORMED_DISPLAY_FIELD_MUST_BE_OMITTED"

        malformed = indicator.UsageWindow(
            percent=42,
            window_minutes=300,
            reset_text=MalformedDisplayField(),
        )
        self.view.render(
            _snapshot(
                _runtime_snapshot(
                    "codex",
                    "good",
                    usage=indicator.Usage(secondary=malformed),
                )
            )
        )
        labels = _visible_menu_labels(self.view)
        rendered = next(label for label in labels if "42%" in label)
        self.assertEqual(rendered, "████░░░░░░   42%  Codex Week")
        dialog = self.view.show_details()
        self.assertNotIn(
            "MALFORMED_DISPLAY_FIELD_MUST_BE_OMITTED", _dialog_text(dialog)
        )

        safe_reset = indicator.sanitize_diagnostic("before-\ud800-after")
        safe_reset.encode("utf-8", errors="strict")
        self.view.render(
            _snapshot(
                _runtime_snapshot(
                    "codex",
                    "good",
                    usage=indicator.Usage(
                        secondary=indicator.UsageWindow(
                            percent=43,
                            window_minutes=300,
                            reset_text=safe_reset,
                        )
                    ),
                )
            )
        )
        safe_label = next(
            label for label in _visible_menu_labels(self.view) if "43%" in label
        )
        safe_label.encode("utf-8", errors="strict")
        callback_ran = threading.Event()
        indicator.GLib.idle_add(callback_ran.set)
        _pump_gtk_until(callback_ran)

    def test_bdd_v05_details_dialog_is_native_nonmodal_and_main_loop_remains_live(
        self,
    ) -> None:
        self.view.render(
            _snapshot(_runtime_snapshot("codex", "good", usage=_usage(10)))
        )
        callback_ran = threading.Event()
        indicator.GLib.idle_add(callback_ran.set)
        with (
            patch.object(
                indicator.Gtk.Dialog, "run", side_effect=AssertionError("nested loop")
            ),
            patch.object(
                indicator.subprocess,
                "Popen",
                side_effect=AssertionError("external details process"),
            ),
        ):
            dialog = self.view.show_details()
        self.assertIs(dialog, self.view.details_dialog)
        self.assertFalse(dialog.get_modal())
        self.assertTrue(dialog.get_visible())
        text = _dialog_text(dialog)
        self.assertIn("Codex session: 10%", text)
        self.assertNotIn("Raw JSON", text)
        self.assertNotIn("RuntimeSnapshot(", text)
        self.assertLessEqual(len(text), indicator.MAX_DETAILS_CHARS)
        _pump_gtk_until(callback_ran)

    def test_bdd_v05_gateway_details_cross_coordinator_into_native_view(self) -> None:
        class JsonExecutor:
            executable = Path("/bin/true")

            def reserve(self, _runtime: str) -> object:
                return object()

            def release(self, _handle: object) -> None:
                pass

            def run(self, request: object, _handle: object | None = None) -> object:
                if request.argv[1:] == ("usage", "--help"):
                    return indicator.ProcessResult(
                        0,
                        b"--provider codex|grok|claude|zai --json-only --no-color\n",
                        b"",
                    )
                provider = request.argv[request.argv.index("--provider") + 1]
                usage = (
                    {"secondary": {"usedPercent": 37}}
                    if provider == "codex"
                    else {
                        "primary": {"usedPercent": 12},
                        "secondary": {"usedPercent": 37},
                    }
                )
                return indicator.ProcessResult(
                    0,
                    json.dumps([{"provider": provider, "usage": usage}]).encode(),
                    b"",
                )

            def cancel(self, _runtime: str) -> None:
                pass

            def close(self) -> None:
                pass

        gateway = indicator.ProviderGateway(
            JsonExecutor(), Path("/missing-claude-settings.json")
        )
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(codex="manual", grok="manual"),
            on_snapshot=self.view.render,
            worker_submit=lambda _runtime, job: job(),
        )
        try:
            coordinator.request_manual()

            snapshot = coordinator.snapshot()
            self.assertEqual(_runtime(snapshot, "codex").usage.secondary.percent, 37)
            self.assertEqual(_runtime(snapshot, "grok").usage.primary.percent, 12)
            self.assertIn("Codex Week", "\n".join(_visible_menu_labels(self.view)))
            detail_lines = _dialog_text(self.view.show_details()).splitlines()
            self.assertIn("Codex weekly: 37%", detail_lines)
            self.assertIn("Grok session: 12%", detail_lines)
            self.assertIn("Grok weekly: 37%", detail_lines)
        finally:
            coordinator.close()

    def test_bdd_v06_close_destroys_details_dialog_synchronously(self) -> None:
        dialog = self.view.show_details()
        destroyed = threading.Event()
        dialog.connect("destroy", lambda *_: destroyed.set())
        self.assertTrue(dialog.get_visible())
        self.view.close()
        self.assertTrue(destroyed.is_set())
        self.assertIsNone(self.view.details_dialog)

    def test_bdd_v06_details_cannot_be_recreated_after_close(self) -> None:
        self.view.close()

        self.assertIsNone(self.view.show_details())
        self.assertIsNone(self.view.details_dialog)

    def test_bdd_v06_runtime_toggle_controls_persist_and_menu_quit_works(
        self,
    ) -> None:
        gateway = ScriptedGateway()
        store = AcceptingSettingsStore()
        coordinator = indicator.RefreshCoordinator(
            gateway, _settings(), settings_store=store
        )
        quit_requested = threading.Event()
        view = indicator.IndicatorView(
            FakePanelIndicator(),
            settings=_settings(),
            on_setting_toggle=lambda runtime, key, active, complete: complete(
                coordinator.update_setting(runtime, key, active)
            ),
            on_quit=quit_requested.set,
        )
        try:
            controls = {
                item.get_label(): item
                for item in view.menu.get_children()
                if isinstance(item, indicator.Gtk.CheckMenuItem)
            }
            self.assertEqual(
                set(controls),
                {
                    "Poll Codex",
                    "Auto-refresh Codex",
                    "Poll Grok",
                    "Auto-refresh Grok",
                    "Poll Claude",
                    "Auto-refresh Claude",
                },
            )
            controls["Poll Claude"].set_active(True)
            controls["Auto-refresh Claude"].set_active(True)
            controls["Poll Claude"].set_active(False)
            self.assertEqual(len(store.saved), 3)
            self.assertEqual(
                store.saved[-1]["runtimes"]["claude"],
                {"poll": False, "autoRefresh": False},
            )
            self.assertEqual(coordinator.snapshot().runtimes, ())

            quit_item = next(
                item
                for item in view.menu.get_children()
                if getattr(item, "get_label", lambda: None)() == "Quit"
            )
            quit_item.activate()
            self.assertTrue(quit_requested.is_set())
        finally:
            view.close()
            coordinator.close()

    def test_bdd_s08_failed_save_rolls_back_checkbox_and_surfaces_bounded_error(
        self,
    ) -> None:
        class FailingStore:
            def save(self, _settings: object) -> None:
                raise indicator.SettingsFailure("x" * 1000)

        gateway = ScriptedGateway()
        holder: dict[str, object] = {}
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(),
            settings_store=FailingStore(),
            on_snapshot=lambda snapshot: holder["view"].render(snapshot),
        )
        view = indicator.IndicatorView(
            FakePanelIndicator(),
            settings=_settings(),
            on_setting_toggle=lambda runtime, key, active, complete: complete(
                coordinator.update_setting(runtime, key, active)
            ),
        )
        holder["view"] = view
        try:
            poll_codex = next(
                item
                for item in view.menu.get_children()
                if isinstance(item, indicator.Gtk.CheckMenuItem)
                and item.get_label() == "Poll Codex"
            )

            poll_codex.set_active(True)

            self.assertFalse(poll_codex.get_active())
            snapshot = coordinator.snapshot()
            self.assertLessEqual(len(snapshot.global_error), 512)
            labels = _visible_menu_labels(view)
            self.assertTrue(
                any(label.startswith("CodexBar: ERROR:") for label in labels), labels
            )
            self.assertEqual(gateway.calls, [])
        finally:
            view.close()
            coordinator.close()

    def test_bdd_s08_slow_save_does_not_block_toggle_or_menu_quit(self) -> None:
        save_entered = threading.Event()
        release_save = threading.Event()
        quit_requested = threading.Event()

        class SlowStore:
            def save(self, _settings: dict[str, object]) -> None:
                save_entered.set()
                if not release_save.wait(5):
                    raise AssertionError("settings save barrier was not released")

        coordinator = indicator.RefreshCoordinator(
            ScriptedGateway(), _settings(), settings_store=SlowStore()
        )
        application = indicator.CodexBarApplication(
            coordinator=coordinator, view=object()
        )

        view = indicator.IndicatorView(
            FakePanelIndicator(),
            settings=_settings(),
            on_setting_toggle=application._update_setting_async,
            on_quit=quit_requested.set,
        )
        try:
            poll_codex = next(
                item
                for item in view.menu.get_children()
                if isinstance(item, indicator.Gtk.CheckMenuItem)
                and item.get_label() == "Poll Codex"
            )
            quit_item = next(
                item
                for item in view.menu.get_children()
                if getattr(item, "get_label", lambda: None)() == "Quit"
            )

            started = time.monotonic()
            poll_codex.set_active(True)
            self.assertLess(time.monotonic() - started, 0.25)
            self.assertTrue(save_entered.wait(3))
            self.assertFalse(poll_codex.get_sensitive())

            quit_item.activate()
            self.assertTrue(quit_requested.is_set())

            main_loop_progressed = threading.Event()
            indicator.GLib.idle_add(lambda: (main_loop_progressed.set(), False)[1])
            _pump_gtk_until(main_loop_progressed)

            release_save.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                while indicator.Gtk.events_pending():
                    indicator.Gtk.main_iteration_do(False)
                if poll_codex.get_active() and poll_codex.get_sensitive():
                    break
                time.sleep(0.01)
            self.assertTrue(poll_codex.get_active())
            self.assertTrue(poll_codex.get_sensitive())
        finally:
            release_save.set()
            view.close()
            coordinator.close()

    def test_bdd_z09b_filtered_zai_windows_are_not_reconstructed_by_view(self) -> None:
        self.view.render(
            _snapshot(
                _runtime_snapshot(
                    "claude",
                    "good",
                    source="zai",
                    usage=indicator.Usage(primary=_usage(4).primary),
                )
            )
        )
        rendered = "\n".join(_visible_menu_labels(self.view)).lower()
        details = _dialog_text(self.view.show_details()).lower()
        self.assertEqual(self.panel.labels[-1][0], "Cl 4%/--")
        for forbidden in ("mcp", "time", "monthly"):
            self.assertNotRegex(rendered, rf"\b{forbidden}\b")
            self.assertNotRegex(details, rf"\b{forbidden}\b")


class FakeLifecycleCoordinator:
    def __init__(self) -> None:
        self.auto_requests = 0
        self.closed = 0

    def request_auto(self) -> None:
        self.auto_requests += 1

    def close(self) -> None:
        self.closed += 1


class FakeLifecycleView:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class FakeTimerPort:
    def __init__(self) -> None:
        self.added: list[tuple[int, object]] = []
        self.removed: list[int] = []

    def add(self, seconds: int, callback) -> int:
        self.added.append((seconds, callback))
        return 73

    def remove(self, timer_id: int) -> bool:
        self.removed.append(timer_id)
        return True


class FakeApplicationHold:
    def __init__(self) -> None:
        self.holds = 0
        self.releases = 0

    def hold(self) -> None:
        self.holds += 1

    def release(self) -> None:
        self.releases += 1


class LocalInvocationServer:
    def __init__(self, path: Path) -> None:
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(path))
        self.socket.listen(8)

    def accept(self, timeout: float = 3.0) -> tuple[tuple[str, ...], socket.socket]:
        self.socket.settimeout(timeout)
        connection, _ = self.socket.accept()
        connection.settimeout(timeout)
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = connection.recv(128)
            if not chunk:
                raise AssertionError("fake CodexBar closed before recording provider")
            data.extend(chunk)
        return tuple(data.decode("ascii").split()), connection

    def assert_no_pending_call(self) -> None:
        self.socket.setblocking(False)
        try:
            connection, _ = self.socket.accept()
        except (BlockingIOError, TimeoutError):
            return
        else:
            connection.close()
            raise AssertionError("unexpected duplicate provider invocation")
        finally:
            self.socket.setblocking(True)

    def close(self) -> None:
        self.socket.close()


def _write_blocking_fake_codexbar(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import sys

if sys.argv[1:] == ["usage", "--help"]:
    print("--json-only --provider codex grok claude zai")
    raise SystemExit(0)

provider = sys.argv[sys.argv.index("--provider") + 1]
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
connection.connect(os.environ["CODEXBAR_TEST_SOCKET"])
connection.sendall(
    f"{provider} {os.getpid()} {grandchild.pid}\\n".encode("ascii")
)
try:
    if connection.recv(1) != b"1":
        raise SystemExit(2)
    print(json.dumps([{
        "provider": provider,
        "usage": {
            "primary": {"usedPercent": 10},
            "secondary": {"usedPercent": 20},
        },
    }]))
finally:
    grandchild.terminate()
    grandchild.wait(timeout=3)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_lifecycle_helper(path: Path) -> None:
    path.write_text(
        f"""#!/usr/bin/env python3
import importlib.util
import os
import socket
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

ready_dir = os.environ.get("CODEXBAR_TEST_START_READY_DIR")
release_path = os.environ.get("CODEXBAR_TEST_START_RELEASE")
if ready_dir and release_path:
    Path(ready_dir).mkdir(parents=True, exist_ok=True)
    Path(ready_dir, str(os.getpid())).touch()
    deadline = time.monotonic() + 5
    while not Path(release_path).exists():
        if time.monotonic() >= deadline:
            raise SystemExit(70)
        time.sleep(0.01)

module_path = {str(MODULE_PATH)!r}
spec = importlib.util.spec_from_loader(
    "codexbar_lifecycle_helper",
    SourceFileLoader("codexbar_lifecycle_helper", module_path),
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

def report(event):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(os.environ["CODEXBAR_TEST_SOCKET"])
    connection.sendall((event + "\\n").encode("ascii"))
    connection.close()

class Coordinator:
    def request_auto(self):
        report("refresh-complete")

    def close(self):
        pass

class View:
    def close(self):
        pass

def coordinator_factory():
    report("coordinator-created")
    return Coordinator()

def view_factory(*args, **kwargs):
    report("view-created")
    return View()

application = module.CodexBarApplication(
    coordinator_factory=coordinator_factory,
    view_factory=view_factory,
)
raise SystemExit(application.run([]))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _isolated_application_env(root: Path, fake_cli: Path, socket_path: Path):
    allowed = (
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "LANG",
        "PATH",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
    )
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env.update(
        {
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / ".config"),
            "XDG_STATE_HOME": str(root / ".local/state"),
            "CODEXBAR_BIN": str(fake_cli),
            "CODEXBAR_GNOME_CONFIG": str(root / "config.json"),
            "CODEXBAR_TEST_SOCKET": str(socket_path),
            "CODEXBAR_INDICATOR_REFRESH_SECONDS": "30",
        }
    )
    return env


def _start_isolated_application(env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(MODULE_PATH)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _start_lifecycle_helper(path: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(path)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_application_owner(env: dict[str, str]) -> None:
    completed = subprocess.run(
        [
            "gdbus",
            "wait",
            "--session",
            "--timeout=3",
            "io.github.antonshalin76.CodexBarGnome",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"application never owned its D-Bus name: {completed.stderr}"
        )


def _name_has_owner(env: dict[str, str]) -> bool:
    completed = subprocess.run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.freedesktop.DBus",
            "--object-path",
            "/org/freedesktop/DBus",
            "--method",
            "org.freedesktop.DBus.NameHasOwner",
            "io.github.antonshalin76.CodexBarGnome",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=3,
        check=True,
    )
    return "true" in completed.stdout.lower()


def _quit_application(env: dict[str, str]) -> None:
    subprocess.run(
        ["gapplication", "action", "io.github.antonshalin76.CodexBarGnome", "quit"],
        env=env,
        text=True,
        capture_output=True,
        timeout=3,
        check=True,
    )


def _stop_exact_process(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def _assert_pid_absent(pid: int, timeout: float = 3.0) -> None:
    try:
        pidfd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        if not poller.poll(round(timeout * 1000)):
            raise AssertionError(f"process {pid} survived application exit")
    finally:
        os.close(pidfd)
    deadline = time.monotonic() + timeout
    path = Path(f"/proc/{pid}")
    while path.exists():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"process {pid} remained as an unreaped zombie")
        threading.Event().wait(min(remaining, 0.01))


def _parent_pid(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
        if line.startswith("PPid:"):
            return int(line.split()[1])
    raise AssertionError(f"process {pid} has no parent ledger")


def _wait_for_path(path: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path.name}")
        threading.Event().wait(0.01)


class ApplicationLifecycleRedTests(unittest.TestCase):
    def test_bdd_l01_refresh_interval_parser_is_bounded_and_never_raises(self) -> None:
        valid = {None: 300, "": 300, "30": 30, "300": 300, "86400": 86400}
        invalid = ("text", "30.0", "0", "-1", "29", "86401", str(10**100))
        for raw, expected in valid.items():
            with self.subTest(raw=raw):
                warnings: list[str] = []
                self.assertEqual(
                    indicator.parse_refresh_seconds(raw, warnings.append), expected
                )
                self.assertEqual(warnings, [])
        for raw in invalid:
            with self.subTest(raw=raw):
                warnings = []
                self.assertEqual(
                    indicator.parse_refresh_seconds(raw, warnings.append), 300
                )
                self.assertEqual(len(warnings), 1)
                self.assertLessEqual(len(warnings[0]), 512)

        def broken_warning_sink(_message: str) -> None:
            raise BrokenPipeError

        self.assertEqual(
            indicator.parse_refresh_seconds("invalid", broken_warning_sink), 300
        )

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            config = root / "config.json"
            env = {
                "HOME": str(root),
                "PATH": os.environ.get("PATH", ""),
                "CODEXBAR_INDICATOR_REFRESH_SECONDS": "not-an-integer",
                "CODEXBAR_GNOME_CONFIG": str(config),
            }
            completed = subprocess.run(
                [sys.executable, str(MODULE_PATH)],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertNotIn("ValueError", completed.stderr)
            self.assertIn("display", completed.stderr.lower())
            self.assertFalse(config.exists())

    def test_bdd_l02_headless_launch_fails_before_config_or_provider_side_effect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            config = root / "config.json"
            marker = root / "provider-called"
            fake_cli = root / "codexbar"
            fake_cli.write_text(
                '#!/bin/sh\nprintf called > "$CODEXBAR_TEST_MARKER"\n',
                encoding="utf-8",
            )
            fake_cli.chmod(0o755)
            env = {
                "HOME": str(root),
                "PATH": os.environ.get("PATH", ""),
                "CODEXBAR_BIN": str(fake_cli),
                "CODEXBAR_TEST_MARKER": str(marker),
                "CODEXBAR_GNOME_CONFIG": str(config),
                "PYTHONPATH": str(MODULE_PATH.parent.parent),
            }
            completed = subprocess.run(
                [sys.executable, str(MODULE_PATH)],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("display", completed.stderr.lower())
            self.assertNotIn("Gtk-ERROR", completed.stderr)
            self.assertNotIn("core dumped", completed.stderr.lower())
            self.assertFalse(config.exists())
            self.assertFalse(marker.exists())

    def test_bdd_l03_two_processes_create_one_poller(self) -> None:
        self.assertTrue(
            hasattr(indicator, "CodexBarApplication"),
            "unique Gtk.Application boundary is not implemented",
        )
        if not indicator.Gtk.init_check([])[0] or not os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS"
        ):
            self.fail("L03 requires xvfb-run inside an isolated dbus-run-session")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            helper = root / "lifecycle-helper"
            socket_path = root / "calls.sock"
            config = root / "config.json"
            original_config = b'{"sentinel":"secondary-must-not-rewrite"}\n'
            config.write_bytes(original_config)
            _write_lifecycle_helper(helper)
            server = LocalInvocationServer(socket_path)
            env = _isolated_application_env(root, helper, socket_path)
            ready_dir = root / "start-ready"
            release = root / "start-release"
            env.update(
                {
                    "CODEXBAR_TEST_START_READY_DIR": str(ready_dir),
                    "CODEXBAR_TEST_START_RELEASE": str(release),
                }
            )
            first = _start_lifecycle_helper(helper, env)
            second = _start_lifecycle_helper(helper, env)
            connection = None
            try:
                _wait_for_path(ready_dir / str(first.pid))
                _wait_for_path(ready_dir / str(second.pid))
                release.touch()
                _wait_for_application_owner(env)
                primary_events = []
                for _ in range(3):
                    invocation, connection = server.accept()
                    primary_events.append(invocation)
                    self.assertEqual(connection.recv(1), b"")
                    connection.close()
                    connection = None
                self.assertEqual(
                    Counter(primary_events),
                    Counter(
                        {
                            ("coordinator-created",): 1,
                            ("view-created",): 1,
                            ("refresh-complete",): 1,
                        }
                    ),
                )
                deadline = time.monotonic() + 3
                while first.poll() is None and second.poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail("concurrent secondary application did not exit")
                    threading.Event().wait(0.01)
                exited = [
                    process for process in (first, second) if process.poll() is not None
                ]
                owners = [
                    process for process in (first, second) if process.poll() is None
                ]
                self.assertEqual(len(exited), 1)
                self.assertEqual(len(owners), 1)
                self.assertEqual(exited[0].wait(timeout=1), 0)
                server.assert_no_pending_call()
                self.assertEqual(config.read_bytes(), original_config)
                _quit_application(env)
                self.assertEqual(owners[0].wait(timeout=5), 0)
            finally:
                if connection is not None:
                    connection.close()
                _stop_exact_process(second)
                _stop_exact_process(first)
                server.close()

    def test_bdd_l04_owner_recovery_and_child_cleanup_for_all_exit_modes(self) -> None:
        self.assertTrue(
            hasattr(indicator, "CodexBarApplication"),
            "Gtk.Application recovery boundary is not implemented",
        )
        if not indicator.Gtk.init_check([])[0] or not os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS"
        ):
            self.fail("L04 requires xvfb-run inside an isolated dbus-run-session")
        exit_modes = ("quit", signal.SIGTERM, signal.SIGINT, signal.SIGKILL)
        for exit_mode in exit_modes:
            with (
                self.subTest(exit_mode=exit_mode),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp)
                fake_cli = root / "codexbar"
                socket_path = root / "calls.sock"
                _write_blocking_fake_codexbar(fake_cli)
                (root / "config.json").write_text(
                    json.dumps(_settings(codex="auto")), encoding="utf-8"
                )
                server = LocalInvocationServer(socket_path)
                env = _isolated_application_env(root, fake_cli, socket_path)
                first = _start_isolated_application(env)
                successor = None
                first_connection = None
                successor_connection = None
                try:
                    _wait_for_application_owner(env)
                    invocation, first_connection = server.accept()
                    self.assertEqual(invocation[0], "codex")
                    self.assertEqual(len(invocation), 3)
                    first_child_pids = tuple(int(value) for value in invocation[1:])
                    if exit_mode == "quit":
                        _quit_application(env)
                    else:
                        first.send_signal(exit_mode)
                    first.wait(timeout=5)
                    first_connection.settimeout(3)
                    self.assertEqual(first_connection.recv(1), b"")
                    for pid in first_child_pids:
                        _assert_pid_absent(pid)
                    self.assertFalse(_name_has_owner(env))

                    successor = _start_isolated_application(env)
                    _wait_for_application_owner(env)
                    invocation, successor_connection = server.accept()
                    self.assertEqual(invocation[0], "codex")
                    self.assertEqual(len(invocation), 3)
                    successor_child_pids = tuple(int(value) for value in invocation[1:])
                    successor_connection.sendall(b"1")
                    _quit_application(env)
                    self.assertEqual(successor.wait(timeout=5), 0)
                    for pid in successor_child_pids:
                        _assert_pid_absent(pid)
                finally:
                    if first_connection is not None:
                        first_connection.close()
                    if successor_connection is not None:
                        successor_connection.close()
                    if successor is not None:
                        _stop_exact_process(successor)
                    _stop_exact_process(first)
                    server.close()

    def test_bdd_l04_quit_reaps_stopped_supervisor_before_process_exit(self) -> None:
        if not indicator.Gtk.init_check([])[0] or not os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS"
        ):
            self.fail("L04 requires xvfb-run inside an isolated dbus-run-session")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            fake_cli = root / "codexbar"
            socket_path = root / "calls.sock"
            _write_blocking_fake_codexbar(fake_cli)
            (root / "config.json").write_text(
                json.dumps(_settings(codex="auto")), encoding="utf-8"
            )
            server = LocalInvocationServer(socket_path)
            env = _isolated_application_env(root, fake_cli, socket_path)
            application = _start_isolated_application(env)
            connection = None
            owned_pids: tuple[int, ...] = ()
            supervisor_pid: int | None = None
            try:
                _wait_for_application_owner(env)
                invocation, connection = server.accept()
                self.assertEqual(invocation[0], "codex")
                owned_pids = tuple(int(value) for value in invocation[1:])
                supervisor_pid = _parent_pid(owned_pids[0])
                self.assertEqual(_parent_pid(supervisor_pid), application.pid)
                os.kill(supervisor_pid, signal.SIGSTOP)

                _quit_application(env)
                self.assertEqual(application.wait(timeout=8), 0)
                connection.settimeout(3)
                self.assertEqual(connection.recv(1), b"")
                for pid in (*owned_pids, supervisor_pid):
                    _assert_pid_absent(pid)
                self.assertFalse(_name_has_owner(env))
            finally:
                if connection is not None:
                    connection.close()
                if supervisor_pid is not None:
                    try:
                        os.kill(supervisor_pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                for pid in (
                    *owned_pids,
                    *((supervisor_pid,) if supervisor_pid else ()),
                ):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                _stop_exact_process(application)
                server.close()

    def test_bdd_l05_shutdown_removes_exact_timer_and_closes_once(self) -> None:
        coordinator = FakeLifecycleCoordinator()
        view = FakeLifecycleView()
        timer = FakeTimerPort()
        hold = FakeApplicationHold()
        application = indicator.CodexBarApplication(
            coordinator=coordinator,
            view=view,
            refresh_seconds=30,
            timer_add=timer.add,
            timer_remove=timer.remove,
            application_hold=hold.hold,
            application_release=hold.release,
        )
        application.start()
        self.assertEqual(timer.added[0][0], 30)
        self.assertEqual(coordinator.auto_requests, 1)
        callback = timer.added[0][1]
        self.assertTrue(callback())
        self.assertEqual(coordinator.auto_requests, 2)
        application.shutdown()
        application.shutdown()
        self.assertEqual(timer.removed, [73])
        self.assertEqual(coordinator.closed, 1)
        self.assertEqual(view.closed, 1)
        self.assertEqual(hold.releases, 1)
        self.assertFalse(callback())
        self.assertEqual(coordinator.auto_requests, 2)

        class FailingCoordinator(FakeLifecycleCoordinator):
            def close(self) -> None:
                super().close()
                raise RuntimeError("injected coordinator close failure")

        failing_coordinator = FailingCoordinator()
        surviving_view = FakeLifecycleView()
        failing_timer_removals: list[int] = []
        surviving_hold = FakeApplicationHold()
        warnings: list[str] = []

        def failing_timer_remove(timer_id: int) -> bool:
            failing_timer_removals.append(timer_id)
            raise RuntimeError("injected timer removal failure")

        application = indicator.CodexBarApplication(
            coordinator=failing_coordinator,
            view=surviving_view,
            refresh_seconds=30,
            timer_add=lambda _seconds, _callback: 91,
            timer_remove=failing_timer_remove,
            application_hold=surviving_hold.hold,
            application_release=surviving_hold.release,
            warning_sink=warnings.append,
        )
        application.start()
        application.shutdown()
        self.assertEqual(failing_timer_removals, [91])
        self.assertEqual(failing_coordinator.closed, 1)
        self.assertEqual(surviving_view.closed, 1)
        self.assertEqual(surviving_hold.releases, 1)
        self.assertEqual(len(warnings), 2)
        self.assertTrue(
            all(len(message) <= indicator.MAX_DIAGNOSTIC_CHARS for message in warnings)
        )

        class FailingStartCoordinator(FakeLifecycleCoordinator):
            def request_auto(self) -> None:
                super().request_auto()
                raise RuntimeError("injected initial refresh failure")

        failing_start_coordinator = FailingStartCoordinator()
        start_view = FakeLifecycleView()
        start_timer = FakeTimerPort()
        start_hold = FakeApplicationHold()
        application = indicator.CodexBarApplication(
            coordinator=failing_start_coordinator,
            view=start_view,
            refresh_seconds=30,
            timer_add=start_timer.add,
            timer_remove=start_timer.remove,
            application_hold=start_hold.hold,
            application_release=start_hold.release,
        )
        with self.assertRaisesRegex(RuntimeError, "initial refresh"):
            application.start()
        self.assertEqual(start_timer.removed, [73])
        self.assertEqual(failing_start_coordinator.closed, 1)
        self.assertEqual(start_view.closed, 1)
        self.assertEqual(start_hold.releases, 1)
        application.shutdown()
        self.assertEqual(start_hold.releases, 1)

        created: list[str] = []
        closed_application = indicator.CodexBarApplication(
            coordinator_factory=lambda: created.append("coordinator") or object(),
            view_factory=lambda: created.append("view") or object(),
        )
        closed_application.shutdown()
        closed_application._activate(closed_application._application)
        self.assertEqual(created, [])
        self.assertIsNone(closed_application._coordinator)
        self.assertIsNone(closed_application._view)

        partially_created_coordinator = FakeLifecycleCoordinator()

        def fail_view_factory() -> object:
            raise RuntimeError("injected view construction failure")

        partial_application = indicator.CodexBarApplication(
            coordinator_factory=lambda: partially_created_coordinator,
            view_factory=fail_view_factory,
        )
        with self.assertRaisesRegex(RuntimeError, "view construction"):
            partial_application.start()
        self.assertEqual(partially_created_coordinator.closed, 1)
        self.assertTrue(partial_application._closed)
        partial_application.shutdown()
        self.assertEqual(partially_created_coordinator.closed, 1)

    def test_bdd_l05_gtk_activation_failure_returns_nonzero_process_status(
        self,
    ) -> None:
        if not indicator.Gtk.init_check([])[0] or not os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS"
        ):
            self.fail("L05 requires xvfb-run inside an isolated dbus-run-session")
        with tempfile.TemporaryDirectory() as raw_tmp:
            helper = Path(raw_tmp) / "startup-failure.py"
            helper.write_text(
                f"""#!/usr/bin/env python3
import importlib.util
import sys
from importlib.machinery import SourceFileLoader

module_path = {str(MODULE_PATH)!r}
spec = importlib.util.spec_from_loader(
    'codexbar_startup_failure',
    SourceFileLoader('codexbar_startup_failure', module_path),
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

def fail_factory():
    raise RuntimeError('injected activation failure')

application = module.CodexBarApplication(
    coordinator_factory=fail_factory,
    view_factory=lambda: object(),
)
raise SystemExit(application.run([sys.argv[0]]))
""",
                encoding="utf-8",
            )
            env = {
                key: os.environ[key]
                for key in (
                    "DBUS_SESSION_BUS_ADDRESS",
                    "DISPLAY",
                    "LANG",
                    "PATH",
                    "XAUTHORITY",
                    "XDG_RUNTIME_DIR",
                )
                if key in os.environ
            }
            completed = subprocess.run(
                [sys.executable, str(helper)],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("CodexBar startup failed: RuntimeError", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)

    def test_bdd_e05_missing_status_notifier_watcher_is_visible_and_quittable(
        self,
    ) -> None:
        if not indicator.Gtk.init_check([])[0] or not os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS"
        ):
            self.fail("E05 requires xvfb-run inside an isolated dbus-run-session")

        panel = FakePanelIndicator()
        view = indicator.IndicatorView(panel)
        gateway = ScriptedGateway()
        coordinator = indicator.RefreshCoordinator(
            gateway,
            _settings(),
            on_snapshot=view.render,
            global_error="Settings error: malformed configuration",
        )
        timer = FakeTimerPort()
        warnings: list[str] = []
        warning_seen = threading.Event()

        def record_warning(message: str) -> None:
            warnings.append(message)
            warning_seen.set()

        application = indicator.CodexBarApplication(
            coordinator=coordinator,
            view=view,
            refresh_seconds=30,
            timer_add=timer.add,
            timer_remove=timer.remove,
            application_hold=lambda: None,
            application_release=lambda: None,
            watcher_probe=lambda: False,
            warning_sink=record_warning,
        )
        try:
            application.start()
            _pump_gtk_until(warning_seen)
            self.assertEqual(len(warnings), 1)
            self.assertLessEqual(len(warnings[0]), 512)
            self.assertIn("StatusNotifierWatcher", warnings[0])
            labels = _visible_menu_labels(view)
            self.assertTrue(
                any("StatusNotifierWatcher" in label for label in labels), labels
            )
            self.assertTrue(
                any(
                    "Settings error: malformed configuration" in label
                    for label in labels
                ),
                labels,
            )
            self.assertEqual(gateway.calls, [])

            self.assertTrue(coordinator.update_settings(_settings()))
            recovered_labels = _visible_menu_labels(view)
            self.assertTrue(
                any("StatusNotifierWatcher" in label for label in recovered_labels),
                recovered_labels,
            )
            self.assertFalse(
                any("Settings error:" in label for label in recovered_labels),
                recovered_labels,
            )
        finally:
            application.shutdown()

        probe_entered = threading.Event()
        probe_release = threading.Event()
        start_returned = threading.Event()
        start_failures: list[BaseException] = []

        def blocking_probe() -> bool:
            probe_entered.set()
            if not probe_release.wait(3):
                raise AssertionError("test did not release watcher probe")
            return True

        nonblocking_application = indicator.CodexBarApplication(
            coordinator=FakeLifecycleCoordinator(),
            view=FakeLifecycleView(),
            refresh_seconds=30,
            timer_add=lambda _seconds, _callback: 92,
            timer_remove=lambda _timer_id: True,
            application_hold=lambda: None,
            application_release=lambda: None,
            watcher_probe=blocking_probe,
        )

        def start_application() -> None:
            try:
                nonblocking_application.start()
            except BaseException as exc:  # noqa: BLE001 - retained for test thread
                start_failures.append(exc)
            finally:
                start_returned.set()

        starter = threading.Thread(target=start_application, daemon=True)
        starter.start()
        self.assertTrue(probe_entered.wait(1))
        returned_before_probe = start_returned.wait(1)
        probe_release.set()
        starter.join(timeout=3)
        nonblocking_application.shutdown()
        self.assertTrue(
            returned_before_probe, "watcher probe blocked application start"
        )
        self.assertEqual(start_failures, [])

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            config = root / "config.json"
            config.write_text(json.dumps(_settings()), encoding="utf-8")
            marker = root / "provider-called"
            fake_cli = root / "codexbar"
            fake_cli.write_text(
                '#!/bin/sh\nprintf called > "$CODEXBAR_TEST_MARKER"\n',
                encoding="utf-8",
            )
            fake_cli.chmod(0o755)
            env = _isolated_application_env(root, fake_cli, root / "unused.sock")
            env["CODEXBAR_TEST_MARKER"] = str(marker)
            self.assertFalse(_name_has_owner(env))
            watcher_check = subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.freedesktop.DBus",
                    "--object-path",
                    "/org/freedesktop/DBus",
                    "--method",
                    "org.freedesktop.DBus.NameHasOwner",
                    "org.kde.StatusNotifierWatcher",
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=3,
                check=True,
            )
            self.assertIn("false", watcher_check.stdout.lower())
            process = _start_isolated_application(env)
            try:
                _wait_for_application_owner(env)
                self.assertIsNone(process.poll())
                readable, _writable, _exceptional = select.select(
                    [process.stderr], [], [], 3
                )
                self.assertEqual(readable, [process.stderr])
                watcher_warning = process.stderr.readline()
                _quit_application(env)
                stdout, stderr = process.communicate(timeout=5)
                stderr = watcher_warning + stderr
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertIn("StatusNotifierWatcher", stderr)
                self.assertFalse(marker.exists())
                self.assertFalse(_name_has_owner(env))
            finally:
                _stop_exact_process(process)


if __name__ == "__main__":
    unittest.main()
