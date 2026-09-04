#!/usr/bin/env python3
"""Exercise the packaged indicator against a real X11 and session D-Bus."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from gi.repository import Gio, GLib

APP_ID = "io.github.antonshalin76.CodexBarGnome"
DESKTOP_NAME = "codexbar-gnome-indicator.desktop"
EXPECTED_LABEL = "CxW 7%"
EVENT_NAMES = (
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
)

T = TypeVar("T")


class E2EError(RuntimeError):
    """A bounded end-to-end contract failure."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def wait_for(
    predicate: Callable[[], T],
    description: str,
    *,
    timeout: float = 12.0,
    processes: tuple[subprocess.Popen[object], ...] = (),
) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        for process in processes:
            return_code = process.poll()
            if return_code is not None:
                raise E2EError(
                    f"process exited with {return_code} while waiting for {description}"
                )
        time.sleep(0.02)
    raise E2EError(f"timeout waiting for {description}")


def terminate_exact(process: subprocess.Popen[object] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def verified_extract(
    archive_path: Path, destination: Path
) -> tuple[Path, dict[str, bytes]]:
    if not archive_path.is_file() or archive_path.is_symlink():
        raise E2EError("archive must be a regular file")
    if archive_path.stat().st_size > 10 * 1024 * 1024:
        raise E2EError("archive exceeds the end-to-end size limit")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        roots = {member.name.split("/", 1)[0] for member in members}
        if len(roots) != 1:
            raise E2EError("archive must have one top-level directory")
        root_name = roots.pop()
        if not root_name.startswith("codexbar-gnome-"):
            raise E2EError("archive top-level directory is invalid")
        expected = {
            root_name: ("directory", 0o755),
            f"{root_name}/bin": ("directory", 0o755),
            f"{root_name}/share": ("directory", 0o755),
            f"{root_name}/VERSION": ("file", 0o644),
            f"{root_name}/CHANGELOG.md": ("file", 0o644),
            f"{root_name}/LICENSE": ("file", 0o644),
            f"{root_name}/README.md": ("file", 0o644),
            f"{root_name}/install.sh": ("file", 0o755),
            f"{root_name}/uninstall.sh": ("file", 0o755),
            f"{root_name}/bin/codexbar-gnome-indicator": ("file", 0o755),
            f"{root_name}/share/{DESKTOP_NAME}": ("file", 0o644),
        }
        observed: dict[str, tuple[str, int]] = {}
        payloads: dict[str, bytes] = {}
        for member in members:
            kind = (
                "file"
                if member.isfile()
                else "directory"
                if member.isdir()
                else "other"
            )
            observed[member.name] = (kind, member.mode)
            if (
                member.uid != 0
                or member.gid != 0
                or member.uname
                or member.gname
                or member.mtime != 0
                or member.pax_headers
            ):
                raise E2EError("archive metadata is not reproducible")
            if member.size > 4 * 1024 * 1024:
                raise E2EError(f"archive member is oversized: {member.name}")
        if observed != expected or [member.name for member in members] != sorted(
            expected
        ):
            raise E2EError("archive member contract does not match")

        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(member.mode)
            elif member.isfile():
                source = archive.extractfile(member)
                if source is None:
                    raise E2EError(f"archive payload is missing: {member.name}")
                payload = source.read()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                target.chmod(member.mode)
                payloads[member.name] = payload
            else:
                raise E2EError(f"unsafe archive member: {member.name}")
    version = payloads[f"{root_name}/VERSION"].decode("ascii").strip()
    if root_name != f"codexbar-gnome-{version}":
        raise E2EError("archive version and root directory disagree")
    return destination / root_name, payloads


WATCHER_CODE = r'''\
import pathlib
import sys

from gi.repository import Gio, GLib

NAME = "org.kde.StatusNotifierWatcher"
PATH = "/StatusNotifierWatcher"
INTERFACE = NAME
XML = """
<node>
  <interface name="org.kde.StatusNotifierWatcher">
    <method name="RegisterStatusNotifierItem">
      <arg type="s" name="service" direction="in"/>
    </method>
    <method name="RegisterStatusNotifierHost">
      <arg type="s" name="service" direction="in"/>
    </method>
    <property name="RegisteredStatusNotifierItems" type="as" access="read"/>
    <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
    <property name="ProtocolVersion" type="i" access="read"/>
    <signal name="StatusNotifierItemRegistered">
      <arg type="s" name="service"/>
    </signal>
    <signal name="StatusNotifierItemUnregistered">
      <arg type="s" name="service"/>
    </signal>
    <signal name="StatusNotifierHostRegistered"/>
  </interface>
</node>
"""

items = []
connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

def changed():
    connection.emit_signal(
        None,
        PATH,
        "org.freedesktop.DBus.Properties",
        "PropertiesChanged",
        GLib.Variant(
            "(sa{sv}as)",
            (
                INTERFACE,
                {"RegisteredStatusNotifierItems": GLib.Variant("as", items)},
                [],
            ),
        ),
    )

def method_call(
    _connection, sender, _path, _interface, method, parameters, invocation
):
    if method == "RegisterStatusNotifierItem":
        requested = parameters.unpack()[0]
        if requested.startswith("/"):
            item = sender + requested
        elif "/" in requested:
            item = requested
        else:
            item = requested + "/StatusNotifierItem"
        if item not in items:
            items.append(item)
            changed()
            connection.emit_signal(
                None,
                PATH,
                INTERFACE,
                "StatusNotifierItemRegistered",
                GLib.Variant("(s)", (item,)),
            )
        invocation.return_value(GLib.Variant("()", ()))
        return
    if method == "RegisterStatusNotifierHost":
        invocation.return_value(GLib.Variant("()", ()))
        connection.emit_signal(
            None, PATH, INTERFACE, "StatusNotifierHostRegistered", None
        )
        return
    invocation.return_dbus_error("org.kde.StatusNotifierWatcher.Error", "unknown method")

def get_property(_connection, _sender, _path, _interface, name):
    if name == "RegisteredStatusNotifierItems":
        return GLib.Variant("as", items)
    if name == "IsStatusNotifierHostRegistered":
        return GLib.Variant("b", True)
    if name == "ProtocolVersion":
        return GLib.Variant("i", 0)
    return None

node = Gio.DBusNodeInfo.new_for_xml(XML)
registration = connection.register_object(
    PATH, node.interfaces[0], method_call, get_property, None
)
if not registration:
    raise RuntimeError("unable to register watcher object")
reply = connection.call_sync(
    "org.freedesktop.DBus",
    "/org/freedesktop/DBus",
    "org.freedesktop.DBus",
    "RequestName",
    GLib.Variant("(su)", (NAME, 0)),
    GLib.VariantType("(u)"),
    Gio.DBusCallFlags.NONE,
    3000,
    None,
)
if reply.unpack()[0] != 1:
    raise RuntimeError("unable to own watcher name")
pathlib.Path(sys.argv[1]).touch()
GLib.MainLoop().run()
'''


FAKE_CODE = r"""\
import fcntl
import json
import os
import pathlib
import sys

ledger = pathlib.Path(os.environ["CODEXBAR_E2E_LEDGER"])
ledger.parent.mkdir(parents=True, exist_ok=True)
argv = sys.argv[1:]
with ledger.open("a", encoding="utf-8") as stream:
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    stream.write(json.dumps({"argv": argv}, sort_keys=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())
if argv == ["usage", "--help"]:
    print("usage --json-only --provider codex grok claude zai")
elif argv == [
    "usage", "--provider", "codex", "--json-only", "--no-color"
]:
    print(json.dumps([{
        "provider": "codex",
        "usage": {
            "primary": {"usedPercent": 3},
            "secondary": {"usedPercent": 7},
        },
    }]))
else:
    print("unsupported test provider command", file=sys.stderr)
    raise SystemExit(64)
"""


def create_fake_provider(probe: Path) -> tuple[Path, Path]:
    fake_bin = probe / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    executable = fake_bin / "codexbar"
    executable.write_text("#!/usr/bin/python3\n" + FAKE_CODE, encoding="utf-8")
    executable.chmod(0o755)
    ledger = probe / "provider-ledger.jsonl"
    return executable, ledger


def provider_records(ledger: Path) -> list[object]:
    if not ledger.exists():
        return []
    try:
        with ledger.open("r", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            lines = stream.read().splitlines()
        return [json.loads(line) for line in lines if line]
    except (OSError, json.JSONDecodeError) as error:
        raise E2EError("provider ledger is invalid") from error


def call(
    connection: Gio.DBusConnection,
    destination: str,
    path: str,
    interface: str,
    method: str,
    parameters: GLib.Variant,
    reply_type: GLib.VariantType | None = None,
) -> GLib.Variant:
    return connection.call_sync(
        destination,
        path,
        interface,
        method,
        parameters,
        reply_type,
        Gio.DBusCallFlags.NONE,
        3000,
        None,
    )


def has_owner(connection: Gio.DBusConnection, name: str) -> bool:
    reply = call(
        connection,
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        GLib.Variant("(s)", (name,)),
        GLib.VariantType("(b)"),
    )
    return bool(reply.unpack()[0])


def get_property(
    connection: Gio.DBusConnection,
    destination: str,
    path: str,
    interface: str,
    name: str,
) -> object:
    reply = call(
        connection,
        destination,
        path,
        "org.freedesktop.DBus.Properties",
        "Get",
        GLib.Variant("(ss)", (interface, name)),
        GLib.VariantType("(v)"),
    )
    return reply.unpack()[0]


def registered_items(connection: Gio.DBusConnection) -> list[str]:
    value = get_property(
        connection,
        "org.kde.StatusNotifierWatcher",
        "/StatusNotifierWatcher",
        "org.kde.StatusNotifierWatcher",
        "RegisteredStatusNotifierItems",
    )
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise E2EError("watcher returned invalid registered items")
    return value


def split_item(value: str) -> tuple[str, str]:
    if value.startswith("/"):
        raise E2EError("watcher returned an item without a service")
    slash = value.find("/")
    if slash < 0:
        return value, "/StatusNotifierItem"
    return value[:slash], value[slash:]


def collect_menu(node: object, result: dict[str, int]) -> None:
    if not isinstance(node, tuple) or len(node) != 3:
        raise E2EError("D-Bus menu layout has an invalid shape")
    item_id, properties, children = node
    if not isinstance(item_id, int) or not isinstance(properties, dict):
        raise E2EError("D-Bus menu item has an invalid shape")
    label = properties.get("label")
    if hasattr(label, "unpack"):
        label = label.unpack()
    if isinstance(label, str):
        result[label] = item_id
    if not isinstance(children, list):
        raise E2EError("D-Bus menu children have an invalid shape")
    for child in children:
        collect_menu(child, result)


def menu_event(
    connection: Gio.DBusConnection,
    service: str,
    menu_path: str,
    item_id: int,
) -> None:
    call(
        connection,
        service,
        menu_path,
        "com.canonical.dbusmenu",
        "Event",
        GLib.Variant("(isvu)", (item_id, "clicked", GLib.Variant("s", ""), 0)),
    )


def primary_process_count(installed: Path) -> int:
    expected = os.fsencode(installed)
    count = 0
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            arguments = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
        if expected in arguments and b"--child-supervisor" not in arguments:
            count += 1
    return count


def managed_paths(home: Path) -> tuple[Path, ...]:
    data_home = Path(os.environ.get("XDG_DATA_HOME") or str(home / ".local/share"))
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or str(home / ".config"))
    state_home = Path(os.environ.get("XDG_STATE_HOME") or str(home / ".local/state"))
    return (
        home / ".local/bin/codexbar-gnome-indicator",
        data_home / "applications" / DESKTOP_NAME,
        config_home / "autostart" / DESKTOP_NAME,
        state_home / "codexbar-gnome/install-manifest.json",
    )


def run(options: argparse.Namespace) -> dict[str, object]:
    archive_path = options.archive.resolve()
    probe = options.probe_dir.resolve()
    probe.mkdir(parents=True, exist_ok=True)
    archive_digest = sha256(archive_path)
    events: list[dict[str, object]] = []
    watcher: subprocess.Popen[object] | None = None
    application: subprocess.Popen[object] | None = None
    extracted: Path | None = None
    app_log = None
    home = Path.home()
    installed = managed_paths(home)[0]
    installed_hashes_match = False
    details_non_blocking = False
    process_count = 0
    panel_label = ""
    temporary = tempfile.TemporaryDirectory(prefix="codexbar-e2e-")

    def passed(name: str) -> None:
        expected = EVENT_NAMES[len(events)]
        if name != expected:
            raise E2EError(f"event order violation: expected {expected}, got {name}")
        events.append({"name": name, "passed": True})

    try:
        extraction_root = Path(temporary.name)
        if any(path.exists() or path.is_symlink() for path in managed_paths(home)):
            raise E2EError("end-to-end HOME already contains managed paths")
        else:
            extracted, archive_payloads = verified_extract(
                archive_path, extraction_root
            )
            passed("archive-verified")

            installed_result = subprocess.run(
                [str(extracted / "install.sh")],
                cwd=extracted,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if installed_result.returncode != 0:
                raise E2EError(
                    "installer failed: "
                    + (installed_result.stderr.strip() or "no diagnostic")[:512]
                )
            installed_payloads = {
                "indicator": installed.read_bytes(),
                "desktop": managed_paths(home)[1].read_bytes(),
                "autostart": managed_paths(home)[2].read_bytes(),
            }
            root_name = extracted.name
            installed_hashes_match = (
                installed_payloads["indicator"]
                == archive_payloads[f"{root_name}/bin/codexbar-gnome-indicator"]
                and installed_payloads["desktop"]
                == archive_payloads[f"{root_name}/share/{DESKTOP_NAME}"]
                and installed_payloads["autostart"]
                == archive_payloads[f"{root_name}/share/{DESKTOP_NAME}"]
            )
            if not installed_hashes_match:
                raise E2EError("installed files differ from the packaged artifact")
            passed("installed")

            fake_provider, ledger = create_fake_provider(probe)
            config = (
                Path(os.environ.get("XDG_CONFIG_HOME") or str(home / ".config"))
                / "codexbar-gnome/config.json"
            )
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                json.dumps(
                    {
                        "runtimes": {
                            "codex": {"poll": True, "autoRefresh": True},
                            "grok": {"poll": False, "autoRefresh": False},
                            "claude": {"poll": False, "autoRefresh": False},
                        }
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            config.chmod(0o600)

            watcher_ready = probe / "watcher.ready"
            watcher = subprocess.Popen(
                [sys.executable, "-c", WATCHER_CODE, str(watcher_ready)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            wait_for(
                watcher_ready.exists,
                "StatusNotifierWatcher startup",
                processes=(watcher,),
            )

            environment = dict(os.environ)
            environment.update(
                {
                    "CODEXBAR_BIN": str(fake_provider),
                    "CODEXBAR_E2E_LEDGER": str(ledger),
                    "CODEXBAR_GNOME_CONFIG": str(config),
                    "CODEXBAR_INDICATOR_REFRESH_SECONDS": "86400",
                }
            )
            app_log = (probe / "application.log").open("w", encoding="utf-8")
            application = subprocess.Popen(
                [str(installed)],
                stdin=subprocess.DEVNULL,
                stdout=app_log,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
                close_fds=True,
            )
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            wait_for(
                lambda: has_owner(connection, APP_ID),
                "application D-Bus owner",
                processes=(application, watcher),
            )
            passed("owner-acquired")

            items = wait_for(
                lambda: registered_items(connection),
                "StatusNotifierItem registration",
                processes=(application, watcher),
            )
            if len(items) != 1:
                raise E2EError(f"expected one StatusNotifierItem, got {len(items)}")
            service, item_path = split_item(items[0])
            passed("status-notifier-registered")

            def expected_label() -> str:
                try:
                    value = get_property(
                        connection,
                        service,
                        item_path,
                        "org.kde.StatusNotifierItem",
                        "XAyatanaLabel",
                    )
                except GLib.Error:
                    return ""
                return value if value == EXPECTED_LABEL else ""

            panel_label = wait_for(
                expected_label,
                "first provider refresh",
                processes=(application, watcher),
            )
            process_count = primary_process_count(installed)
            if process_count != 1:
                raise E2EError(f"expected one primary process, got {process_count}")
            passed("first-refresh")

            menu_path = get_property(
                connection,
                service,
                item_path,
                "org.kde.StatusNotifierItem",
                "Menu",
            )
            if not isinstance(menu_path, str) or not menu_path.startswith("/"):
                raise E2EError("StatusNotifierItem returned an invalid menu path")
            layout_reply = call(
                connection,
                service,
                menu_path,
                "com.canonical.dbusmenu",
                "GetLayout",
                GLib.Variant("(iias)", (0, -1, [])),
            )
            layout = layout_reply.unpack()[1]
            menu: dict[str, int] = {}
            collect_menu(layout, menu)
            missing = {"Show details", "Refresh", "Quit"} - set(menu)
            if missing:
                raise E2EError(f"D-Bus menu is missing: {', '.join(sorted(missing))}")
            passed("menu-layout-read")

            menu_event(connection, service, menu_path, menu["Show details"])
            details_non_blocking = has_owner(connection, APP_ID)
            if not details_non_blocking:
                raise E2EError("details action blocked or stopped the application")
            passed("details-opened")

            before = len(provider_records(ledger))
            menu_event(connection, service, menu_path, menu["Refresh"])
            wait_for(
                lambda: len(provider_records(ledger)) > before,
                "manual provider refresh",
                processes=(application, watcher),
            )
            details_non_blocking = details_non_blocking and has_owner(
                connection, APP_ID
            )
            if not details_non_blocking:
                raise E2EError("application stopped after the details action")
            passed("post-details-action")

            if options.external_observer:
                (probe / "first-refresh.ready").touch()
                wait_for(
                    lambda: (probe / "observer-complete").exists(),
                    "external observer completion",
                    processes=(application, watcher),
                )
            else:
                menu_event(connection, service, menu_path, menu["Quit"])
            wait_for(
                lambda: not has_owner(connection, APP_ID),
                "application D-Bus owner release",
                processes=(watcher,),
            )
            try:
                application.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise E2EError("application did not exit after Quit") from error
            if application.returncode != 0:
                raise E2EError(f"application exited with {application.returncode}")
            passed("owner-released")

            uninstalled = subprocess.run(
                [str(extracted / "uninstall.sh")],
                cwd=extracted,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if uninstalled.returncode != 0:
                raise E2EError(
                    "uninstaller failed: "
                    + (uninstalled.stderr.strip() or "no diagnostic")[:512]
                )
            uninstall_clean = all(not path.exists() for path in managed_paths(home))
            if not uninstall_clean:
                raise E2EError("managed paths remain after uninstall")
            passed("uninstalled")

            return {
                "schemaVersion": 1,
                "status": "passed",
                "archiveSha256": archive_digest,
                "skipped": [],
                "events": events,
                "process": {"primaryCount": process_count},
                "panelLabel": panel_label,
                "installedHashesMatchedArchive": installed_hashes_match,
                "detailsNonBlocking": details_non_blocking,
                "uninstallClean": uninstall_clean,
            }
    finally:
        if application is not None and application.poll() is None:
            subprocess.run(
                ["gapplication", "action", APP_ID, "quit"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            terminate_exact(application)
        terminate_exact(watcher)
        if app_log is not None:
            app_log.close()
        if extracted is not None and managed_paths(home)[3].exists():
            subprocess.run(
                [str(extracted / "uninstall.sh")],
                cwd=extracted,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        temporary.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--probe-dir", type=Path, required=True)
    parser.add_argument(
        "--external-observer",
        action="store_true",
        help="pause before Quit for a separate D-Bus observer",
    )
    options = parser.parse_args()
    try:
        value = run(options)
    except (
        E2EError,
        GLib.Error,
        OSError,
        ValueError,
        subprocess.SubprocessError,
        tarfile.TarError,
    ) as error:
        write_json(
            options.report.resolve(),
            {
                "schemaVersion": 1,
                "status": "failed",
                "error": f"{type(error).__name__}: {error}"[:512],
            },
        )
        print(f"e2e-x11: {type(error).__name__}: {error}"[:1024], file=sys.stderr)
        return 1
    write_json(options.report.resolve(), value)
    print("Exact-artifact X11 end-to-end check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
