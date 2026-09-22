# UI accessibility and responsive qualification

The redesigned codex-web workspace treats accessibility and responsive behavior as release qualification, not as a per-feature afterthought.

## Automated matrix

Primary workflow browser coverage runs at representative phone (390 px), tablet / narrow desktop (768 px), and desktop (1280 px) widths. The qualification suite verifies:

- named dialogs and labelled primary controls
- keyboard entry into workspace navigation and predictable focus order
- visible focus treatment for operator actions
- no page-level horizontal overflow
- 44 px minimum touch targets on phone layouts
- 200% text scaling without loss of primary controls or page-level overflow
- status semantics that include text / symbols rather than color alone
- polite live-region semantics for dynamic operator status
- reduced-motion behavior through the shared responsive stylesheet

The Work Item operator and workspace shell are representative primary workflows. Feature-local browser tests continue to cover their detailed interaction and authorization behavior.

## Responsive contract

- **Phone:** navigation becomes a drawer, primary dialogs become viewport-sized, controls wrap/stack, and horizontal scrolling is constrained to intentionally scrollable table/code regions.
- **Tablet / narrow desktop:** multi-column workspace layouts collapse where necessary while preserving Project context and primary actions.
- **Desktop:** richer multi-column layouts are allowed, but data volumes and rendering remain bounded by the frontend performance contract.

The shared `mobile_responsive.css` layer owns generic layout behavior. Product modules should not introduce independent page-level viewport hacks.

## Accessibility contract

Interactive controls must be reachable without hover and have a programmatic name. Dynamic status changes use an appropriate live region. Visual status must not rely on color alone. Focus indicators must remain visible for keyboard users.

Motion is non-essential. When `prefers-reduced-motion: reduce` is active, shared animation and transition durations collapse to effectively zero and smooth scrolling is disabled.

## Manual release checks

Automated checks do not prove WCAG conformance. Before a major UI release, manually verify the primary workflows with:

1. keyboard-only navigation, including open/close/focus restoration for dialogs and drawers;
2. a screen reader pass over the workspace shell, Work Item operator, Inbox/Attention, and Operations surfaces;
3. browser zoom at 200% and narrow-window resizing;
4. high-contrast / forced-colors behavior where supported;
5. mobile virtual-keyboard interaction with the composer and representative forms;
6. touch interaction on at least one phone-sized device or emulator.

Failures should identify the affected surface and state in the test name so regressions are actionable without relying solely on screenshot inspection.
