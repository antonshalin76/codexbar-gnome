# Changelog

## 0.1.1 - 2026-09-04

- Made GitHub release verification robust when asset digests are temporarily or permanently unavailable.
- Kept post-release verification bound to the immutable release commit while allowing repository development to continue.
- Updated GitHub Actions to maintained Node 24 releases and isolated generated release evidence from test fixtures.

## 0.1.0 - 2026-09-04

- Stabilized Codex, Grok, and Claude/Z.AI quota polling with bounded process execution.
- Added atomic settings persistence, concurrent refresh coordination, and native GTK details.
- Added transactional installation, ownership-safe removal, deterministic release archives, and CI gates.
- Added deterministic unit, integration, lifecycle, packaging, and X11 end-to-end coverage.
