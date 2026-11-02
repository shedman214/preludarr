# Preludarr project guidance

Read CONTRIBUTING.md and SECURITY.md before changing this repository. This file records project and release preferences for future Claude sessions; verify the live repository instead of relying on remembered version numbers.

## Sources of truth
- Application version: `APP_VERSION` in `preludarr/login.py`.
- Released behavior and release notes: `CHANGELOG.md` and the corresponding GitHub release.
- Installation and supported settings: `README.md` and `templates/my-preludarr.xml`.
- Tests and image publication gates: `.github/workflows/` and `tools/`.

## Development and review
- Inspect the existing code paths before changing behavior. Preserve existing installs and saved choices.
- Use feature branches and pull requests. Do not push product changes directly to main or bypass branch protection.
- Add meaningful regression tests for behavior changes. Required CI must pass before merging. Report limitations honestly.
- Keep secrets, real databases, private paths, logs and media out of commits and public issues.
- Inspect external service connections read-only first. Publishing a release does not authorize updating or restarting a user's containers.
- Posters and theme music are separate features. Automatic posters must not enable automatic theme downloads, replace cached posters, or bypass pause/cooldown/rate limits.
- Theme and provider verification links must use actual provider data, open securely, and never install or remember a match by following a link.
- Keep interface translations scoped accurately and preserve the English recovery controls.

## Release notes and changelogs
- Use Preludarr's own professional, plain, human voice. Lead with the version and a short user-benefit sentence.
- Use relevant sections in this order: `### ✨ Highlights`, `### ➕ Added`, `### 🔄 Changed`, `### 🔧 Fixed`, `### ⚠️ Upgrade notes`.
- Omit empty sections. Include Upgrade notes only when users need to act. Use emoji sparingly, normally one per heading.
- Highlights are brief benefits. Added/Changed/Fixed bullets use a bold feature label and explain the resulting behavior.
- Include reporters and issue numbers only when verified. Never publish placeholders.
- Use the same section structure in the matching CHANGELOG.md entry.
- Show the complete release-note draft and obtain approval before posting. Approval of an unchanged exact draft remains valid; do not ask again unless materially revising it.
- End with the exact versioned Docker image, applicable moving tags, concise Unraid update guidance and a clickable full changelog link. Do not claim an image is available until publication is verified.
- Keep APP_VERSION and the release tag aligned. Verify the image workflow, both supported architectures and version metadata before announcing availability. Never move an already-published tag to add documentation.
- Keep README and Community Applications wording current with shipped behavior; retain documented limitations.

## Memory handoffs
When a handoff is provided, distinguish released, merged, pending and deployed states. Update your own project memory/runbook only after checking the referenced commits, PRs and releases. Do not assume a container was deployed because a GitHub release exists.
