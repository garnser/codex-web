# Reproducible documentation screenshots

The documentation screenshot set is generated from deterministic, sanitized browser fixtures already used by the UI regression suite. The current manifest covers the redesigned workspace shell plus representative Home, Work, Team, Attention, Automation, Operations, mobile, and state-specific views.

## Refresh

Install the locked browser dependencies and Chromium, then run:

```bash
npm ci
npx playwright install chromium
npm run docs:screenshots
```

Generated PNGs are written to:

```text
docs/assets/screenshots/generated/
```

The generated directory is intentionally build output. CI uploads it as a workflow artifact so reviewers can inspect current screenshots without committing production/user data.

## Manifest

[manifest.json](manifest.json) is the source of truth for:

- fixture URL;
- stable screenshot name;
- required visible landmarks;
- annotation topics;
- forbidden secret-like markers;
- the optional deterministic action used to open a captured surface (selector or UI event).

If a UI change removes a required landmark, the browser documentation test fails. Update the fixture/manifest and regenerate screenshots as part of the same intentional UI/documentation change.

## Sanitization rules

Fixtures must use fictional organizations, people, repositories, Resources, credentials and IDs. Never copy a production page into this workflow.

The capture process fails on common live-token/private-key prefixes. This is a guardrail, not a substitute for review.

## Screenshot review rule

A materially stale screenshot is a documentation defect. When a UI PR changes one of the captured surfaces:

1. update its browser fixture/spec if needed;
2. update manifest landmarks/annotations intentionally;
3. run `npm run docs:screenshots`;
4. inspect the generated PNGs;
5. ensure the corresponding documentation remains accurate.

The screenshot artifact complements, rather than replaces, browser assertions.
