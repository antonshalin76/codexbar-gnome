from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import patch
from importlib.machinery import SourceFileLoader
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "bin" / "codexbar-gnome-indicator"
SPEC = importlib.util.spec_from_loader(
    "codexbar_gnome_indicator",
    SourceFileLoader("codexbar_gnome_indicator", str(MODULE_PATH)),
)
assert SPEC is not None and SPEC.loader is not None
indicator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(indicator)


class GrokRuntimeTests(unittest.TestCase):
    def test_main_keeps_indicator_alive_for_the_gtk_loop(self) -> None:
        instance = object()
        with patch.object(indicator, "CodexBarIndicator", return_value=instance) as create, patch.object(
            indicator.Gtk, "main",
        ) as gtk_main:
            indicator.main()
        create.assert_called_once_with()
        gtk_main.assert_called_once_with()

    def test_default_source_defers_to_codexbar_provider_configuration(self) -> None:
        completed = type("Completed", (), {"stdout": "[]", "stderr": "", "returncode": 0})()
        with patch.object(indicator, "SOURCE", ""), patch.object(
            indicator.subprocess, "run", return_value=completed,
        ) as run:
            indicator._run_codexbar_provider("claude")
        self.assertNotIn("--source", run.call_args.args[0])

    def test_legacy_oauth_source_falls_back_to_auto_for_grok_only(self) -> None:
        completed = type("Completed", (), {"stdout": "[]", "stderr": "", "returncode": 0})()
        with patch.object(indicator, "SOURCE", "oauth"), patch.object(
            indicator.subprocess, "run", return_value=completed,
        ) as run:
            indicator._run_codexbar_provider("grok")
            self.assertEqual(run.call_args.args[0][-2:], ["--source", "auto"])
            indicator._run_codexbar_provider("codex")
            self.assertEqual(run.call_args.args[0][-2:], ["--source", "oauth"])

    def test_defaults_enable_codex_and_grok_but_not_retired_claude(self) -> None:
        settings = indicator._normalized_settings(None)
        self.assertEqual(indicator._enabled_providers(settings, auto_only=True), ["codex", "grok"])
        self.assertFalse(settings["runtimes"]["claude"]["poll"])

    def test_panel_and_rows_render_grok_primary_window(self) -> None:
        payload = [
            {"provider": "codex", "usage": {"secondary": {"usedPercent": 37}}},
            {"provider": "grok", "usage": {"primary": {"usedPercent": 12}}},
        ]
        self.assertEqual(indicator._panel_label(payload, False), "CxW 37%  GkS 12%")
        self.assertEqual(
            [row["label"] for row in indicator._collect_limit_rows(payload)],
            ["Codex Week", "Grok Session"],
        )

    def test_panel_renders_both_zai_claude_windows_compactly(self) -> None:
        payload = [
            {"provider": "codex", "usage": {"secondary": {"usedPercent": 37}}},
            {"provider": "grok", "usage": {"primary": {"usedPercent": 12}}},
            {"provider": "claude", "source": "zai", "usage": {
                "primary": {"usedPercent": 18}, "secondary": {"usedPercent": 96},
            }},
        ]
        self.assertEqual(indicator._panel_label(payload, False), "CxW 37%  GkS 12%  Cl 18%/96%")

    def test_zai_quota_is_rendered_as_claude_windows(self) -> None:
        payload = indicator._zai_usage_payload({
            "data": {"limits": [
                {"type": "TOKENS_LIMIT", "percentage": 18, "nextResetTime": 1787938975905},
                {"type": "TOKENS_LIMIT", "percentage": 96, "nextResetTime": 1788120817998},
                {"type": "TIME_LIMIT", "percentage": 6, "nextResetTime": 1788984817997},
            ]}
        })
        self.assertEqual(
            [row["label"] for row in indicator._collect_limit_rows(payload)],
            ["Claude (Z.AI) 5h", "Claude (Z.AI) period"],
        )

    def test_legacy_settings_gain_grok_without_reenabling_claude(self) -> None:
        settings = indicator._normalized_settings({
            "runtimes": {
                "codex": {"poll": True, "autoRefresh": False},
                "claude": {"poll": False, "autoRefresh": True},
            }
        })
        self.assertEqual(settings["runtimes"]["grok"], {"poll": True, "autoRefresh": True})
        self.assertEqual(settings["runtimes"]["claude"], {"poll": False, "autoRefresh": False})


if __name__ == "__main__":
    unittest.main()
