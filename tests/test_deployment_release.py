from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from gi.repository import Gio

from tests.support import load_indicator

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
APP_ID = "io.github.antonshalin76.CodexBarGnome"
DESKTOP_NAME = "codexbar-gnome-indicator.desktop"
ARCHIVE_NAME = f"codexbar-gnome-{VERSION}.tar.gz"
CHECKSUM_NAME = f"{ARCHIVE_NAME}.sha256"
MANIFEST_KEYS = {"indicator", "desktop", "autostart"}
TRANSACTION_FILE = "install-transaction.json"
DEFAULT_SETTINGS = {
    "runtimes": {
        "codex": {"poll": True, "autoRefresh": True},
        "grok": {"poll": True, "autoRefresh": True},
        "claude": {"poll": False, "autoRefresh": False},
    },
}

# Publicly named fault-injection checkpoints keep the installer transaction
# observable without coupling tests to a particular shell utility or call count.
INSTALL_FAILURE_PHASES = (
    "stage-indicator",
    "stage-desktop",
    "stage-autostart",
    "validate-staged",
    "prepare-journal",
    "commit-indicator",
    "commit-desktop",
    "commit-autostart",
    "commit-manifest",
)
INSTALL_INTERRUPT_PHASES = (
    "prepared",
    "indicator-committed",
    "desktop-committed",
    "autostart-committed",
    "committed",
)
UNINSTALL_FAILURE_PHASES = (
    "prepare-journal",
    "remove-indicator",
    "remove-desktop",
    "remove-autostart",
    "remove-manifest",
)
UNINSTALL_INTERRUPT_PHASES = (
    "uninstall-prepared",
    "indicator-removed",
    "desktop-removed",
    "autostart-removed",
    "manifest-removed",
    "uninstall-committed",
)
PUBLISH_FAILURE_PHASES = (
    "tag-created",
    "draft-created",
    "archive-uploaded",
    "checksum-uploaded",
    "verified",
    "published",
)

DETERMINISTIC_BDD_IDS = {
    "BDD-S01",
    "BDD-S02",
    "BDD-S03",
    "BDD-S04",
    "BDD-S05",
    "BDD-S05B",
    "BDD-S06",
    "BDD-S07",
    "BDD-S08",
    "BDD-S09",
    "BDD-P01",
    "BDD-P02",
    "BDD-P03",
    "BDD-P04",
    "BDD-P05",
    "BDD-P06",
    "BDD-P07",
    "BDD-P08",
    "BDD-P09",
    "BDD-P10",
    "BDD-P11",
    "BDD-P12",
    "BDD-Z01",
    "BDD-Z02",
    "BDD-Z03",
    "BDD-Z04",
    "BDD-Z05",
    "BDD-Z06",
    "BDD-Z07",
    "BDD-Z08",
    "BDD-Z09",
    "BDD-Z09B",
    "BDD-Z10",
    "BDD-R01",
    "BDD-R02",
    "BDD-R03",
    "BDD-R04",
    "BDD-R05",
    "BDD-R06",
    "BDD-R07",
    "BDD-R08",
    "BDD-R09",
    "BDD-R10",
    "BDD-R11",
    "BDD-R12",
    "BDD-V01",
    "BDD-V02",
    "BDD-V03",
    "BDD-V04",
    "BDD-V05",
    "BDD-V06",
    "BDD-L01",
    "BDD-L02",
    "BDD-L03",
    "BDD-L04",
    "BDD-L05",
    "BDD-I01",
    "BDD-I02",
    "BDD-I03",
    "BDD-I04",
    "BDD-I04B",
    "BDD-I05",
    "BDD-I06",
    "BDD-I07",
    "BDD-I08",
    "BDD-I08B",
    "BDD-I08C",
    "BDD-I09",
    "BDD-E01",
    "BDD-E02",
    "BDD-E04",
    "BDD-E05",
    "BDD-Q01",
    "BDD-Q01B",
    "BDD-Q02",
    "BDD-Q04A",
}
EXTERNAL_BDD_IDS = {"BDD-E03", "BDD-E06", "BDD-Q03", "BDD-Q04B", "BDD-Q05"}
PREPUBLICATION_BDD_IDS = {"BDD-E03", "BDD-E06", "BDD-Q03"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _tree_snapshot(paths: list[Path]) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in paths:
        key = str(path)
        if path.is_symlink():
            snapshot[key] = ("symlink", os.readlink(path))
        elif path.is_file():
            snapshot[key] = ("file", _mode(path), _sha256(path))
        elif path.is_dir():
            snapshot[key] = ("directory", _mode(path))
        elif path.exists():
            metadata = path.lstat()
            snapshot[key] = (
                "other",
                stat.S_IFMT(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
            )
        else:
            snapshot[key] = ("missing",)
    return snapshot


class IsolatedHome:
    """Writable /mnt inside a read-only, network- and PID-isolated host view."""

    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="codexbar-deploy-test-")
        self.root = Path(self._temporary.name)
        self.home = self.root / "home"
        self.tmp = self.root / "tmp"
        self.runtime = self.root / "runtime"
        self.wrappers = self.root / "wrappers"
        for directory in (self.home, self.tmp, self.runtime, self.wrappers):
            directory.mkdir(parents=True)
        self.runtime.chmod(0o700)

    def close(self) -> None:
        self._temporary.cleanup()

    def guest(self, path: Path) -> str:
        relative = path.resolve().relative_to(self.root.resolve())
        return str(Path("/mnt") / relative)

    @property
    def config(self) -> Path:
        return self.home / ".config" / "codexbar-gnome" / "config.json"

    @property
    def state_dir(self) -> Path:
        return self.home / ".local" / "state" / "codexbar-gnome"

    @property
    def manifest(self) -> Path:
        return self.state_dir / "install-manifest.json"

    @property
    def managed(self) -> list[Path]:
        return [
            self.home / ".local" / "bin" / "codexbar-gnome-indicator",
            self.home / ".local" / "share" / "applications" / DESKTOP_NAME,
            self.home / ".config" / "autostart" / DESKTOP_NAME,
            self.manifest,
        ]

    def env(self, *, wrapper_path: bool = False) -> dict[str, str]:
        path = "/mnt/wrappers:/usr/bin:/bin" if wrapper_path else "/usr/bin:/bin"
        return {
            "HOME": "/mnt/home",
            "PATH": path,
            "TMPDIR": "/mnt/tmp",
            "XDG_CONFIG_HOME": "/mnt/home/.config",
            "XDG_DATA_HOME": "/mnt/home/.local/share",
            "XDG_STATE_HOME": "/mnt/home/.local/state",
            "XDG_RUNTIME_DIR": "/mnt/runtime",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "GALLIUM_DRIVER": "llvmpipe",
            "__EGL_VENDOR_LIBRARY_FILENAMES": (
                "/usr/share/glvnd/egl_vendor.d/50_mesa.json"
            ),
        }

    def command(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 15,
        pid_namespace: bool = False,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        prefix = [
            "/usr/bin/bwrap",
            "--die-with-parent",
            "--unshare-net",
            "--unshare-user",
            "--uid",
            "0",
            "--gid",
            "0",
        ]
        if pid_namespace:
            prefix += ["--unshare-pid", "--as-pid-1"]
        prefix += [
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/home",
            "--dir",
            "/home/anton",
            "--dir",
            "/home/anton/Source",
            "--ro-bind",
            str(REPO_ROOT),
            str(REPO_ROOT),
            "--tmpfs",
            "/root",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--chmod",
            "1777",
            "/tmp",
            "--bind",
            str(self.root),
            "/mnt",
        ]
        if cwd is not None:
            prefix += ["--chdir", self.guest(cwd)]
        prefix += ["--", *argv]
        return subprocess.run(
            prefix,
            check=check,
            capture_output=True,
            env=env or self.env(),
            start_new_session=True,
            text=True,
            timeout=timeout,
        )

    def popen(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        pid_namespace: bool = False,
    ) -> subprocess.Popen[str]:
        prefix = [
            "/usr/bin/bwrap",
            "--die-with-parent",
            "--unshare-net",
            "--unshare-user",
            "--uid",
            "0",
            "--gid",
            "0",
        ]
        if pid_namespace:
            prefix += ["--unshare-pid", "--as-pid-1"]
        prefix += [
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/home",
            "--dir",
            "/home/anton",
            "--dir",
            "/home/anton/Source",
            "--ro-bind",
            str(REPO_ROOT),
            str(REPO_ROOT),
            "--tmpfs",
            "/root",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--chmod",
            "1777",
            "/tmp",
            "--bind",
            str(self.root),
            "/mnt",
        ]
        if cwd is not None:
            prefix += ["--chdir", self.guest(cwd)]
        prefix += ["--", *argv]
        return subprocess.Popen(
            prefix,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env or self.env(),
            start_new_session=True,
            text=True,
        )

    def copy_repository(self, name: str = "candidate") -> Path:
        destination = self.root / name

        def ignored(_directory: str, names: list[str]) -> set[str]:
            return {
                name
                for name in names
                if name
                in {".git", ".ruff_cache", "__pycache__", "dist", ".papercuts.jsonl"}
                or name.endswith(".pyc")
            }

        shutil.copytree(REPO_ROOT, destination, ignore=ignored)
        return destination


class DeploymentReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = IsolatedHome()

    def tearDown(self) -> None:
        self.sandbox.close()

    def test_deterministic_sandbox_hides_host_home(self) -> None:
        checked = self.sandbox.command(
            [
                "/bin/sh",
                "-c",
                (
                    "test ! -e /home/anton/.claude/settings.json && "
                    'test ! -e /home/anton/.config && test -r "$1"'
                ),
                "sandbox-check",
                str(REPO_ROOT / "README.md"),
            ]
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def _install(
        self, repository: Path = REPO_ROOT, *, env: dict[str, str] | None = None
    ):
        script = (
            Path(self.sandbox.guest(repository / "install.sh"))
            if repository != REPO_ROOT
            else repository / "install.sh"
        )
        return self.sandbox.command(
            ["/bin/sh", str(script)],
            cwd=repository if repository != REPO_ROOT else None,
            env=env,
        )

    def _assert_manifest(self) -> dict[str, object]:
        self.assertTrue(
            self.sandbox.manifest.is_file(), "missing install ownership manifest"
        )
        manifest = json.loads(self.sandbox.manifest.read_text(encoding="utf-8"))
        self.assertEqual(_mode(self.sandbox.state_dir), 0o700)
        self.assertEqual(_mode(self.sandbox.manifest), 0o600)
        self.assertEqual(manifest.get("schemaVersion"), 1)
        self.assertEqual(manifest.get("version"), VERSION)
        files = manifest.get("files")
        self.assertIsInstance(files, dict)
        self.assertEqual(set(files), MANIFEST_KEYS)
        managed_by_key = dict(
            zip(("indicator", "desktop", "autostart"), self.sandbox.managed[:3])
        )
        for key, entry in files.items():
            self.assertEqual(entry.get("type"), "file")
            self.assertRegex(entry.get("sha256", ""), r"^[0-9a-f]{64}$")
            self.assertIn(entry.get("mode"), ("0644", "0755", 0o644, 0o755))
            self.assertEqual(entry["sha256"], _sha256(managed_by_key[key]))
        encoded = json.dumps(manifest)
        self.assertNotIn(str(self.sandbox.home), encoded)
        self.assertNotIn(self.sandbox.guest(self.sandbox.home), encoded)
        return manifest

    def _assert_installed_generation(self, repository: Path) -> None:
        source_desktop = repository / "share" / DESKTOP_NAME
        self.assertTrue(source_desktop.is_file())
        binary, desktop, autostart, _manifest = self.sandbox.managed
        self.assertEqual(_sha256(binary), _sha256(repository / "bin" / binary.name))
        self.assertEqual(_sha256(desktop), _sha256(source_desktop))
        self.assertEqual(_sha256(autostart), _sha256(source_desktop))
        self._assert_manifest()

    def _assert_safe_uninstaller(self) -> None:
        source = (REPO_ROOT / "uninstall.sh").read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r"\b(?:kill|pkill|pgrep|killall|os\.kill|signal\.kill)\b", source)
        )
        self.assertIn(f"gapplication action {APP_ID} quit", source)

    def _uninstall(self):
        self._assert_safe_uninstaller()
        return self.sandbox.command(
            [
                "/usr/bin/dbus-run-session",
                "--",
                "/bin/sh",
                str(REPO_ROOT / "uninstall.sh"),
            ]
        )

    def _write_config(
        self, data: bytes = b'{"sentinel":"keep-byte-for-byte"}\n'
    ) -> bytes:
        self.sandbox.config.parent.mkdir(parents=True, exist_ok=True)
        self.sandbox.config.write_bytes(data)
        return data

    @staticmethod
    def _mutate_candidate_generation(repository: Path, marker: str) -> None:
        with (repository / "bin" / "codexbar-gnome-indicator").open("ab") as stream:
            stream.write(f"\n# {marker} binary\n".encode())
        desktop = repository / "share" / DESKTOP_NAME
        if not desktop.exists():
            desktop = repository / "share" / f"{DESKTOP_NAME}.in"
        with desktop.open("a", encoding="utf-8") as stream:
            stream.write(f"\n# {marker} desktop\n")

    def _fault_env(self, phase: str) -> dict[str, str]:
        env = self.sandbox.env()
        env["CODEXBAR_INSTALL_TEST_FAIL_PHASE"] = phase
        return env

    def _assert_no_transaction_residue(self) -> None:
        allowed = {path.absolute() for path in self.sandbox.managed if path.is_file()}
        roots = [
            self.sandbox.home / ".local" / "bin",
            self.sandbox.home / ".local" / "share" / "applications",
            self.sandbox.home / ".config" / "autostart",
            self.sandbox.state_dir,
            self.sandbox.tmp,
        ]
        residue: list[str] = []
        for root in roots:
            if not root.exists():
                continue
            for candidate in root.rglob("*"):
                if (
                    not candidate.is_symlink()
                    and candidate.is_file()
                    and candidate.absolute() in allowed
                ):
                    continue
                residue.append(str(candidate.relative_to(self.sandbox.root)))
        self.assertEqual(residue, [], f"transaction residue: {residue}")

    def _start_blocked_install(
        self,
        repository: Path,
        phase: str,
        *,
        suffix: str,
        script_name: str = "install.sh",
    ) -> tuple[subprocess.Popen[str], Path, Path]:
        ready = self.sandbox.root / f"ready-{suffix}"
        release = self.sandbox.root / f"release-{suffix}"
        env = self.sandbox.env()
        env.update(
            {
                "CODEXBAR_INSTALL_TEST_BLOCK_PHASE": phase,
                "CODEXBAR_INSTALL_TEST_READY": self.sandbox.guest(ready),
                "CODEXBAR_INSTALL_TEST_RELEASE": self.sandbox.guest(release),
            }
        )
        process = self.sandbox.popen(
            ["/bin/sh", self.sandbox.guest(repository / script_name)],
            cwd=repository,
            env=env,
        )
        try:
            deadline = time.monotonic() + 8
            while (
                not ready.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertTrue(
                ready.exists(), f"installer did not reach checkpoint {phase}"
            )
        except BaseException:
            self._stop_process_group(process)
            raise
        return process, ready, release

    @staticmethod
    def _stop_process_group(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def _interrupt_install_with_signal(
        self,
        repository: Path,
        phase: str,
        signal_name: str,
    ) -> subprocess.CompletedProcess[str]:
        ready = self.sandbox.root / f"signal-ready-{signal_name}-{phase}"
        release = self.sandbox.root / f"signal-release-{signal_name}-{phase}"
        env = self.sandbox.env()
        env.update(
            {
                "CODEXBAR_INSTALL_TEST_BLOCK_PHASE": phase,
                "CODEXBAR_INSTALL_TEST_READY": self.sandbox.guest(ready),
                "CODEXBAR_INSTALL_TEST_RELEASE": self.sandbox.guest(release),
            }
        )
        script = (
            '/bin/sh "$1" & child=$!; count=0; '
            'while test ! -f "$2" && kill -0 "$child" 2>/dev/null; do '
            '  count=$((count + 1)); test "$count" -lt 400 || exit 96; sleep 0.02; '
            "done; "
            'test -f "$2" || exit 95; '
            f'kill -s {signal_name} "$child" || exit 94; '
            'wait "$child"; code=$?; '
            'test "$code" -ne 0'
        )
        return self.sandbox.command(
            [
                "/bin/sh",
                "-c",
                script,
                "signal-controller",
                self.sandbox.guest(repository / "install.sh"),
                self.sandbox.guest(ready),
            ],
            cwd=repository,
            env=env,
            timeout=15,
        )

    def test_bdd_i01_static_desktop_supports_special_home_and_exact_launch(
        self,
    ) -> None:
        desktop = REPO_ROOT / "share" / DESKTOP_NAME
        self.assertTrue(desktop.is_file(), "installer must copy a static desktop entry")
        text = desktop.read_text(encoding="utf-8")
        self.assertNotIn("@HOME@", text)
        self.assertIn(
            r'Exec=/bin/sh -c "exec \\"\\$HOME/.local/bin/codexbar-gnome-indicator\\""',
            text,
        )
        validation = subprocess.run(
            ["desktop-file-validate", str(desktop)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(validation.returncode, 0, validation.stderr)

        special_home = self.sandbox.root / 'home space&@#()[]\\"-Русский'
        self.sandbox.home.rename(special_home)
        self.sandbox.home = special_home
        candidate = self.sandbox.copy_repository()
        ledger = self.sandbox.root / "launch-ledger"
        fake = candidate / "bin" / "codexbar-gnome-indicator"
        fake.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$0" > "$LAUNCH_LEDGER"\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)
        env = self.sandbox.env()
        env["HOME"] = self.sandbox.guest(special_home)
        env["XDG_CONFIG_HOME"] = f"{env['HOME']}/.config"
        env["XDG_DATA_HOME"] = f"{env['HOME']}/.local/share"
        env["XDG_STATE_HOME"] = f"{env['HOME']}/.local/state"
        env["LAUNCH_LEDGER"] = self.sandbox.guest(ledger)
        result = self._install(candidate, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        installed_desktop = (
            special_home / ".local" / "share" / "applications" / DESKTOP_NAME
        )
        installed_autostart = special_home / ".config" / "autostart" / DESKTOP_NAME
        for installed_entry in (installed_desktop, installed_autostart):
            with self.subTest(entry=installed_entry):
                validated = subprocess.run(
                    ["desktop-file-validate", str(installed_entry)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(validated.returncode, 0, validated.stderr)
                info = Gio.DesktopAppInfo.new_from_filename(str(installed_entry))
                self.assertIsNotNone(info)
                self.assertEqual(
                    info.get_string("Exec"),
                    r'/bin/sh -c "exec \"\$HOME/.local/bin/codexbar-gnome-indicator\""',
                )
        launch = self.sandbox.command(
            [
                "/usr/bin/dbus-run-session",
                "--",
                "/usr/bin/xvfb-run",
                "-a",
                "/usr/bin/gtk-launch",
                "codexbar-gnome-indicator",
            ],
            env=env,
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        deadline = time.monotonic() + 5
        while not ledger.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(
            ledger.is_file(), "desktop launch did not reach installed binary"
        )
        self.assertEqual(
            ledger.read_text(encoding="utf-8").strip(),
            f"{env['HOME']}/.local/bin/codexbar-gnome-indicator",
        )

    def test_bdd_i02_fresh_install_modes_metadata_and_manifest(self) -> None:
        result = self._install()
        self.assertEqual(result.returncode, 0, result.stderr)
        binary, desktop, autostart, _manifest = self.sandbox.managed
        self.assertEqual(_mode(binary), 0o755)
        self.assertEqual(_mode(desktop), 0o644)
        self.assertEqual(_mode(autostart), 0o644)
        for runtime in ("Codex", "Grok", "Claude"):
            self.assertIn(runtime, desktop.read_text(encoding="utf-8"))
        self._assert_manifest()

    def test_bdd_i02_installer_rejects_arguments_without_side_effects(self) -> None:
        before = _tree_snapshot(self.sandbox.managed)
        result = self.sandbox.command(
            ["/bin/sh", str(REPO_ROOT / "install.sh"), "--help"]
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)
        self.assertEqual(_tree_snapshot(self.sandbox.managed), before)
        self.assertFalse(self.sandbox.state_dir.exists())

    def test_bdd_i03_i05_reinstall_is_identical_and_preserves_config(self) -> None:
        config = self._write_config(b"{not-json-and-must-still-survive}\n")
        first = self._install()
        self.assertEqual(first.returncode, 0, first.stderr)
        self._assert_manifest()
        before = _tree_snapshot(self.sandbox.managed)
        second = self._install()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(_tree_snapshot(self.sandbox.managed), before)
        self.assertEqual(self.sandbox.config.read_bytes(), config)

    def test_bdd_i04_failed_fresh_install_leaves_no_managed_generation(self) -> None:
        for phase in INSTALL_FAILURE_PHASES:
            with self.subTest(phase=phase):
                case = IsolatedHome()
                old = self.sandbox
                self.sandbox = case
                try:
                    result = self._install(env=self._fault_env(phase))
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(
                        _tree_snapshot(self.sandbox.managed),
                        {str(path): ("missing",) for path in self.sandbox.managed},
                    )
                    self._assert_no_transaction_residue()
                finally:
                    self.sandbox = old
                    case.close()

    def test_bdd_i04_i05_failed_update_restores_prior_generation_and_config(
        self,
    ) -> None:
        for phase in INSTALL_FAILURE_PHASES:
            with self.subTest(phase=phase):
                case = IsolatedHome()
                old = self.sandbox
                self.sandbox = case
                try:
                    config = self._write_config()
                    initial = self._install()
                    self.assertEqual(initial.returncode, 0, initial.stderr)
                    before = _tree_snapshot(self.sandbox.managed)
                    candidate = self.sandbox.copy_repository(f"candidate-{phase}")
                    self._mutate_candidate_generation(candidate, "candidate generation")

                    result = self._install(candidate, env=self._fault_env(phase))

                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(_tree_snapshot(self.sandbox.managed), before)
                    self.assertEqual(self.sandbox.config.read_bytes(), config)
                    self._assert_no_transaction_residue()
                finally:
                    self.sandbox = old
                    case.close()

    def test_bdd_i04b_installer_declares_durable_interrupt_recovery(self) -> None:
        source = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("XDG_STATE_HOME", source)
        self.assertRegex(source, r"prepared")
        self.assertRegex(source, r"committed")
        self.assertRegex(source, r"trap\s+[^\n]*(?:INT[^\n]*TERM|TERM[^\n]*INT)")
        self.assertNotRegex(source, r"/tmp/.+codexbar.+journal")

    def test_bdd_i04b_state_home_falls_back_when_xdg_variable_is_absent(
        self,
    ) -> None:
        env = self.sandbox.env()
        env.pop("XDG_STATE_HOME")

        installed = self._install(env=env)

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self._assert_manifest()
        removed = self.sandbox.command(
            [
                "/usr/bin/dbus-run-session",
                "--",
                "/bin/sh",
                str(REPO_ROOT / "uninstall.sh"),
            ],
            env=env,
        )
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(all(not path.exists() for path in self.sandbox.managed))
        self._assert_no_transaction_residue()

    def test_bdd_i04b_sigkill_transaction_is_recovered_by_next_install(self) -> None:
        for initial_state, phase in itertools.product(
            ("fresh", "update"), INSTALL_INTERRUPT_PHASES
        ):
            with self.subTest(initial_state=initial_state, phase=phase):
                case = IsolatedHome()
                old = self.sandbox
                self.sandbox = case
                process = None
                recovery = None
                try:
                    config = self._write_config()
                    if initial_state == "update":
                        initial = self._install()
                        self.assertEqual(initial.returncode, 0, initial.stderr)
                    prior = _tree_snapshot(self.sandbox.managed)
                    candidate = self.sandbox.copy_repository(
                        f"interrupted-{initial_state}-{phase}"
                    )
                    self._mutate_candidate_generation(
                        candidate, "interrupted candidate generation"
                    )

                    process, _ready, _release = self._start_blocked_install(
                        candidate,
                        phase,
                        suffix=f"interrupt-{initial_state}-{phase}",
                    )
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                    self._stop_process_group(process)

                    journal = self.sandbox.state_dir / TRANSACTION_FILE
                    self.assertTrue(journal.is_file())
                    self.assertEqual(_mode(journal), 0o600)
                    journal_value = json.loads(journal.read_text(encoding="utf-8"))
                    expected_phase = "committed" if phase == "committed" else "prepared"
                    self.assertEqual(journal_value.get("phase"), expected_phase)

                    recovery, _ready, release = self._start_blocked_install(
                        candidate,
                        "recovery-complete",
                        suffix=f"recovery-{initial_state}-{phase}",
                    )
                    if phase == "committed":
                        self._assert_installed_generation(candidate)
                    else:
                        self.assertEqual(_tree_snapshot(self.sandbox.managed), prior)
                    self.assertFalse(journal.exists())
                    self.assertEqual(self.sandbox.config.read_bytes(), config)
                    release.touch()
                    completed = recovery.communicate(timeout=8)
                    self.assertEqual(recovery.returncode, 0, completed[1])
                    self._stop_process_group(recovery)

                    self._assert_installed_generation(candidate)
                    self.assertEqual(self.sandbox.config.read_bytes(), config)
                    self.assertEqual(
                        [path.name for path in self.sandbox.state_dir.iterdir()],
                        ["install-manifest.json"],
                    )
                    self._assert_no_transaction_residue()
                finally:
                    if recovery is not None:
                        self._stop_process_group(recovery)
                    if process is not None:
                        self._stop_process_group(process)
                    self.sandbox = old
                    case.close()

    def test_bdd_i04b_sigint_sigterm_roll_back_immediately(self) -> None:
        for initial_state, signal_name, phase in itertools.product(
            ("fresh", "update"), ("INT", "TERM"), INSTALL_INTERRUPT_PHASES
        ):
            with self.subTest(
                initial_state=initial_state, signal=signal_name, phase=phase
            ):
                case = IsolatedHome()
                old = self.sandbox
                self.sandbox = case
                try:
                    config = self._write_config()
                    if initial_state == "update":
                        installed = self._install()
                        self.assertEqual(installed.returncode, 0, installed.stderr)
                    prior = _tree_snapshot(self.sandbox.managed)
                    candidate = self.sandbox.copy_repository(
                        f"signal-{initial_state}-{signal_name.lower()}-{phase}"
                    )
                    self._mutate_candidate_generation(
                        candidate, "signal interrupted generation"
                    )

                    interrupted = self._interrupt_install_with_signal(
                        candidate, phase, signal_name
                    )

                    self.assertEqual(interrupted.returncode, 0, interrupted.stderr)
                    if phase == "committed":
                        self._assert_installed_generation(candidate)
                    else:
                        self.assertEqual(_tree_snapshot(self.sandbox.managed), prior)
                    self.assertEqual(self.sandbox.config.read_bytes(), config)
                    self._assert_no_transaction_residue()
                finally:
                    self.sandbox = old
                    case.close()

    def test_bdd_i04b_uninstaller_recovers_interrupted_generation_first(
        self,
    ) -> None:
        for initial_state, phase in itertools.product(
            ("fresh", "update"), INSTALL_INTERRUPT_PHASES
        ):
            with self.subTest(initial_state=initial_state, phase=phase):
                case = IsolatedHome()
                old = self.sandbox
                self.sandbox = case
                interrupted = None
                recovering = None
                try:
                    config = self._write_config()
                    if initial_state == "update":
                        installed = self._install()
                        self.assertEqual(installed.returncode, 0, installed.stderr)
                    prior = _tree_snapshot(self.sandbox.managed)
                    candidate = self.sandbox.copy_repository(
                        f"uninstall-recovery-{initial_state}"
                    )
                    self._mutate_candidate_generation(
                        candidate, "uninstaller recovery candidate"
                    )
                    interrupted, _ready, _release = self._start_blocked_install(
                        candidate,
                        phase,
                        suffix=f"uninstall-interrupt-{initial_state}-{phase}",
                    )
                    os.killpg(interrupted.pid, signal.SIGKILL)
                    interrupted.wait(timeout=5)
                    self._stop_process_group(interrupted)

                    recovering, _ready, release = self._start_blocked_install(
                        candidate,
                        "recovery-complete",
                        suffix=f"uninstall-recover-{initial_state}-{phase}",
                        script_name="uninstall.sh",
                    )
                    if phase == "committed":
                        self._assert_installed_generation(candidate)
                    else:
                        self.assertEqual(_tree_snapshot(self.sandbox.managed), prior)
                    self.assertFalse(
                        (self.sandbox.state_dir / TRANSACTION_FILE).exists()
                    )
                    release.touch()
                    stdout, stderr = recovering.communicate(timeout=8)
                    self.assertEqual(recovering.returncode, 0, stdout + stderr)
                    self._stop_process_group(recovering)
                    self.assertTrue(
                        all(not path.exists() for path in self.sandbox.managed)
                    )
                    self.assertEqual(self.sandbox.config.read_bytes(), config)
                    self._assert_no_transaction_residue()
                finally:
                    if recovering is not None:
                        self._stop_process_group(recovering)
                    if interrupted is not None:
                        self._stop_process_group(interrupted)
                    self.sandbox = old
                    case.close()

    def test_bdd_i06_i07_i08_uninstall_is_precise_idempotent_and_preserves_config(
        self,
    ) -> None:
        self._assert_safe_uninstaller()
        config = self._write_config()
        unrelated = self.sandbox.home / "unrelated.txt"
        unrelated.write_text("keep", encoding="utf-8")
        install = self._install()
        self.assertEqual(install.returncode, 0, install.stderr)
        self._assert_manifest()
        unrelated_managed_parents = [
            self.sandbox.home / ".local" / "bin" / "keep-tool",
            self.sandbox.home / ".local" / "share" / "applications" / "keep.desktop",
            self.sandbox.home / ".config" / "autostart" / "keep.desktop",
            self.sandbox.state_dir / "keep-directory" / "keep.txt",
        ]
        for index, path in enumerate(unrelated_managed_parents):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"keep-{index}", encoding="utf-8")
        unrelated_before = _tree_snapshot(unrelated_managed_parents)
        first = self._uninstall()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")
        self.assertTrue(all(not path.exists() for path in self.sandbox.managed))
        self.assertEqual(self.sandbox.config.read_bytes(), config)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
        self.assertEqual(_tree_snapshot(unrelated_managed_parents), unrelated_before)
        second = self._uninstall()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stderr, "")
        self.assertEqual(self.sandbox.config.read_bytes(), config)
        self.assertEqual(_tree_snapshot(unrelated_managed_parents), unrelated_before)

    def test_bdd_i06_uninstaller_rejects_arguments_without_side_effects(self) -> None:
        config = self._write_config()
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        before = _tree_snapshot(self.sandbox.managed)
        result = self.sandbox.command(
            ["/bin/sh", str(REPO_ROOT / "uninstall.sh"), "--help"]
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)
        self.assertEqual(_tree_snapshot(self.sandbox.managed), before)
        self.assertEqual(self.sandbox.config.read_bytes(), config)

    def test_bdd_i08_uninstall_transaction_recovers_every_interruption(self) -> None:
        for phase in UNINSTALL_FAILURE_PHASES:
            with self.subTest(kind="failure", phase=phase):
                case = IsolatedHome()
                old = self.sandbox
                self.sandbox = case
                try:
                    config = self._write_config()
                    installed = self._install()
                    self.assertEqual(installed.returncode, 0, installed.stderr)
                    before = _tree_snapshot(self.sandbox.managed)
                    failed = self.sandbox.command(
                        ["/bin/sh", str(REPO_ROOT / "uninstall.sh")],
                        env=self._fault_env(phase),
                    )
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertEqual(_tree_snapshot(self.sandbox.managed), before)
                    self.assertEqual(self.sandbox.config.read_bytes(), config)
                    self._assert_no_transaction_residue()
                finally:
                    self.sandbox = old
                    case.close()

        for phase in UNINSTALL_INTERRUPT_PHASES:
            with self.subTest(kind="sigkill", phase=phase):
                case = IsolatedHome()
                old = self.sandbox
                self.sandbox = case
                process = None
                try:
                    config = self._write_config()
                    candidate = self.sandbox.copy_repository(
                        f"uninstall-transaction-{phase}"
                    )
                    installed = self._install(candidate)
                    self.assertEqual(installed.returncode, 0, installed.stderr)
                    process, _ready, _release = self._start_blocked_install(
                        candidate,
                        phase,
                        suffix=f"uninstall-{phase}",
                        script_name="uninstall.sh",
                    )
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                    self._stop_process_group(process)

                    recovered = self._uninstall()

                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    self.assertTrue(
                        all(not path.exists() for path in self.sandbox.managed)
                    )
                    self.assertEqual(self.sandbox.config.read_bytes(), config)
                    self._assert_no_transaction_residue()
                finally:
                    if process is not None:
                        self._stop_process_group(process)
                    self.sandbox = old
                    case.close()

    def test_bdd_i07_uninstall_quits_only_the_session_bus_owner(self) -> None:
        self._assert_safe_uninstaller()
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        fake_dir = self.sandbox.root / "fake-bin-i07"
        fake_dir.mkdir()
        fake = fake_dir / "codexbar"
        fake.write_text(
            "#!/usr/bin/python3\n"
            "import json, sys\n"
            "if sys.argv[1:] == ['usage', '--help']:\n"
            "    print('--provider codex grok claude zai --json-only --no-color')\n"
            "elif sys.argv[1:] == ['usage', '--provider', 'codex', '--json-only', '--no-color']:\n"
            "    print(json.dumps([{'provider':'codex','usage':{'secondary':{'usedPercent':7}}}]))\n"
            "else:\n"
            "    raise SystemExit(64)\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        self.sandbox.config.parent.mkdir(parents=True, exist_ok=True)
        self.sandbox.config.write_text(
            json.dumps(
                {
                    "runtimes": {
                        "codex": {"poll": True, "autoRefresh": True},
                        "grok": {"poll": False, "autoRefresh": False},
                        "claude": {"poll": False, "autoRefresh": False},
                    }
                }
            ),
            encoding="utf-8",
        )
        report = self.sandbox.root / "i07-report.json"
        env = self.sandbox.env()
        env["PATH"] = f"{self.sandbox.guest(fake_dir)}:/usr/bin:/bin"
        env["CODEXBAR_INDICATOR_REFRESH_SECONDS"] = "86400"
        env["I07_REPORT"] = self.sandbox.guest(report)
        env["I07_UNINSTALL"] = str(REPO_ROOT / "uninstall.sh")
        harness = r"""
import json, os, subprocess, time

app = subprocess.Popen(["/mnt/home/.local/bin/codexbar-gnome-indicator"])
decoy = subprocess.Popen(["/bin/sleep", "60"])
try:
    deadline = time.monotonic() + 8
    owner = False
    while time.monotonic() < deadline and app.poll() is None:
        probe = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
             "--object-path", "/org/freedesktop/DBus", "--method",
             "org.freedesktop.DBus.NameHasOwner",
             "io.github.antonshalin76.CodexBarGnome"],
            capture_output=True, text=True,
        )
        owner = probe.returncode == 0 and "true" in probe.stdout.lower()
        if owner:
            break
        time.sleep(0.02)
    uninstall = subprocess.run(
        ["/bin/sh", os.environ["I07_UNINSTALL"]],
        capture_output=True, text=True, timeout=8,
    )
    try:
        app.wait(timeout=8)
    except subprocess.TimeoutExpired:
        pass
    owner_after = subprocess.run(
        ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
         "--object-path", "/org/freedesktop/DBus", "--method",
         "org.freedesktop.DBus.NameHasOwner",
         "io.github.antonshalin76.CodexBarGnome"],
        capture_output=True, text=True,
    )
    value = {
        "owner_seen": owner,
        "uninstall_code": uninstall.returncode,
        "uninstall_stderr": uninstall.stderr[:512],
        "app_exited": app.poll() is not None,
        "owner_released": owner_after.returncode == 0 and "false" in owner_after.stdout.lower(),
        "decoy_alive": decoy.poll() is None,
    }
    with open(os.environ["I07_REPORT"], "w", encoding="utf-8") as stream:
        json.dump(value, stream)
finally:
    for process in (app, decoy):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
"""
        result = self.sandbox.command(
            [
                "/usr/bin/dbus-run-session",
                "--",
                "/usr/bin/xvfb-run",
                "-a",
                "/usr/bin/python3",
                "-c",
                harness,
            ],
            env=env,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(value["owner_seen"])
        self.assertEqual(value["uninstall_code"], 0, value["uninstall_stderr"])
        self.assertTrue(value["app_exited"])
        self.assertTrue(value["owner_released"])
        self.assertTrue(value["decoy_alive"])

    def test_bdd_i08b_bad_manifest_fails_closed_without_removing_targets(self) -> None:
        self._assert_safe_uninstaller()
        mutations = {
            "missing": lambda path: path.unlink(),
            "symlink": lambda path: (
                path.unlink(),
                path.symlink_to("foreign-manifest"),
            ),
            "oversized": lambda path: path.write_bytes(b"x" * (64 * 1024 + 1)),
            "malformed": lambda path: path.write_text("{", encoding="utf-8"),
            "unknown-version": lambda path: path.write_text(
                json.dumps({"schemaVersion": 999, "version": VERSION, "files": {}}),
                encoding="utf-8",
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                case = IsolatedHome()
                old = self.sandbox
                self.sandbox = case
                try:
                    config = self._write_config()
                    installed = self._install()
                    self.assertEqual(installed.returncode, 0, installed.stderr)
                    mutate(self.sandbox.manifest)
                    before = _tree_snapshot(self.sandbox.managed)
                    result = self._uninstall()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(result.stderr.strip())
                    self.assertLessEqual(len(result.stderr), 512)
                    self.assertRegex(
                        result.stderr.lower(), r"manifest|ownership|cannot uninstall"
                    )
                    self.assertEqual(_tree_snapshot(self.sandbox.managed), before)
                    self.assertEqual(self.sandbox.config.read_bytes(), config)
                finally:
                    self.sandbox = old
                    case.close()

    def test_bdd_i08c_foreign_target_fails_closed_as_one_unit(self) -> None:
        self._assert_safe_uninstaller()
        mutations = {
            "hash": lambda path: path.write_bytes(path.read_bytes() + b"foreign"),
            "mode": lambda path: path.chmod(0o700),
            "symlink": lambda path: (path.unlink(), path.symlink_to("foreign-target")),
            "directory": lambda path: (path.unlink(), path.mkdir()),
            "fifo": lambda path: (path.unlink(), os.mkfifo(path)),
        }
        for target_index, (name, mutate) in itertools.product(
            range(3), mutations.items()
        ):
            with self.subTest(target=target_index, mutation=name):
                case = IsolatedHome()
                old = self.sandbox
                self.sandbox = case
                try:
                    config = self._write_config()
                    installed = self._install()
                    self.assertEqual(installed.returncode, 0, installed.stderr)
                    target = self.sandbox.managed[target_index]
                    mutate(target)
                    before = _tree_snapshot(self.sandbox.managed)
                    result = self._uninstall()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertLessEqual(len(result.stderr), 512)
                    self.assertIn("ownership", result.stderr.lower())
                    self.assertEqual(_tree_snapshot(self.sandbox.managed), before)
                    self.assertEqual(self.sandbox.config.read_bytes(), config)
                finally:
                    self.sandbox = old
                    case.close()

    def test_bdd_i09_installed_runtime_resolves_codexbar_from_path(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        fake_dir = self.sandbox.root / "fake-bin"
        fake_dir.mkdir()
        ledger = self.sandbox.root / "codexbar-ledger.jsonl"
        fake = fake_dir / "codexbar"
        fake.write_text(
            "#!/usr/bin/python3\n"
            "import json, os, sys\n"
            "with open(os.environ['FAKE_CODEXBAR_LEDGER'], 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:] == ['usage', '--help']:\n"
            "    print('--provider codex|grok|claude|zai --json-only --no-color')\n"
            "elif sys.argv[1:] == ['usage', '--provider', 'codex', '--json-only', '--no-color']:\n"
            '    print(\'[{"provider":"codex","usage":{"secondary":{"usedPercent":7}}}]\')\n'
            "else:\n"
            "    raise SystemExit(64)\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        self.sandbox.config.parent.mkdir(parents=True, exist_ok=True)
        self.sandbox.config.write_text(
            json.dumps(
                {
                    "runtimes": {
                        "codex": {"poll": True, "autoRefresh": True},
                        "grok": {"poll": False, "autoRefresh": False},
                        "claude": {"poll": False, "autoRefresh": False},
                    }
                }
            ),
            encoding="utf-8",
        )
        env = self.sandbox.env()
        env["PATH"] = f"{self.sandbox.guest(fake_dir)}:/usr/bin:/bin"
        env["FAKE_CODEXBAR_LEDGER"] = self.sandbox.guest(ledger)
        env["CODEXBAR_INDICATOR_REFRESH_SECONDS"] = "86400"
        process = self.sandbox.popen(
            [
                "/usr/bin/dbus-run-session",
                "--",
                "/usr/bin/xvfb-run",
                "-a",
                "/mnt/home/.local/bin/codexbar-gnome-indicator",
            ],
            env=env,
        )
        try:
            deadline = time.monotonic() + 5
            records: list[list[str]] = []
            while time.monotonic() < deadline:
                if ledger.exists():
                    records = [
                        json.loads(line)
                        for line in ledger.read_text(encoding="utf-8").splitlines()
                    ]
                    if [
                        "usage",
                        "--provider",
                        "codex",
                        "--json-only",
                        "--no-color",
                    ] in records:
                        break
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            self.assertIn(
                ["usage", "--provider", "codex", "--json-only", "--no-color"], records
            )
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def test_bdd_e01_coverage_manifest_is_complete_and_unambiguous(self) -> None:
        path = REPO_ROOT / "tests" / "bdd_manifest.json"
        self.assertTrue(path.is_file(), "missing deterministic BDD coverage manifest")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        scenarios = manifest.get("scenarios")
        self.assertIsInstance(scenarios, dict)
        self.assertEqual(set(scenarios), DETERMINISTIC_BDD_IDS | EXTERNAL_BDD_IDS)
        for scenario_id in DETERMINISTIC_BDD_IDS:
            entry = scenarios[scenario_id]
            self.assertEqual(entry.get("status"), "deterministic")
            self.assertTrue(entry.get("tests"), scenario_id)
            self.assertEqual(len(entry["tests"]), len(set(entry["tests"])), scenario_id)
            for test_id in entry["tests"]:
                loaded = unittest.defaultTestLoader.loadTestsFromName(test_id)
                resolved: list[str] = []
                pending = [loaded]
                while pending:
                    current = pending.pop()
                    if isinstance(current, unittest.TestSuite):
                        pending.extend(current)
                    else:
                        resolved.append(current.id())
                self.assertEqual(
                    resolved, [test_id], f"unresolved test for {scenario_id}"
                )
        self.assertEqual(
            {
                scenario_id
                for scenario_id, entry in scenarios.items()
                if entry.get("status") == "external-gate"
            },
            EXTERNAL_BDD_IDS,
        )
        for scenario_id in EXTERNAL_BDD_IDS:
            entry = scenarios[scenario_id]
            self.assertEqual(entry.get("tests"), [])
            self.assertRegex(entry.get("receipt", ""), r"^\.release/evidence/.+\.json$")

        validator = REPO_ROOT / "scripts" / "validate-bdd-manifest.py"
        self.assertTrue(validator.is_file())
        valid_results = sorted(
            {
                test_id
                for scenario_id in DETERMINISTIC_BDD_IDS
                for test_id in scenarios[scenario_id]["tests"]
            }
        )
        valid_report = {
            "tests": [{"id": test_id, "status": "passed"} for test_id in valid_results],
            "summary": {
                "passed": len(valid_results),
                "failed": 0,
                "errors": 0,
                "skipped": 0,
            },
        }

        def rejected(
            name: str,
            manifest_bytes: bytes,
            report_value: dict[str, object],
            diagnostic: str,
        ) -> None:
            manifest_path = self.sandbox.root / f"invalid-{name}-manifest.json"
            report_path = self.sandbox.root / f"invalid-{name}-report.json"
            manifest_path.write_bytes(manifest_bytes)
            report_path.write_text(json.dumps(report_value), encoding="utf-8")
            checked = self.sandbox.command(
                [
                    "/usr/bin/python3",
                    str(validator),
                    "--manifest",
                    self.sandbox.guest(manifest_path),
                    "--test-report",
                    self.sandbox.guest(report_path),
                ]
            )
            self.assertNotEqual(checked.returncode, 0, name)
            self.assertIn(diagnostic, checked.stderr.lower())

        missing = json.loads(json.dumps(manifest))
        missing["scenarios"].pop("BDD-S01")
        rejected("missing", json.dumps(missing).encode(), valid_report, "missing")
        unknown = json.loads(json.dumps(manifest))
        unknown["scenarios"]["BDD-UNKNOWN"] = {
            "status": "deterministic",
            "tests": [valid_results[0]],
        }
        rejected("unknown", json.dumps(unknown).encode(), valid_report, "unknown")
        duplicate_mapping = json.loads(json.dumps(manifest))
        duplicate_mapping["scenarios"]["BDD-S01"]["tests"] *= 2
        rejected(
            "duplicate-mapping",
            json.dumps(duplicate_mapping).encode(),
            valid_report,
            "duplicate",
        )
        duplicate_object_key = (
            '{"schemaVersion":1,"scenarios":'
            + json.dumps(scenarios)
            + ',"scenarios":'
            + json.dumps(scenarios)
            + "}"
        ).encode()
        rejected("duplicate-key", duplicate_object_key, valid_report, "duplicate")
        rejected("invalid-utf8", b"\xff", valid_report, "invalid json")
        skipped_report = json.loads(json.dumps(valid_report))
        skipped_report["tests"][0]["status"] = "skipped"
        skipped_report["summary"]["passed"] -= 1
        skipped_report["summary"]["skipped"] = 1
        rejected(
            "skipped",
            json.dumps(manifest).encode(),
            skipped_report,
            "skipped",
        )
        missing_result = json.loads(json.dumps(valid_report))
        missing_result["tests"].pop()
        missing_result["summary"]["passed"] -= 1
        rejected(
            "missing-result",
            json.dumps(manifest).encode(),
            missing_result,
            "missing",
        )
        duplicate_result = json.loads(json.dumps(valid_report))
        duplicate_result["tests"].append(dict(duplicate_result["tests"][0]))
        duplicate_result["summary"]["passed"] += 1
        rejected(
            "duplicate-result",
            json.dumps(manifest).encode(),
            duplicate_result,
            "duplicate",
        )
        unknown_result = json.loads(json.dumps(valid_report))
        unknown_result["tests"].append(
            {"id": "tests.unknown.test_not_part_of_contract", "status": "passed"}
        )
        unknown_result["summary"]["passed"] += 1
        rejected(
            "unknown-result",
            json.dumps(manifest).encode(),
            unknown_result,
            "unknown",
        )
        for status in ("failed", "error"):
            invalid_status = json.loads(json.dumps(valid_report))
            invalid_status["tests"][0]["status"] = status
            invalid_status["summary"]["passed"] -= 1
            summary_key = "failed" if status == "failed" else "errors"
            invalid_status["summary"][summary_key] += 1
            rejected(
                f"result-{status}",
                json.dumps(manifest).encode(),
                invalid_status,
                status,
            )
        inconsistent_summary = json.loads(json.dumps(valid_report))
        inconsistent_summary["summary"]["passed"] += 1
        rejected(
            "summary",
            json.dumps(manifest).encode(),
            inconsistent_summary,
            "summary",
        )

        # The official quality gate sets this marker before running the suite,
        # then validates the complete report after the child process exits.
        if os.environ.get("CODEXBAR_TEST_RUNNER_CHILD") == "1":
            return

        runner = REPO_ROOT / "scripts" / "run-tests.py"
        self.assertTrue(runner.is_file())
        report = self.sandbox.root / "nested-test-report.json"
        run = self.sandbox.command(
            [
                "/usr/bin/dbus-run-session",
                "--",
                "/usr/bin/xvfb-run",
                "-a",
                "/usr/bin/python3",
                str(runner),
                "--report",
                self.sandbox.guest(report),
            ],
            timeout=120,
        )
        self.assertEqual(run.returncode, 0, run.stderr[-4000:])
        generated = json.loads(report.read_text(encoding="utf-8"))
        results = generated.get("tests")
        self.assertIsInstance(results, list)
        self.assertTrue(results)
        self.assertTrue(all(item.get("status") == "passed" for item in results))
        self.assertEqual(generated.get("summary", {}).get("skipped"), 0)
        validated = self.sandbox.command(
            [
                "/usr/bin/python3",
                str(validator),
                "--manifest",
                str(path),
                "--test-report",
                self.sandbox.guest(report),
            ]
        )
        self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_bdd_e02_exact_artifact_x11_runner_has_explicit_contract(self) -> None:
        runner = REPO_ROOT / "scripts" / "e2e-x11.py"
        self.assertTrue(runner.is_file())
        candidate = self._clean_release_repository()
        output = self.sandbox.root / "e02-release"
        built = self._build_release(candidate, output)
        self.assertEqual(built.returncode, 0, built.stderr)
        archive = output / ARCHIVE_NAME
        report_path = self.sandbox.root / "e02-report.json"
        probe_dir = self.sandbox.root / "e02-probe"
        observer_path = self.sandbox.root / "e02-observer.json"
        observer = r"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import time

from gi.repository import Gio, GLib

runner, archive_name, report_name, probe_name, observer_name = sys.argv[1:]
probe = pathlib.Path(probe_name)
probe.mkdir(parents=True, exist_ok=True)
process = subprocess.Popen([
    sys.executable, runner, "--archive", archive_name, "--report", report_name,
    "--probe-dir", probe_name, "--external-observer",
], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def wait_for(predicate, message, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if process.poll() is not None:
            raise RuntimeError(f"runner exited before {message}: {process.returncode}")
        time.sleep(0.02)
    raise RuntimeError(f"timeout waiting for {message}")

def call(connection, destination, path, interface, method, parameters, reply_type=None):
    return connection.call_sync(
        destination, path, interface, method, parameters, reply_type,
        Gio.DBusCallFlags.NONE, 3000, None,
    )

def has_owner(connection, name):
    result = call(
        connection, "org.freedesktop.DBus", "/org/freedesktop/DBus",
        "org.freedesktop.DBus", "NameHasOwner", GLib.Variant("(s)", (name,)),
        GLib.VariantType("(b)"),
    )
    return result.unpack()[0]

def get_property(connection, destination, path, interface, name):
    result = call(
        connection, destination, path, "org.freedesktop.DBus.Properties", "Get",
        GLib.Variant("(ss)", (interface, name)), GLib.VariantType("(v)"),
    )
    return result.unpack()[0]

def split_item(value):
    if value.startswith("/"):
        raise RuntimeError("watcher returned path without a service name")
    slash = value.find("/")
    return (value[:slash], value[slash:]) if slash >= 0 else (value, "/StatusNotifierItem")

def menu_items(node, found):
    item_id, properties, children = node
    label = properties.get("label")
    if hasattr(label, "unpack"):
        label = label.unpack()
    if isinstance(label, str):
        found[label] = item_id
    for child in children:
        menu_items(child, found)

def provider_records():
    ledger = probe / "provider-ledger.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]

connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
try:
    wait_for(lambda: (probe / "first-refresh.ready").exists(), "first refresh")
    owner = has_owner(connection, "io.github.antonshalin76.CodexBarGnome")
    watcher = has_owner(connection, "org.kde.StatusNotifierWatcher")
    registered = get_property(
        connection, "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
        "org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems",
    )
    if len(registered) != 1:
        raise RuntimeError(f"unexpected registered items: {registered!r}")
    service, item_path = split_item(registered[0])
    label = get_property(
        connection, service, item_path, "org.kde.StatusNotifierItem", "XAyatanaLabel"
    )
    menu_path = get_property(
        connection, service, item_path, "org.kde.StatusNotifierItem", "Menu"
    )
    layout = call(
        connection, service, menu_path, "com.canonical.dbusmenu", "GetLayout",
        GLib.Variant("(iias)", (0, -1, [])), None,
    ).unpack()[1]
    menu = {}
    menu_items(layout, menu)
    for required in ("Show details", "Refresh", "Quit"):
        if required not in menu:
            raise RuntimeError(f"missing menu item {required}: {menu!r}")

    home = pathlib.Path(os.environ["HOME"])
    installed = home / ".local/bin/codexbar-gnome-indicator"
    with tarfile.open(archive_name, "r:gz") as archive:
        archive_payloads = {
            "indicator": archive.extractfile(
                "codexbar-gnome-0.1.0/bin/codexbar-gnome-indicator"
            ).read(),
            "desktop": archive.extractfile(
                "codexbar-gnome-0.1.0/share/codexbar-gnome-indicator.desktop"
            ).read(),
        }
    installed_payloads = {
        "indicator": installed.read_bytes(),
        "desktop": (
            home / ".local/share/applications/codexbar-gnome-indicator.desktop"
        ).read_bytes(),
        "autostart": (
            home / ".config/autostart/codexbar-gnome-indicator.desktop"
        ).read_bytes(),
    }
    installed_hash_matches = all((
        hashlib.sha256(installed_payloads[key]).digest()
        == hashlib.sha256(archive_payloads["indicator" if key == "indicator" else "desktop"]).digest()
    ) for key in installed_payloads)
    process_count = 0
    for cmdline in pathlib.Path("/proc").glob("[0-9]*/cmdline"):
        try:
            value = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
        if str(installed).encode() in value and b"--child-supervisor" not in value:
            process_count += 1

    before = len(provider_records())
    for label_name in ("Show details", "Refresh"):
        call(
            connection, service, menu_path, "com.canonical.dbusmenu", "Event",
            GLib.Variant(
                "(isvu)", (menu[label_name], "clicked", GLib.Variant("s", ""), 0)
            ),
            None,
        )
    wait_for(lambda: len(provider_records()) > before, "post-details refresh")
    responsive_after_details = has_owner(
        connection, "io.github.antonshalin76.CodexBarGnome"
    )
    call(
        connection, service, menu_path, "com.canonical.dbusmenu", "Event",
        GLib.Variant(
            "(isvu)", (menu["Quit"], "clicked", GLib.Variant("s", ""), 0)
        ),
        None,
    )
    wait_for(
        lambda: not has_owner(connection, "io.github.antonshalin76.CodexBarGnome"),
        "owner release",
    )
    (probe / "observer-complete").touch()
    stdout, stderr = process.communicate(timeout=12)
    value = {
        "owner": owner,
        "watcher": watcher,
        "registered": registered,
        "label": label,
        "menu": sorted(menu),
        "installedHashMatches": installed_hash_matches,
        "primaryCount": process_count,
        "providerCallsBeforeAction": before,
        "providerCallsAfterAction": len(provider_records()),
        "responsiveAfterDetails": responsive_after_details,
        "quitCode": 0,
        "runnerCode": process.returncode,
        "runnerStderr": stderr[-1000:],
    }
    pathlib.Path(observer_name).write_text(json.dumps(value), encoding="utf-8")
finally:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
"""
        result = self.sandbox.command(
            [
                "/usr/bin/dbus-run-session",
                "--",
                "/usr/bin/xvfb-run",
                "-a",
                "/usr/bin/python3",
                "-c",
                observer,
                str(runner),
                self.sandbox.guest(archive),
                self.sandbox.guest(report_path),
                self.sandbox.guest(probe_dir),
                self.sandbox.guest(observer_path),
            ],
            timeout=55,
        )

        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-4000:])
        observed = json.loads(observer_path.read_text(encoding="utf-8"))
        self.assertTrue(observed["owner"])
        self.assertTrue(observed["watcher"])
        self.assertEqual(len(observed["registered"]), 1)
        self.assertEqual(observed["label"], "CxW 7%")
        self.assertIn("Show details", observed["menu"])
        self.assertIn("Refresh", observed["menu"])
        self.assertIn("Quit", observed["menu"])
        self.assertTrue(observed["installedHashMatches"])
        self.assertEqual(observed["primaryCount"], 1)
        self.assertGreater(
            observed["providerCallsAfterAction"], observed["providerCallsBeforeAction"]
        )
        self.assertTrue(observed["responsiveAfterDetails"])
        self.assertEqual(observed["quitCode"], 0)
        self.assertEqual(observed["runnerCode"], 0, observed["runnerStderr"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report.get("schemaVersion"), 1)
        self.assertEqual(report.get("status"), "passed")
        self.assertEqual(report.get("archiveSha256"), _sha256(archive))
        self.assertEqual(report.get("skipped"), [])
        events = report.get("events")
        self.assertIsInstance(events, list)
        self.assertEqual(
            [event.get("name") for event in events],
            [
                "archive-verified",
                "installed",
                "owner-acquired",
                "status-notifier-registered",
                "first-refresh",
                "menu-layout-read",
                "details-opened",
                "post-details-action",
                "owner-released",
                "uninstalled",
            ],
        )

        standalone_report = self.sandbox.root / "e02-standalone-report.json"
        standalone_probe = self.sandbox.root / "e02-standalone-probe"
        standalone = self.sandbox.command(
            [
                "/usr/bin/dbus-run-session",
                "--",
                "/usr/bin/xvfb-run",
                "-a",
                "/usr/bin/python3",
                str(runner),
                "--archive",
                self.sandbox.guest(archive),
                "--report",
                self.sandbox.guest(standalone_report),
                "--probe-dir",
                self.sandbox.guest(standalone_probe),
            ],
            timeout=45,
        )
        self.assertEqual(standalone.returncode, 0, standalone.stderr[-4000:])
        self.assertEqual(
            json.loads(standalone_report.read_text(encoding="utf-8")).get("status"),
            "passed",
        )
        self.assertTrue(all(event.get("passed") is True for event in events))
        self.assertEqual(report.get("process", {}).get("primaryCount"), 1)
        self.assertEqual(report.get("panelLabel"), "CxW 7%")
        self.assertTrue(report.get("installedHashesMatchedArchive"))
        self.assertTrue(report.get("detailsNonBlocking"))
        self.assertTrue(report.get("uninstallClean"))
        self.assertTrue(all(not path.exists() for path in self.sandbox.managed))

    def test_bdd_q01_gate_checks_staged_diff_and_required_tools(self) -> None:
        gate = REPO_ROOT / "scripts" / "quality-gate.sh"
        expected_commands = {
            "git-diff",
            "git-diff-staged",
            "python-compile",
            "ruff-check",
            "ruff-format",
            "shellcheck",
            "unit-e2e-tests",
            "bdd-manifest",
            "documentation-contract",
            "release-dry-run",
        }
        if os.environ.get("CODEXBAR_TEST_RUNNER_CHILD") == "1":
            active_report_name = os.environ.get("CODEXBAR_GATE_REPORT")
            if active_report_name is None:
                return
            active_report = Path(active_report_name)
            value = json.loads(active_report.read_text(encoding="utf-8"))
            completed = {item["name"] for item in value.get("commands", [])}
            self.assertTrue(
                {
                    "git-diff",
                    "git-diff-staged",
                    "python-compile",
                    "ruff-check",
                    "ruff-format",
                    "shellcheck",
                }.issubset(completed)
            )
            self.assertTrue(
                all(item.get("status") == "passed" for item in value["commands"])
            )
            return

        report_path = self.sandbox.root / "quality-gate-report.json"
        gate_env = dict(os.environ)
        gate_env.update(
            {
                "CODEXBAR_TEST_RUNNER_CHILD": "1",
                "CODEXBAR_GATE_REPORT": str(report_path),
            }
        )
        result = subprocess.run(
            [str(gate), "--report", str(report_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            env=gate_env,
            text=True,
            timeout=180,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        commands = report.get("commands")
        self.assertIsInstance(commands, list)
        self.assertEqual({item.get("name") for item in commands}, expected_commands)
        self.assertTrue(all(item.get("status") == "passed" for item in commands))
        self.assertEqual(report.get("status"), "passed")
        self.assertEqual(report.get("mode"), "full")
        self.assertRegex(report.get("commitSha", ""), r"^[0-9a-f]{40,64}$")
        self.assertGreater(report.get("testSummary", {}).get("passed", 0), 0)
        self.assertEqual(report.get("testSummary", {}).get("skipped"), 0)
        self.assertEqual(
            report.get("bddSummary"),
            {
                "status": "passed",
                "missing": 0,
                "duplicate": 0,
                "unknown": 0,
                "skipped": 0,
            },
        )
        workflow = (REPO_ROOT / ".github/workflows/quality.yml").read_text(
            encoding="utf-8"
        )
        self.assertRegex(workflow, r"actions/upload-artifact@[0-9a-f]{40}")
        self.assertIn("quality-gate-${{ matrix.os }}", workflow)
        self.assertIn(".release/evidence/quality-gate.json", workflow)

        default_preflight = self.sandbox.copy_repository("preflight-report-path")
        initialized = self.sandbox.command(
            ["/usr/bin/git", "init", "-q"], cwd=default_preflight
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        preflight = self.sandbox.command(
            [
                "/bin/sh",
                self.sandbox.guest(default_preflight / "scripts" / "quality-gate.sh"),
                "--preflight-only",
            ],
            cwd=default_preflight,
            env=self.sandbox.env(wrapper_path=True),
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertFalse(
            (default_preflight / ".release/evidence/quality-gate.json").exists()
        )
        preflight_value = json.loads(
            (
                default_preflight / ".release/evidence/quality-gate-preflight.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(preflight_value.get("mode"), "preflight")

        candidate = self.sandbox.copy_repository("missing-tool-gate")
        initialized = self.sandbox.command(
            ["/usr/bin/git", "init", "-q"], cwd=candidate
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        missing_report = self.sandbox.root / "missing-tool-report.json"
        missing = self.sandbox.command(
            [
                "/bin/sh",
                self.sandbox.guest(candidate / "scripts" / "quality-gate.sh"),
                "--preflight-only",
                "--report",
                self.sandbox.guest(missing_report),
            ],
            cwd=candidate,
            env=self.sandbox.env(),
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertLessEqual(len(missing.stderr), 1024)
        missing_value = json.loads(missing_report.read_text(encoding="utf-8"))
        self.assertEqual(missing_value.get("status"), "failed")
        self.assertTrue(missing_value.get("missingTools"))
        self.assertRegex(missing.stderr.lower(), r"install.+(?:ruff|shellcheck)")

        for kind in ("unstaged", "staged"):
            with self.subTest(whitespace=kind):
                dirty = self._clean_release_repository(f"whitespace-{kind}")
                with (dirty / "README.md").open("a", encoding="utf-8") as stream:
                    stream.write("trailing whitespace   \n")
                if kind == "staged":
                    staged = self.sandbox.command(
                        ["/usr/bin/git", "add", "README.md"], cwd=dirty
                    )
                    self.assertEqual(staged.returncode, 0, staged.stderr)
                whitespace_report = self.sandbox.root / f"{kind}-report.json"
                blocked = self.sandbox.command(
                    [
                        "/bin/sh",
                        self.sandbox.guest(dirty / "scripts" / "quality-gate.sh"),
                        "--report",
                        self.sandbox.guest(whitespace_report),
                    ],
                    cwd=dirty,
                    env=self.sandbox.env(),
                )
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn(
                    "trailing whitespace", (blocked.stdout + blocked.stderr).lower()
                )
                whitespace_value = json.loads(
                    whitespace_report.read_text(encoding="utf-8")
                )
                failed = [
                    item
                    for item in whitespace_value.get("commands", [])
                    if item.get("status") == "failed"
                ]
                expected = "git-diff-staged" if kind == "staged" else "git-diff"
                self.assertEqual([item.get("name") for item in failed], [expected])

    def test_bdd_q01b_documentation_matches_runtime_and_release_contract(self) -> None:
        runtime_contract = load_indicator("codexbar_documentation_contract")
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        lowered = readme.lower()
        self.assertNotIn("zenity", lowered)
        self.assertNotRegex(lowered, r"\bpkill\b")
        self.assertNotIn("/api/monitor/usage/quota/limit", lowered)
        self.assertIn("--provider zai", readme)
        self.assertRegex(
            lowered,
            rf"(?:minimum|min)[^\n]*{runtime_contract.MIN_REFRESH_SECONDS}",
        )
        self.assertRegex(
            lowered,
            rf"(?:maximum|max)[^\n]*{runtime_contract.MAX_REFRESH_SECONDS}",
        )
        self.assertRegex(
            lowered,
            rf"default[^\n]*{runtime_contract.DEFAULT_REFRESH_SECONDS}",
        )
        self.assertIn(APP_ID, readme)
        self.assertIn(ARCHIVE_NAME, readme)
        self.assertIn(CHECKSUM_NAME, readme)
        self.assertRegex(lowered, r"uninstall[^\n]*(?:preserv|keep)[^\n]*config")
        self.assertNotRegex(lowered, r"optional\s+shellcheck")
        defaults_match = re.search(
            r"Current safe defaults.*?```json\s*(\{.*?\})\s*```",
            readme,
            re.DOTALL,
        )
        self.assertIsNotNone(defaults_match)
        self.assertEqual(
            json.loads(defaults_match.group(1)), runtime_contract.DEFAULT_SETTINGS
        )
        for managed_path in (
            "~/.local/bin/codexbar-gnome-indicator",
            f"~/.local/share/applications/{DESKTOP_NAME}",
            f"~/.config/autostart/{DESKTOP_NAME}",
            "~/.local/state/codexbar-gnome/install-manifest.json",
            "~/.config/codexbar-gnome/config.json",
        ):
            self.assertIn(managed_path, readme)
        self.assertIn("GLib.file_set_contents_full", readme)
        self.assertIn("CONSISTENT", readme)
        self.assertIn("DURABLE", readme)
        self.assertRegex(readme, r"(?i)mode\s+`?0600`?")
        self.assertRegex(
            readme,
            r"(?s)--provider claude.*--provider zai.*Z_AI_API_KEY",
        )
        self.assertRegex(lowered, r"x11.*wayland|wayland.*x11")
        self.assertRegex(
            lowered,
            r"quality gate[^\n]*(?:mandatory|required|must pass|blocking)",
        )
        self.assertRegex(lowered, r"shellcheck[^\n]*(?:required|mandatory)")

    def _clean_release_repository(self, name: str = "release-source") -> Path:
        candidate = self.sandbox.copy_repository(name)
        commands = [
            ["/usr/bin/git", "init", "-q"],
            ["/usr/bin/git", "add", "--all"],
            [
                "/usr/bin/git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "release fixture",
            ],
        ]
        for command in commands:
            result = self.sandbox.command(command, cwd=candidate)
            self.assertEqual(result.returncode, 0, result.stderr)
        return candidate

    def _build_release(
        self, candidate: Path, output: Path
    ) -> subprocess.CompletedProcess[str]:
        output.mkdir(parents=True)
        return self.sandbox.command(
            [
                "/bin/sh",
                self.sandbox.guest(candidate / "scripts" / "build-release.sh"),
                "--output-dir",
                self.sandbox.guest(output),
            ],
            cwd=candidate,
        )

    def _publication_fixture(
        self,
        name: str,
        *,
        receipts: bool = True,
    ) -> tuple[Path, Path, Path, Path, dict[str, str]]:
        candidate = self._clean_release_repository(name)
        output = self.sandbox.root / f"{name}-release"
        built = self._build_release(candidate, output)
        self.assertEqual(built.returncode, 0, built.stderr)
        remote = self.sandbox.root / f"{name}-remote.git"
        created = self.sandbox.command(
            ["/usr/bin/git", "init", "--bare", "-q", self.sandbox.guest(remote)]
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        added = self.sandbox.command(
            ["/usr/bin/git", "remote", "add", "origin", self.sandbox.guest(remote)],
            cwd=candidate,
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        pushed = self.sandbox.command(
            ["/usr/bin/git", "push", "-q", "-u", "origin", "HEAD:master"],
            cwd=candidate,
        )
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        state = self.sandbox.root / f"{name}-gh-state.json"
        state.write_text('{"release":null,"calls":[]}\n', encoding="utf-8")
        self._write_fake_gh()
        env = self.sandbox.env(wrapper_path=True)
        env["FAKE_GH_STATE"] = self.sandbox.guest(state)
        if receipts:
            self._write_external_receipts(candidate, output)
        return candidate, output, remote, state, env

    def _write_external_receipts(
        self,
        candidate: Path,
        output: Path,
        scenario_ids: set[str] = PREPUBLICATION_BDD_IDS,
    ) -> None:
        manifest = json.loads(
            (candidate / "tests" / "bdd_manifest.json").read_text(encoding="utf-8")
        )
        commit_sha = self.sandbox.command(
            ["/usr/bin/git", "rev-parse", "HEAD"], cwd=candidate
        ).stdout.strip()
        archive_sha256 = _sha256(output / ARCHIVE_NAME)
        for scenario_id in scenario_ids:
            receipt = candidate / manifest["scenarios"][scenario_id]["receipt"]
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "bddId": scenario_id,
                        "status": "passed",
                        "commitSha": commit_sha,
                        "archiveSha256": archive_sha256,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

    def _write_fake_gh(self) -> None:
        wrapper = self.sandbox.wrappers / "gh"
        wrapper.write_text(
            r"""#!/usr/bin/python3
import hashlib
import json
import os
import pathlib
import sys

path = pathlib.Path(os.environ["FAKE_GH_STATE"])
state = json.loads(path.read_text(encoding="utf-8"))
args = sys.argv[1:]
state["calls"].append(args)

def save():
    path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")

def fail_after_mutation():
    if os.environ.get("FAKE_GH_FAIL_AFTER_OPERATION") == " ".join(args[:2]):
        print("injected post-mutation gh failure", file=sys.stderr)
        raise SystemExit(97)

if args[:2] == ["repo", "view"]:
    save()
    print(json.dumps({
        "nameWithOwner": os.environ.get(
            "FAKE_GH_REPOSITORY", "fixture/codexbar-gnome"
        )
    }))
    raise SystemExit(0)

if args[:2] != ["release", "view"] and os.environ.get("FAKE_GH_FAIL_OPERATION") == " ".join(args[:2]):
    save()
    print("injected gh failure", file=sys.stderr)
    raise SystemExit(97)

if args[:2] == ["release", "view"]:
    save()
    view_number = sum(
        call[:2] == ["release", "view"] for call in state["calls"]
    )
    if os.environ.get("FAKE_GH_FAIL_RELEASE_VIEW_NUMBER") == str(view_number):
        print("injected release view failure", file=sys.stderr)
        raise SystemExit(97)
    if state["release"] is None:
        raise SystemExit(1)
    print(json.dumps(state["release"], sort_keys=True))
elif args[:2] == ["release", "create"]:
    tag = args[2]
    target = args[args.index("--target") + 1]
    if state["release"] is not None:
        save()
        raise SystemExit(65)
    state["release"] = {
        "tagName": tag,
        "isDraft": "--draft" in args,
        "targetCommitish": target,
        "assets": [],
        "url": f"https://example.invalid/releases/tag/{tag}",
    }
    save()
    fail_after_mutation()
elif args[:2] == ["release", "upload"]:
    release = state["release"]
    if release is None:
        save()
        raise SystemExit(66)
    for raw in args[3:]:
        if raw.startswith("-"):
            continue
        asset_path = pathlib.Path(raw.split("#", 1)[0])
        payload = asset_path.read_bytes()
        release["assets"].append({
            "name": asset_path.name,
            "size": len(payload),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        })
    save()
    fail_after_mutation()
elif args[:2] == ["release", "delete"]:
    state["release"] = None
    save()
elif args[:2] == ["release", "edit"]:
    if state["release"] is None:
        save()
        raise SystemExit(67)
    if "--draft=false" not in args:
        save()
        print("release edit must explicitly publish the draft", file=sys.stderr)
        raise SystemExit(68)
    if os.environ.get("FAKE_GH_KEEP_DRAFT") != "1":
        state["release"]["isDraft"] = False
    if os.environ.get("FAKE_GH_MUTATE_AFTER_EDIT") == "asset":
        state["release"]["assets"][0]["digest"] = "sha256:" + "0" * 64
    elif os.environ.get("FAKE_GH_MUTATE_AFTER_EDIT") == "target":
        state["release"]["targetCommitish"] = "0" * 40
    save()
    fail_after_mutation()
else:
    save()
    print("unsupported fake gh argv: " + repr(args), file=sys.stderr)
    raise SystemExit(64)
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    def _write_fake_release_verifier_gh(self) -> None:
        wrapper = self.sandbox.wrappers / "gh"
        wrapper.write_text(
            r"""#!/usr/bin/python3
import hashlib
import json
import os
import pathlib
import shutil
import sys

args = sys.argv[1:]
repository = os.environ.get("FAKE_VERIFY_REPOSITORY", "fixture/codexbar-gnome")
archive = pathlib.Path(os.environ["FAKE_VERIFY_ARCHIVE"])
checksum = pathlib.Path(os.environ["FAKE_VERIFY_CHECKSUM"])
head = os.environ["FAKE_VERIFY_HEAD"]

if args[:2] == ["repo", "view"]:
    print(repository)
elif len(args) >= 5 and args[:3] == ["-R", "fixture/codexbar-gnome", "release"]:
    operation = args[3]
    if operation == "view":
        assets = []
        for path in (archive, checksum):
            payload = path.read_bytes()
            assets.append({
                "name": path.name,
                "size": len(payload),
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            })
        print(json.dumps({
            "tagName": "v0.1.0",
            "isDraft": False,
            "targetCommitish": head,
            "assets": assets,
        }, sort_keys=True))
    elif operation == "download":
        destination = pathlib.Path(args[args.index("--dir") + 1])
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, destination / archive.name)
        shutil.copyfile(checksum, destination / checksum.name)
    else:
        raise SystemExit(64)
else:
    raise SystemExit(64)
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    def _publish(
        self,
        candidate: Path,
        output: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return self.sandbox.command(
            [
                "/bin/sh",
                self.sandbox.guest(candidate / "scripts" / "publish-release.sh"),
                "--archive",
                self.sandbox.guest(output / ARCHIVE_NAME),
                "--checksum",
                self.sandbox.guest(output / CHECKSUM_NAME),
            ],
            cwd=candidate,
            env=env,
            timeout=20,
        )

    def test_bdd_q02_release_archive_is_exact_and_reproducible(self) -> None:
        candidate = self._clean_release_repository()
        first_dir = self.sandbox.root / "release-one"
        second_dir = self.sandbox.root / "release-two"
        first = self._build_release(candidate, first_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self._build_release(candidate, second_dir)
        self.assertEqual(second.returncode, 0, second.stderr)
        first_archive = first_dir / ARCHIVE_NAME
        second_archive = second_dir / ARCHIVE_NAME
        self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())
        self.assertEqual(first_archive.read_bytes()[4:8], b"\x00\x00\x00\x00")
        checksum = (first_dir / CHECKSUM_NAME).read_text(encoding="ascii")
        self.assertEqual(checksum, f"{_sha256(first_archive)}  {ARCHIVE_NAME}\n")
        checked = self.sandbox.command(
            ["/usr/bin/sha256sum", "-c", CHECKSUM_NAME],
            cwd=first_dir,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        root = f"codexbar-gnome-{VERSION}"
        expected = {
            root: ("directory", 0o755),
            f"{root}/bin": ("directory", 0o755),
            f"{root}/share": ("directory", 0o755),
            f"{root}/VERSION": ("file", 0o644),
            f"{root}/CHANGELOG.md": ("file", 0o644),
            f"{root}/LICENSE": ("file", 0o644),
            f"{root}/README.md": ("file", 0o644),
            f"{root}/install.sh": ("file", 0o755),
            f"{root}/uninstall.sh": ("file", 0o755),
            f"{root}/bin/codexbar-gnome-indicator": ("file", 0o755),
            f"{root}/share/{DESKTOP_NAME}": ("file", 0o644),
        }
        with tarfile.open(first_archive, "r:gz") as archive:
            archive_members = archive.getmembers()
            members = {
                member.name: (
                    "file"
                    if member.isfile()
                    else "directory"
                    if member.isdir()
                    else "other",
                    member.mode,
                )
                for member in archive_members
            }
            self.assertEqual(members, expected)
            self.assertEqual(
                [member.name for member in archive_members], sorted(members)
            )
            self.assertEqual(archive.pax_headers, {})
            for member in archive_members:
                self.assertEqual(member.uid, 0)
                self.assertEqual(member.gid, 0)
                self.assertEqual(member.uname, "")
                self.assertEqual(member.gname, "")
                self.assertEqual(member.mtime, 0)
                self.assertEqual(member.pax_headers, {})
            content = b"".join(
                archive.extractfile(member).read()
                for member in archive_members
                if member.isfile()
            )
            version_member = archive.extractfile(f"{root}/VERSION")
            self.assertIsNotNone(version_member)
            self.assertEqual(version_member.read(), b"0.1.0\n")
            self.assertNotIn(b"SECRET-MARKER", content)
            self.assertIsNone(
                re.search(rb"(?:sk-|ghp_|github_pat_)[A-Za-z0-9_]{20,}", content)
            )

        for dirty_kind in ("untracked", "tracked"):
            with self.subTest(dirty=dirty_kind):
                if dirty_kind == "untracked":
                    dirty_path = candidate / "untracked-release-input.txt"
                else:
                    dirty_path = candidate / "README.md"
                original = dirty_path.read_bytes() if dirty_path.exists() else None
                dirty_path.write_text("SECRET-MARKER dirty\n", encoding="utf-8")
                dirty_output = self.sandbox.root / f"dirty-{dirty_kind}-release"
                rejected = self._build_release(candidate, dirty_output)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertFalse((dirty_output / ARCHIVE_NAME).exists())
                if original is None:
                    dirty_path.unlink()
                else:
                    dirty_path.write_bytes(original)

        papercuts = candidate / ".papercuts.jsonl"
        papercuts.write_text("local feedback state\n", encoding="utf-8")
        ignored = self.sandbox.command(
            ["/usr/bin/git", "check-ignore", ".papercuts.jsonl"], cwd=candidate
        )
        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        ignored_output = self.sandbox.root / "ignored-papercuts-release"
        accepted = self._build_release(candidate, ignored_output)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        with tarfile.open(ignored_output / ARCHIVE_NAME, "r:gz") as archive:
            self.assertNotIn(".papercuts.jsonl", archive.getnames())

        race_output = self.sandbox.root / "build-race-release"
        race_output.mkdir()
        ready = self.sandbox.root / "build-race-ready"
        release = self.sandbox.root / "build-race-release-signal"
        race_env = self.sandbox.env()
        race_env.update(
            {
                "CODEXBAR_BUILD_TEST_READY": self.sandbox.guest(ready),
                "CODEXBAR_BUILD_TEST_RELEASE": self.sandbox.guest(release),
            }
        )
        race = self.sandbox.popen(
            [
                "/bin/sh",
                self.sandbox.guest(candidate / "scripts" / "build-release.sh"),
                "--output-dir",
                self.sandbox.guest(race_output),
            ],
            cwd=candidate,
            env=race_env,
        )
        try:
            deadline = time.monotonic() + 5
            while (
                not ready.exists()
                and race.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertTrue(
                ready.exists(), "builder did not reach clean-snapshot boundary"
            )
            readme = candidate / "README.md"
            original_readme = readme.read_bytes()
            readme.write_bytes(original_readme + b"\nconcurrent mutation\n")
            release.touch()
            stdout, stderr = race.communicate(timeout=8)
            self.assertNotEqual(race.returncode, 0, stdout + stderr)
            self.assertIn("does not match HEAD", stderr)
            self.assertFalse((race_output / ARCHIVE_NAME).exists())
            readme.write_bytes(original_readme)
        finally:
            self._stop_process_group(race)

    def test_bdd_q04a_publish_dry_run_has_no_remote_or_tag_side_effect(self) -> None:
        candidate, output, remote, state, env = self._publication_fixture(
            "publish-dry-run"
        )
        without_artifacts = self.sandbox.command(
            [
                "/bin/sh",
                self.sandbox.guest(candidate / "scripts" / "publish-release.sh"),
                "--dry-run",
            ],
            cwd=candidate,
            env=env,
        )
        self.assertEqual(without_artifacts.returncode, 0, without_artifacts.stderr)
        self.assertNotIn("Traceback", without_artifacts.stderr)
        trace = self.sandbox.root / "dry-run-git-trace.jsonl"
        env["GIT_TRACE2_EVENT"] = self.sandbox.guest(trace)
        before_local = self.sandbox.command(
            ["/usr/bin/git", "show-ref", "--tags"], cwd=candidate
        ).stdout
        before_remote = self.sandbox.command(
            [
                "/usr/bin/git",
                f"--git-dir={self.sandbox.guest(remote)}",
                "show-ref",
                "--tags",
            ]
        ).stdout
        result = self.sandbox.command(
            [
                "/bin/sh",
                self.sandbox.guest(candidate / "scripts" / "publish-release.sh"),
                "--dry-run",
                "--archive",
                self.sandbox.guest(output / ARCHIVE_NAME),
                "--checksum",
                self.sandbox.guest(output / CHECKSUM_NAME),
            ],
            cwd=candidate,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(state.read_text(encoding="utf-8"))["calls"],
            [],
            "dry-run must not invoke GitHub CLI",
        )
        self.assertEqual(
            self.sandbox.command(
                ["/usr/bin/git", "show-ref", "--tags"], cwd=candidate
            ).stdout,
            before_local,
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(remote)}",
                    "show-ref",
                    "--tags",
                ]
            ).stdout,
            before_remote,
        )
        traced_argv = [
            event["argv"]
            for event in (
                json.loads(line)
                for line in trace.read_text(encoding="utf-8").splitlines()
            )
            if event.get("event") == "start" and isinstance(event.get("argv"), list)
        ]
        for argv in traced_argv:
            self.assertNotIn("push", argv, argv)
            if "tag" in argv:
                tag_index = argv.index("tag")
                tag_args = argv[tag_index + 1 :]
                self.assertTrue(
                    "--list" in tag_args or "-l" in tag_args,
                    f"dry-run used mutating git tag argv: {argv}",
                )
        self.assertIn("v0.1.0", result.stdout)
        self.assertIn(ARCHIVE_NAME, result.stdout)

    def test_bdd_q04a_publication_requires_receipts_bound_to_commit_and_archive(
        self,
    ) -> None:
        candidate, output, remote, state, env = self._publication_fixture(
            "publish-evidence", receipts=False
        )

        def assert_unmodified_failure(result: subprocess.CompletedProcess[str]) -> None:
            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(result.stderr.lower(), r"evidence|receipt")
            self.assertEqual(
                self.sandbox.command(
                    ["/usr/bin/git", "tag", "--list", "v0.1.0"], cwd=candidate
                ).stdout.strip(),
                "",
            )
            self.assertEqual(
                self.sandbox.command(
                    [
                        "/usr/bin/git",
                        f"--git-dir={self.sandbox.guest(remote)}",
                        "tag",
                        "--list",
                        "v0.1.0",
                    ]
                ).stdout.strip(),
                "",
            )
            self.assertIsNone(json.loads(state.read_text(encoding="utf-8"))["release"])

        assert_unmodified_failure(self._publish(candidate, output, env))
        self._write_external_receipts(candidate, output)
        manifest = json.loads(
            (candidate / "tests" / "bdd_manifest.json").read_text(encoding="utf-8")
        )
        receipt_path = (
            candidate / manifest["scenarios"][min(PREPUBLICATION_BDD_IDS)]["receipt"]
        )
        correct = json.loads(receipt_path.read_text(encoding="utf-8"))
        for field in ("commitSha", "archiveSha256"):
            with self.subTest(field=field):
                invalid = dict(correct)
                invalid[field] = "0" * 64
                receipt_path.write_text(json.dumps(invalid) + "\n", encoding="utf-8")
                assert_unmodified_failure(self._publish(candidate, output, env))
        receipt_path.write_text(json.dumps(correct) + "\n", encoding="utf-8")
        symlink_target = receipt_path.with_name("valid-but-indirect.json")
        symlink_target.write_text(json.dumps(correct) + "\n", encoding="utf-8")
        receipt_path.unlink()
        receipt_path.symlink_to(symlink_target.name)
        assert_unmodified_failure(self._publish(candidate, output, env))
        receipt_path.unlink()
        receipt_path.write_text(json.dumps(correct) + "\n", encoding="utf-8")
        changed_origin = self.sandbox.command(
            [
                "/usr/bin/git",
                "remote",
                "set-url",
                "origin",
                "https://github.com/expected/codexbar-gnome.git",
            ],
            cwd=candidate,
        )
        self.assertEqual(changed_origin.returncode, 0, changed_origin.stderr)
        wrong_repository = self._publish(candidate, output, env)
        self.assertNotEqual(wrong_repository.returncode, 0)
        self.assertIn("repository", wrong_repository.stderr.lower())
        self.assertIsNone(json.loads(state.read_text(encoding="utf-8"))["release"])

    def test_bdd_q04a_rejects_archive_built_from_an_older_head(self) -> None:
        candidate, output, remote, state, env = self._publication_fixture(
            "publish-stale-archive"
        )
        readme = candidate / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nNew release state.\n")
        committed = self.sandbox.command(
            [
                "/usr/bin/git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qam",
                "advance release head",
            ],
            cwd=candidate,
        )
        self.assertEqual(committed.returncode, 0, committed.stderr)
        pushed = self.sandbox.command(
            ["/usr/bin/git", "push", "-q", "origin", "HEAD:master"], cwd=candidate
        )
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self._write_external_receipts(candidate, output)

        rejected = self._publish(candidate, output, env)

        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("payload does not match head", rejected.stderr.lower())
        self.assertEqual(
            self.sandbox.command(
                ["/usr/bin/git", "tag", "--list", "v0.1.0"], cwd=candidate
            ).stdout.strip(),
            "",
        )

        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(remote)}",
                    "tag",
                    "--list",
                    "v0.1.0",
                ]
            ).stdout.strip(),
            "",
        )
        self.assertIsNone(json.loads(state.read_text(encoding="utf-8"))["release"])

    def test_bdd_q04a_publication_rolls_back_each_new_pre_publish_resource(
        self,
    ) -> None:
        injected_phases = PUBLISH_FAILURE_PHASES[:-1]
        for phase in injected_phases:
            with self.subTest(phase=phase):
                case = IsolatedHome()
                old = self.sandbox
                self.sandbox = case
                try:
                    candidate, output, remote, state, env = self._publication_fixture(
                        f"publish-fail-{phase}"
                    )
                    env["CODEXBAR_PUBLISH_TEST_FAIL_PHASE"] = phase

                    result = self._publish(candidate, output, env)

                    self.assertNotEqual(result.returncode, 0)
                    local_tags = self.sandbox.command(
                        ["/usr/bin/git", "tag", "--list", "v0.1.0"], cwd=candidate
                    )
                    remote_tags = self.sandbox.command(
                        [
                            "/usr/bin/git",
                            f"--git-dir={self.sandbox.guest(remote)}",
                            "tag",
                            "--list",
                            "v0.1.0",
                        ]
                    )
                    self.assertEqual(local_tags.stdout.strip(), "")
                    self.assertEqual(remote_tags.stdout.strip(), "")
                    self.assertIsNone(
                        json.loads(state.read_text(encoding="utf-8"))["release"]
                    )
                finally:
                    self.sandbox = old
                    case.close()

        candidate, output, remote, state, env = self._publication_fixture(
            "publish-fail-after-published"
        )
        after_publish_env = dict(env)
        after_publish_env["CODEXBAR_PUBLISH_TEST_FAIL_PHASE"] = "published"
        after_publish = self._publish(candidate, output, after_publish_env)
        self.assertNotEqual(after_publish.returncode, 0)
        published_release = json.loads(state.read_text(encoding="utf-8"))["release"]
        self.assertIsNotNone(published_release)
        self.assertFalse(published_release["isDraft"])
        self.assertEqual(
            self.sandbox.command(
                ["/usr/bin/git", "tag", "--list", "v0.1.0"], cwd=candidate
            ).stdout.strip(),
            "v0.1.0",
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(remote)}",
                    "tag",
                    "--list",
                    "v0.1.0",
                ]
            ).stdout.strip(),
            "v0.1.0",
        )
        retried = self._publish(candidate, output, env)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(
            json.loads(state.read_text(encoding="utf-8"))["release"],
            published_release,
        )

        tagged_candidate, tagged_output, tagged_remote, tagged_state, tagged_env = (
            self._publication_fixture("publish-preexisting-correct-tag")
        )
        created_tag = self.sandbox.command(
            [
                "/usr/bin/git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "tag",
                "-a",
                "v0.1.0",
                "-m",
                "preexisting exact tag",
            ],
            cwd=tagged_candidate,
        )
        self.assertEqual(created_tag.returncode, 0, created_tag.stderr)
        pushed_tag = self.sandbox.command(
            ["/usr/bin/git", "push", "-q", "origin", "refs/tags/v0.1.0"],
            cwd=tagged_candidate,
        )
        self.assertEqual(pushed_tag.returncode, 0, pushed_tag.stderr)
        tag_object = self.sandbox.command(
            ["/usr/bin/git", "rev-parse", "v0.1.0^{tag}"], cwd=tagged_candidate
        ).stdout.strip()
        tagged_env["CODEXBAR_PUBLISH_TEST_FAIL_PHASE"] = "draft-created"
        failed_with_existing_tag = self._publish(
            tagged_candidate, tagged_output, tagged_env
        )
        self.assertNotEqual(failed_with_existing_tag.returncode, 0)
        self.assertIsNone(
            json.loads(tagged_state.read_text(encoding="utf-8"))["release"]
        )
        self.assertEqual(
            self.sandbox.command(
                ["/usr/bin/git", "rev-parse", "v0.1.0^{tag}"],
                cwd=tagged_candidate,
            ).stdout.strip(),
            tag_object,
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(tagged_remote)}",
                    "rev-parse",
                    "refs/tags/v0.1.0^{tag}",
                ]
            ).stdout.strip(),
            tag_object,
        )

        candidate, output, remote, state, env = self._publication_fixture(
            "publish-fail-command"
        )
        env["FAKE_GH_FAIL_OPERATION"] = "release edit"
        failed_publish = self._publish(candidate, output, env)
        self.assertNotEqual(failed_publish.returncode, 0)
        self.assertIsNone(json.loads(state.read_text(encoding="utf-8"))["release"])
        self.assertEqual(
            self.sandbox.command(
                ["/usr/bin/git", "tag", "--list", "v0.1.0"], cwd=candidate
            ).stdout.strip(),
            "",
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(remote)}",
                    "tag",
                    "--list",
                    "v0.1.0",
                ]
            ).stdout.strip(),
            "",
        )

    def test_bdd_q04a_post_publish_readback_failure_preserves_public_state(
        self,
    ) -> None:
        candidate, output, remote, state, env = self._publication_fixture(
            "publish-final-readback"
        )
        failing_env = dict(env)
        failing_env["FAKE_GH_FAIL_RELEASE_VIEW_NUMBER"] = "3"

        failed = self._publish(candidate, output, failing_env)

        self.assertNotEqual(failed.returncode, 0)
        after_failure = json.loads(state.read_text(encoding="utf-8"))
        self.assertIsNotNone(after_failure["release"])
        self.assertFalse(after_failure["release"]["isDraft"])
        self.assertEqual(
            self.sandbox.command(
                ["/usr/bin/git", "tag", "--list", "v0.1.0"], cwd=candidate
            ).stdout.strip(),
            "v0.1.0",
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(remote)}",
                    "tag",
                    "--list",
                    "v0.1.0",
                ]
            ).stdout.strip(),
            "v0.1.0",
        )
        calls_before_retry = len(after_failure["calls"])

        retried = self._publish(candidate, output, env)

        self.assertEqual(retried.returncode, 0, retried.stderr)
        after_retry = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(after_retry["release"], after_failure["release"])
        retry_calls = after_retry["calls"][calls_before_retry:]
        self.assertEqual(
            [call[:2] for call in retry_calls],
            [["repo", "view"], ["release", "view"]],
        )

        (
            mutated_candidate,
            mutated_output,
            mutated_remote,
            mutated_state,
            mutated_env,
        ) = self._publication_fixture("publish-mutated-readback")
        mutation_env = dict(mutated_env)
        mutation_env["FAKE_GH_MUTATE_AFTER_EDIT"] = "asset"

        mutated = self._publish(mutated_candidate, mutated_output, mutation_env)

        self.assertNotEqual(mutated.returncode, 0)
        mutated_release = json.loads(mutated_state.read_text(encoding="utf-8"))[
            "release"
        ]
        self.assertFalse(mutated_release["isDraft"])
        self.assertEqual(
            self.sandbox.command(
                ["/usr/bin/git", "tag", "--list", "v0.1.0"], cwd=mutated_candidate
            ).stdout.strip(),
            "v0.1.0",
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(mutated_remote)}",
                    "tag",
                    "--list",
                    "v0.1.0",
                ]
            ).stdout.strip(),
            "v0.1.0",
        )

        create_candidate, create_output, create_remote, create_state, create_env = (
            self._publication_fixture("publish-create-ambiguous")
        )
        create_env["FAKE_GH_FAIL_AFTER_OPERATION"] = "release create"
        create_failed = self._publish(create_candidate, create_output, create_env)
        self.assertNotEqual(create_failed.returncode, 0)
        self.assertIsNone(
            json.loads(create_state.read_text(encoding="utf-8"))["release"]
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(create_remote)}",
                    "tag",
                    "--list",
                    "v0.1.0",
                ]
            ).stdout.strip(),
            "",
        )

        resume_candidate, resume_output, _remote, resume_state, resume_env = (
            self._publication_fixture("publish-edit-retry")
        )
        ambiguous_env = dict(resume_env)
        ambiguous_env["FAKE_GH_FAIL_OPERATION"] = "release edit"
        ambiguous_env["FAKE_GH_FAIL_RELEASE_VIEW_NUMBER"] = "3"

        ambiguous = self._publish(resume_candidate, resume_output, ambiguous_env)

        self.assertNotEqual(ambiguous.returncode, 0)
        preserved = json.loads(resume_state.read_text(encoding="utf-8"))
        self.assertTrue(preserved["release"]["isDraft"])
        marker = (
            resume_candidate / ".release" / "evidence" / "publication-transaction.json"
        )
        self.assertTrue(marker.is_file())
        calls_before_retry = len(preserved["calls"])

        wrong_repository_env = dict(resume_env)
        wrong_repository_env["FAKE_GH_REPOSITORY"] = "other/codexbar-gnome"
        wrong_repository = self._publish(
            resume_candidate, resume_output, wrong_repository_env
        )
        self.assertNotEqual(wrong_repository.returncode, 0)
        self.assertIn("marker", wrong_repository.stderr.lower())
        after_wrong_repository = json.loads(resume_state.read_text(encoding="utf-8"))
        self.assertEqual(after_wrong_repository["release"], preserved["release"])
        self.assertTrue(marker.is_file())

        resumed = self._publish(resume_candidate, resume_output, resume_env)

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        after_resume = json.loads(resume_state.read_text(encoding="utf-8"))
        self.assertFalse(after_resume["release"]["isDraft"])
        self.assertFalse(marker.exists())
        retry_operations = [
            call[:2] for call in after_resume["calls"][calls_before_retry:]
        ]
        self.assertNotIn(["release", "create"], retry_operations)
        self.assertNotIn(["release", "upload"], retry_operations)
        self.assertIn(["release", "edit"], retry_operations)

        edit_candidate, edit_output, _edit_remote, edit_state, edit_env = (
            self._publication_fixture("publish-edit-ambiguous")
        )
        edit_env["FAKE_GH_FAIL_AFTER_OPERATION"] = "release edit"
        edit_result = self._publish(edit_candidate, edit_output, edit_env)
        self.assertEqual(edit_result.returncode, 0, edit_result.stderr)
        self.assertFalse(
            json.loads(edit_state.read_text(encoding="utf-8"))["release"]["isDraft"]
        )

        draft_candidate, draft_output, draft_remote, draft_state, draft_env = (
            self._publication_fixture("publish-edit-noop")
        )
        draft_env["FAKE_GH_KEEP_DRAFT"] = "1"
        draft_result = self._publish(draft_candidate, draft_output, draft_env)
        self.assertNotEqual(draft_result.returncode, 0)
        self.assertIsNone(
            json.loads(draft_state.read_text(encoding="utf-8"))["release"]
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(draft_remote)}",
                    "tag",
                    "--list",
                    "v0.1.0",
                ]
            ).stdout.strip(),
            "",
        )

    def test_release_download_verifier_owns_postpublication_q05(self) -> None:
        source = (REPO_ROOT / "scripts" / "verify-release.sh").read_text(
            encoding="utf-8"
        )
        for contract in (
            'gh -R "$repository" release view',
            'gh -R "$repository" release download',
            "e2e-x11.py",
            "q05-host-smoke.json",
            "q04b-github-release.json",
            'HOME="$e2e_home"',
            'XDG_RUNTIME_DIR="$e2e_runtime"',
            '"bddId": "BDD-Q05"',
            '"commitSha": sys.argv[3]',
            '"archiveSha256": sys.argv[4]',
        ):
            self.assertIn(contract, source)

        candidate = self._clean_release_repository("verify-published-release")
        output = self.sandbox.root / "verify-published-release-output"
        built = self._build_release(candidate, output)
        self.assertEqual(built.returncode, 0, built.stderr)
        added = self.sandbox.command(
            [
                "/usr/bin/git",
                "remote",
                "add",
                "origin",
                "https://github.com/fixture/codexbar-gnome.git",
            ],
            cwd=candidate,
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        self._write_external_receipts(candidate, output, {"BDD-Q04B"})
        self._write_fake_release_verifier_gh()
        head = self.sandbox.command(
            ["/usr/bin/git", "rev-parse", "HEAD"], cwd=candidate
        ).stdout.strip()
        env = self.sandbox.env(wrapper_path=True)
        env.update(
            {
                "FAKE_VERIFY_ARCHIVE": self.sandbox.guest(output / ARCHIVE_NAME),
                "FAKE_VERIFY_CHECKSUM": self.sandbox.guest(output / CHECKSUM_NAME),
                "FAKE_VERIFY_HEAD": head,
            }
        )
        foreign = self.sandbox.home / ".local" / "bin" / "codexbar-gnome-indicator"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("host sentinel\n", encoding="utf-8")

        verified = self.sandbox.command(
            [
                "/bin/sh",
                self.sandbox.guest(candidate / "scripts" / "verify-release.sh"),
            ],
            cwd=candidate,
            env=env,
            timeout=30,
        )

        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(foreign.read_text(encoding="utf-8"), "host sentinel\n")
        manifest = json.loads(
            (candidate / "tests" / "bdd_manifest.json").read_text(encoding="utf-8")
        )
        q05 = candidate / manifest["scenarios"]["BDD-Q05"]["receipt"]
        self.assertEqual(
            json.loads(q05.read_text(encoding="utf-8")),
            {
                "schemaVersion": 1,
                "bddId": "BDD-Q05",
                "status": "passed",
                "commitSha": head,
                "archiveSha256": _sha256(output / ARCHIVE_NAME),
            },
        )

        q05.unlink()
        wrong_repository_env = dict(env)
        wrong_repository_env["FAKE_VERIFY_REPOSITORY"] = "other/project"
        wrong_repository = self.sandbox.command(
            [
                "/bin/sh",
                self.sandbox.guest(candidate / "scripts" / "verify-release.sh"),
            ],
            cwd=candidate,
            env=wrong_repository_env,
            timeout=20,
        )
        self.assertNotEqual(wrong_repository.returncode, 0)
        self.assertIn("repository", wrong_repository.stderr.lower())
        self.assertFalse(q05.exists())

        q04b = candidate / manifest["scenarios"]["BDD-Q04B"]["receipt"]
        receipt = json.loads(q04b.read_text(encoding="utf-8"))
        receipt["archiveSha256"] = "0" * 64
        q04b.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        wrong_digest = self.sandbox.command(
            [
                "/bin/sh",
                self.sandbox.guest(candidate / "scripts" / "verify-release.sh"),
            ],
            cwd=candidate,
            env=env,
            timeout=20,
        )
        self.assertNotEqual(wrong_digest.returncode, 0)
        self.assertRegex(wrong_digest.stderr.lower(), r"digest|q04b")
        self.assertFalse(q05.exists())

        q04b.write_text(
            json.dumps({**receipt, "archiveSha256": _sha256(output / ARCHIVE_NAME)})
            + "\n",
            encoding="utf-8",
        )
        altered_output = self.sandbox.root / "verify-published-release-altered"
        altered_output.mkdir()
        altered_archive = altered_output / ARCHIVE_NAME
        altered_archive.write_bytes((output / ARCHIVE_NAME).read_bytes() + b"altered")
        altered_checksum = altered_output / CHECKSUM_NAME
        altered_checksum.write_text(
            f"{_sha256(altered_archive)}  {ARCHIVE_NAME}\n", encoding="ascii"
        )
        altered_env = dict(env)
        altered_env.update(
            {
                "FAKE_VERIFY_ARCHIVE": self.sandbox.guest(altered_archive),
                "FAKE_VERIFY_CHECKSUM": self.sandbox.guest(altered_checksum),
            }
        )
        altered_assets = self.sandbox.command(
            [
                "/bin/sh",
                self.sandbox.guest(candidate / "scripts" / "verify-release.sh"),
            ],
            cwd=candidate,
            env=altered_env,
            timeout=20,
        )
        self.assertNotEqual(altered_assets.returncode, 0)
        self.assertIn("digest", altered_assets.stderr.lower())
        self.assertFalse(q05.exists())

    def test_bdd_q04a_preserves_preexisting_state_and_retry_is_idempotent(
        self,
    ) -> None:
        candidate, output, remote, state, env = self._publication_fixture(
            "publish-existing"
        )
        head = self.sandbox.command(
            ["/usr/bin/git", "rev-parse", "HEAD"], cwd=candidate
        ).stdout.strip()
        tagged = self.sandbox.command(
            [
                "/usr/bin/git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "tag",
                "-a",
                "v0.1.0",
                "-m",
                "pre-existing",
            ],
            cwd=candidate,
        )
        self.assertEqual(tagged.returncode, 0, tagged.stderr)
        pushed = self.sandbox.command(
            ["/usr/bin/git", "push", "-q", "origin", "refs/tags/v0.1.0"],
            cwd=candidate,
        )
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        foreign = {
            "tagName": "v0.1.0",
            "isDraft": True,
            "targetCommitish": head,
            "assets": [{"name": "foreign.bin", "size": 1, "digest": "sha256:00"}],
            "url": "https://example.invalid/pre-existing",
        }
        state.write_text(
            json.dumps({"release": foreign, "calls": []}) + "\n", encoding="utf-8"
        )
        before_tag = self.sandbox.command(
            ["/usr/bin/git", "rev-parse", "v0.1.0^{tag}"], cwd=candidate
        ).stdout.strip()
        before_remote_tag = self.sandbox.command(
            [
                "/usr/bin/git",
                f"--git-dir={self.sandbox.guest(remote)}",
                "rev-parse",
                "refs/tags/v0.1.0^{tag}",
            ]
        ).stdout.strip()
        before_remote_object = self.sandbox.command(
            [
                "/usr/bin/git",
                f"--git-dir={self.sandbox.guest(remote)}",
                "cat-file",
                "-p",
                "refs/tags/v0.1.0",
            ]
        ).stdout

        rejected = self._publish(candidate, output, env)

        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(
            json.loads(state.read_text(encoding="utf-8"))["release"], foreign
        )
        self.assertEqual(
            self.sandbox.command(
                ["/usr/bin/git", "rev-parse", "v0.1.0^{tag}"], cwd=candidate
            ).stdout.strip(),
            before_tag,
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(remote)}",
                    "rev-parse",
                    "refs/tags/v0.1.0^{tag}",
                ]
            ).stdout.strip(),
            before_remote_tag,
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(remote)}",
                    "cat-file",
                    "-p",
                    "refs/tags/v0.1.0",
                ]
            ).stdout,
            before_remote_object,
        )

        (
            success_candidate,
            success_output,
            success_remote,
            success_state,
            success_env,
        ) = self._publication_fixture("publish-success")
        first = self._publish(success_candidate, success_output, success_env)
        self.assertEqual(first.returncode, 0, first.stderr)
        success_head = self.sandbox.command(
            ["/usr/bin/git", "rev-parse", "HEAD"], cwd=success_candidate
        ).stdout.strip()
        tag_type = self.sandbox.command(
            ["/usr/bin/git", "cat-file", "-t", "v0.1.0"], cwd=success_candidate
        )
        self.assertEqual(tag_type.stdout.strip(), "tag")
        local_tag_object = self.sandbox.command(
            ["/usr/bin/git", "rev-parse", "v0.1.0^{tag}"], cwd=success_candidate
        ).stdout.strip()
        remote_tag_type = self.sandbox.command(
            [
                "/usr/bin/git",
                f"--git-dir={self.sandbox.guest(success_remote)}",
                "cat-file",
                "-t",
                "refs/tags/v0.1.0",
            ]
        ).stdout.strip()
        remote_tag_object = self.sandbox.command(
            [
                "/usr/bin/git",
                f"--git-dir={self.sandbox.guest(success_remote)}",
                "rev-parse",
                "refs/tags/v0.1.0^{tag}",
            ]
        ).stdout.strip()
        self.assertEqual(remote_tag_type, "tag")
        self.assertEqual(remote_tag_object, local_tag_object)
        self.assertEqual(
            self.sandbox.command(
                ["/usr/bin/git", "rev-parse", "v0.1.0^{}"], cwd=success_candidate
            ).stdout.strip(),
            success_head,
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(success_remote)}",
                    "rev-parse",
                    "refs/tags/v0.1.0^{}",
                ]
            ).stdout.strip(),
            success_head,
        )
        first_release = json.loads(success_state.read_text(encoding="utf-8"))["release"]
        self.assertFalse(first_release["isDraft"])
        self.assertEqual(first_release["targetCommitish"], success_head)
        manifest = json.loads(
            (success_candidate / "tests" / "bdd_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        q04b_receipt = success_candidate / manifest["scenarios"]["BDD-Q04B"]["receipt"]
        self.assertEqual(
            json.loads(q04b_receipt.read_text(encoding="utf-8")),
            {
                "schemaVersion": 1,
                "bddId": "BDD-Q04B",
                "status": "passed",
                "commitSha": success_head,
                "archiveSha256": _sha256(success_output / ARCHIVE_NAME),
            },
        )
        self.assertFalse(
            (success_candidate / manifest["scenarios"]["BDD-Q05"]["receipt"]).exists()
        )
        self.assertEqual(
            {asset["name"] for asset in first_release["assets"]},
            {ARCHIVE_NAME, CHECKSUM_NAME},
        )
        publication_calls = json.loads(success_state.read_text(encoding="utf-8"))[
            "calls"
        ]
        create_index = next(
            index
            for index, call in enumerate(publication_calls)
            if call[:2] == ["release", "create"]
        )
        archive_upload_index = next(
            index
            for index, call in enumerate(publication_calls)
            if call[:2] == ["release", "upload"] and ARCHIVE_NAME in " ".join(call)
        )
        checksum_upload_index = next(
            index
            for index, call in enumerate(publication_calls)
            if call[:2] == ["release", "upload"] and CHECKSUM_NAME in " ".join(call)
        )
        publish_index = next(
            index
            for index, call in enumerate(publication_calls)
            if call[:2] == ["release", "edit"]
        )
        self.assertLess(
            create_index,
            archive_upload_index,
        )
        self.assertLess(archive_upload_index, checksum_upload_index)
        self.assertLess(checksum_upload_index, publish_index)
        self.assertIn("--draft", publication_calls[create_index])
        self.assertIn("--draft=false", publication_calls[publish_index])
        self.assertTrue(
            any(
                call[:2] == ["release", "view"]
                for call in publication_calls[checksum_upload_index + 1 : publish_index]
            ),
            publication_calls,
        )
        self.assertTrue(
            any(
                call[:2] == ["release", "view"]
                for call in publication_calls[publish_index + 1 :]
            ),
            publication_calls,
        )

        calls_before_retry = len(
            json.loads(success_state.read_text(encoding="utf-8"))["calls"]
        )
        retry_trace = self.sandbox.root / "retry-git-trace.jsonl"
        retry_env = dict(success_env)
        retry_env["GIT_TRACE2_EVENT"] = self.sandbox.guest(retry_trace)
        retried = self._publish(success_candidate, success_output, retry_env)

        self.assertEqual(retried.returncode, 0, retried.stderr)
        second_release = json.loads(success_state.read_text(encoding="utf-8"))[
            "release"
        ]
        self.assertEqual(second_release, first_release)
        retry_calls = json.loads(success_state.read_text(encoding="utf-8"))["calls"][
            calls_before_retry:
        ]
        self.assertTrue(retry_calls)
        self.assertTrue(
            all(
                call[:2] in (["repo", "view"], ["release", "view"])
                for call in retry_calls
            ),
            retry_calls,
        )
        for event_line in retry_trace.read_text(encoding="utf-8").splitlines():
            event = json.loads(event_line)
            argv = event.get("argv") if event.get("event") == "start" else None
            if not isinstance(argv, list):
                continue
            self.assertNotIn("push", argv, argv)
            if "tag" in argv:
                tag_args = argv[argv.index("tag") + 1 :]
                self.assertTrue("--list" in tag_args or "-l" in tag_args, argv)
        self.assertEqual(
            self.sandbox.command(
                ["/usr/bin/git", "rev-parse", "v0.1.0^{tag}"],
                cwd=success_candidate,
            ).stdout.strip(),
            local_tag_object,
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(success_remote)}",
                    "rev-parse",
                    "refs/tags/v0.1.0^{tag}",
                ]
            ).stdout.strip(),
            remote_tag_object,
        )

        for asset_index, field in itertools.product(
            range(len(first_release["assets"])), ("digest", "size", "name")
        ):
            with self.subTest(asset=asset_index, field=field):
                corrupted_state = json.loads(success_state.read_text(encoding="utf-8"))
                corrupted_state["release"] = json.loads(json.dumps(first_release))
                asset = corrupted_state["release"]["assets"][asset_index]
                asset[field] = (
                    "sha256:" + "0" * 64
                    if field == "digest"
                    else int(asset["size"]) + 1
                    if field == "size"
                    else f"wrong-{asset['name']}"
                )
                success_state.write_text(
                    json.dumps(corrupted_state) + "\n", encoding="utf-8"
                )
                before = json.loads(success_state.read_text(encoding="utf-8"))[
                    "release"
                ]
                rejected_asset = self._publish(
                    success_candidate, success_output, success_env
                )
                self.assertNotEqual(rejected_asset.returncode, 0)
                self.assertEqual(
                    json.loads(success_state.read_text(encoding="utf-8"))["release"],
                    before,
                )

        corrupted = json.loads(success_state.read_text(encoding="utf-8"))
        corrupted["release"] = json.loads(json.dumps(first_release))
        corrupted["release"]["assets"][0]["digest"] = "sha256:" + "0" * 64
        success_state.write_text(json.dumps(corrupted) + "\n", encoding="utf-8")
        corrupted_before = json.loads(success_state.read_text(encoding="utf-8"))[
            "release"
        ]
        wrong_digest = self._publish(success_candidate, success_output, success_env)
        self.assertNotEqual(wrong_digest.returncode, 0)
        self.assertEqual(
            json.loads(success_state.read_text(encoding="utf-8"))["release"],
            corrupted_before,
        )

        wrong_target_state = json.loads(success_state.read_text(encoding="utf-8"))
        wrong_target_state["release"] = dict(first_release)
        wrong_target_state["release"]["targetCommitish"] = "0" * 40
        success_state.write_text(
            json.dumps(wrong_target_state) + "\n", encoding="utf-8"
        )
        wrong_target_before = json.loads(success_state.read_text(encoding="utf-8"))[
            "release"
        ]

        wrong_target = self._publish(success_candidate, success_output, success_env)

        self.assertNotEqual(wrong_target.returncode, 0)
        self.assertEqual(
            json.loads(success_state.read_text(encoding="utf-8"))["release"],
            wrong_target_before,
        )

        (
            mismatch_candidate,
            _old_output,
            mismatch_remote,
            mismatch_state,
            mismatch_env,
        ) = self._publication_fixture("publish-mismatched-tag")
        committed = self.sandbox.command(
            [
                "/usr/bin/git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--allow-empty",
                "-qm",
                "new release commit",
            ],
            cwd=mismatch_candidate,
        )
        self.assertEqual(committed.returncode, 0, committed.stderr)
        pushed_head = self.sandbox.command(
            ["/usr/bin/git", "push", "-q", "origin", "HEAD:master"],
            cwd=mismatch_candidate,
        )
        self.assertEqual(pushed_head.returncode, 0, pushed_head.stderr)
        mismatch_output = self.sandbox.root / "publish-mismatched-tag-current-release"
        rebuilt = self._build_release(mismatch_candidate, mismatch_output)
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        created_old_tag = self.sandbox.command(
            [
                "/usr/bin/git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "tag",
                "-a",
                "v0.1.0",
                "HEAD^",
                "-m",
                "wrong target",
            ],
            cwd=mismatch_candidate,
        )
        self.assertEqual(created_old_tag.returncode, 0, created_old_tag.stderr)
        pushed_tag = self.sandbox.command(
            ["/usr/bin/git", "push", "-q", "origin", "refs/tags/v0.1.0"],
            cwd=mismatch_candidate,
        )
        self.assertEqual(pushed_tag.returncode, 0, pushed_tag.stderr)
        old_target = self.sandbox.command(
            ["/usr/bin/git", "rev-parse", "v0.1.0^{}"], cwd=mismatch_candidate
        ).stdout.strip()

        mismatch = self._publish(mismatch_candidate, mismatch_output, mismatch_env)

        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIsNone(
            json.loads(mismatch_state.read_text(encoding="utf-8"))["release"]
        )
        self.assertEqual(
            self.sandbox.command(
                ["/usr/bin/git", "rev-parse", "v0.1.0^{}"], cwd=mismatch_candidate
            ).stdout.strip(),
            old_target,
        )
        self.assertEqual(
            self.sandbox.command(
                [
                    "/usr/bin/git",
                    f"--git-dir={self.sandbox.guest(mismatch_remote)}",
                    "rev-parse",
                    "refs/tags/v0.1.0^{}",
                ]
            ).stdout.strip(),
            old_target,
        )


if __name__ == "__main__":
    unittest.main()
