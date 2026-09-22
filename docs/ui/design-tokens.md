# UI design tokens

`static/design_tokens.css` is the presentation source of truth for shared codex-web visual values. New product UI should consume the `--cw-*` tokens instead of introducing page-local values for normal typography, spacing, control sizing, radius, surfaces, semantic status, focus, elevation, or motion.

## Token groups

- **Typography:** `--cw-font-*`, font sizes, and line heights.
- **Spacing and sizing:** `--cw-space-*`, control heights, and content widths.
- **Shape and elevation:** radius, border width, and shared shadows.
- **Surfaces/content:** background, surface, border, and text hierarchy.
- **Semantic state:** accent, success, warning, danger, and info colors.
- **Interaction:** focus ring/color, disabled opacity, and transition durations.
- **Responsive thresholds:** phone 640px, tablet 900px, wide 1200px. CSS custom properties cannot be used in media-query conditions, so media-query literals must stay synchronized with these documented token values.

## Theme contract

Light values live on `:root`. Dark-theme overrides live on `:root[data-theme="dark"]`. A component should normally reference one semantic token and let the theme select its value rather than branching on theme itself.

Do not encode state meaning only through color. Status UI should retain text/icon/structural cues, and interactive controls must retain a visible `:focus-visible` treatment. The shared focus primitives use a two-pixel outline plus a larger ring so keyboard focus remains visible against both supported themes.

## Motion

Normal interactions use `--cw-transition-fast` or `--cw-transition-standard`. Under `prefers-reduced-motion: reduce`, both durations become zero. Components must not override that preference with hard-coded transition or animation durations unless the motion is essential.

## Migration

Existing compatibility variables in `static/styles.css` may map to `--cw-*` tokens while older surfaces are migrated. New components should prefer the canonical `--cw-*` names directly. Page-specific values are appropriate only when they represent genuinely local geometry or domain-specific visualization data.

## Validation

`tests/test_design_tokens.py` verifies that the token stylesheet is loaded before application styles, required foundation groups exist, and every `var(--cw-...)` reference in static CSS resolves to a defined token. This prevents misspelled or undefined shared tokens from silently falling back in browsers.
