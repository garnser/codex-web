from __future__ import annotations

import html
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

VERIDATAOPS_ROOT = Path(
    str(Path.home() / "veridataops")
)
ARTIFACTS_DIR = VERIDATAOPS_ROOT / "artifacts"
DEV_HEALTH_CACHE_PATH = ARTIFACTS_DIR / "dev-health-check.json"
DEV_HEALTH_SCRIPT = VERIDATAOPS_ROOT / "platform" / "scripts" / "dev_health_check.py"
RELEASE_REPORT_PATH = VERIDATAOPS_ROOT / "release-e2e-report.json"
REPORT_REFRESH_SECONDS = 120
CONTEXT_CACHE_SECONDS = 30

_cached_context: dict[str, Any] | None = None
_cached_at = 0.0


def build_context(force_refresh: bool = False) -> dict[str, Any]:
    global _cached_at, _cached_context
    now = time.time()
    if not force_refresh and _cached_context and now - _cached_at < CONTEXT_CACHE_SECONDS:
        return _cached_context

    errors: list[str] = []
    health_report = _load_dev_health_report(force_refresh=force_refresh, errors=errors)
    release_report = _load_json_file(RELEASE_REPORT_PATH, errors=errors, label="latest release report")
    context = {
        "generated_at": (health_report or {}).get("generated_at"),
        "overall_state": _overall_state(health_report, release_report),
        "summary": _summary_cards(health_report),
        "release": _release_context(health_report),
        "release_validation": _release_validation_context(release_report),
        "canonical_items": ((health_report or {}).get("canonical_check") or {}).get("items", []),
        "split_brain_items": ((health_report or {}).get("split_brain") or {}).get("items", []),
        "routing_items": ((health_report or {}).get("routing_drift") or {}).get("items", []),
        "checkout_items": [
            item
            for item in (((health_report or {}).get("checkout_branch_drift") or {}).get("items") or [])
            if not item.get("ok", False)
        ],
        "merge_requests": ((health_report or {}).get("merge_requests") or {}).get("items", []),
        "errors": errors,
    }
    _cached_context = context
    _cached_at = now
    return context


def render_html(context: dict[str, Any]) -> str:
    overall = context.get("overall_state") or {}
    release = context.get("release") or {}
    validation = context.get("release_validation") or {}
    summary_cards = "".join(
        f"""
        <article class="summary-card">
          <span>{_escape(card.get("label"))}</span>
          <strong>{_escape(card.get("value"))}</strong>
        </article>
        """
        for card in context.get("summary") or []
    )
    load_warnings = ""
    if context.get("errors"):
        warnings_html = "".join(f"<li>{_escape(item)}</li>" for item in context["errors"])
        load_warnings = f"""
        <section class="panel wide">
          <div class="section-heading">
            <div>
              <h2>Load Warnings</h2>
              <p class="muted">The dashboard is still rendered, but one or more sources could not be refreshed cleanly.</p>
            </div>
          </div>
          <ul>{warnings_html}</ul>
        </section>
        """

    release_validation_steps = "".join(
        f"""
        <details class="detail-card">
          <summary>{_escape(step.get("name"))} <span class="pill {step.get("tone", "warn")}">{_escape(step.get("status"))}</span></summary>
          {_detail_row('Duration', f"{step.get('duration_seconds')}s" if step.get('duration_seconds') is not None else None)}
          {_paragraph(step.get("detail"))}
        </details>
        """
        for step in validation.get("steps") or []
    )

    release_images = ""
    if validation.get("images"):
        image_rows = "".join(
            f"<tr><td>{_escape(item.get('name'))}</td><td><code>{_escape(item.get('tag'))}</code></td></tr>"
            for item in validation["images"]
        )
        release_images = f"""
        <details class="detail-card">
          <summary>Release images</summary>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Component</th><th>Image Tag</th></tr></thead>
              <tbody>{image_rows}</tbody>
            </table>
          </div>
        </details>
        """

    release_stack = "".join(
        f"""
        <div class="stack-row">
          <div>
            <strong>{_escape(item.get("component"))}</strong>
            <span class="muted">valid={_escape(item.get("latest_valid_tag") or "none")}{_latest_visible_suffix(item)}</span>
          </div>
          <span class="pill {item.get('status', {}).get('tone', 'warn')}">{_escape(item.get('status', {}).get('label'))}</span>
        </div>
        """
        for item in release.get("items") or []
    )

    release_table_rows = "".join(
        f"""
        <tr>
          <td><strong>{_escape(item.get("component"))}</strong></td>
          <td><span class="pill {item.get('status', {}).get('tone', 'warn')}">{_escape(item.get('status', {}).get('label'))}</span></td>
          <td><code>{_escape(item.get("latest_valid_tag") or "none")}</code></td>
          <td><code>{_escape(item.get("latest_any_tag") or "none")}</code></td>
          <td><code>{_escape(item.get("main_sha") or "unknown")}</code></td>
          <td>main_ahead={_escape(item.get("main_ahead_of_valid_tag"))}<br>tag_ahead={_escape(item.get("valid_tag_ahead_of_main"))}</td>
        </tr>
        """
        for item in release.get("items") or []
    ) or '<tr><td colspan="6">No release tag data is available.</td></tr>'

    canonical_rows = _finding_rows(context.get("canonical_items") or [], mode="findings") or '<tr><td colspan="4">No canonical findings.</td></tr>'
    split_brain_rows = _finding_rows(context.get("split_brain_items") or [], mode="findings")
    routing_rows = _finding_rows(context.get("routing_items") or [], mode="routing")
    split_brain_and_routing = split_brain_rows + routing_rows
    if not split_brain_and_routing:
        split_brain_and_routing = '<tr><td colspan="4">No split-brain or routing drift findings.</td></tr>'

    checkout_rows = "".join(
        f"""
        <tr>
          <td><strong>{_escape(item.get("component"))}</strong></td>
          <td><code>{_escape(item.get("branch"))}</code></td>
          <td>dirty={_escape(item.get("dirty"))}<br>stale_merged={_escape(item.get("stale_merged_branch"))}</td>
          <td>{_bullets(item.get("messages") or [])}</td>
        </tr>
        """
        for item in context.get("checkout_items") or []
    ) or '<tr><td colspan="4">No checkout drift findings.</td></tr>'

    mr_rows = "".join(
        f"""
        <tr>
          <td><strong>{_escape(item.get("ref"))}</strong><br><span class="muted">{_escape(item.get("title"))}</span></td>
          <td>{_escape(item.get("merge_status"))}</td>
          <td>{_escape(item.get("pipeline"))}</td>
          <td>{_escape(item.get("updated_at"))}</td>
        </tr>
        """
        for item in context.get("merge_requests") or []
    ) or '<tr><td colspan="4">No open main-targeted merge requests.</td></tr>'

    generated_at = context.get("generated_at")
    generated_line = (
        f'<p class="muted">Generated at <time datetime="{_escape(generated_at)}">{_escape(generated_at)}</time></p>'
        if generated_at
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>VeridataOps Dev Status</title>
    <style>
      :root {{
        color-scheme: dark;
        --bg: #0d1117;
        --panel: #161b22;
        --line: #30363d;
        --text: #e6edf3;
        --muted: #9da7b3;
        --ok-bg: #12261a;
        --ok-text: #7ee787;
        --warn-bg: #2b2111;
        --warn-text: #f2cc60;
        --danger-bg: #30191c;
        --danger-text: #ff9aa5;
        --accent: #58a6ff;
      }}
      * {{ box-sizing: border-box; }}
      body {{ margin: 0; font-family: Inter, system-ui, sans-serif; background: var(--bg); color: var(--text); }}
      main {{ max-width: 1440px; margin: 0 auto; padding: 24px; }}
      a {{ color: var(--accent); text-decoration: none; }}
      .hero {{ display: grid; gap: 16px; grid-template-columns: minmax(0, 1fr) 280px; align-items: start; margin-bottom: 20px; }}
      .eyebrow {{ margin: 0 0 8px; font-size: 0.8rem; font-weight: 700; text-transform: uppercase; color: var(--muted); }}
      h1 {{ margin: 0 0 10px; font-size: 2rem; }}
      h2 {{ margin: 0; font-size: 1.05rem; }}
      .muted {{ color: var(--muted); display: block; margin-top: 6px; line-height: 1.45; }}
      .state-card, .panel, .summary-card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
      .state-card {{ padding: 18px; display: grid; gap: 6px; }}
      .state-card span {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; font-weight: 700; }}
      .state-card strong {{ font-size: 1.35rem; }}
      .state-card small {{ color: var(--muted); }}
      .summary-grid {{ display: grid; gap: 12px; grid-template-columns: repeat(6, minmax(0, 1fr)); margin-bottom: 20px; }}
      .summary-card {{ padding: 14px; display: grid; gap: 6px; min-height: 88px; }}
      .summary-card span {{ color: var(--muted); font-size: 0.82rem; }}
      .summary-card strong {{ font-size: 1.4rem; }}
      .grid-2 {{ display: grid; gap: 16px; grid-template-columns: repeat(2, minmax(0, 1fr)); margin-bottom: 16px; }}
      .panel {{ padding: 18px; }}
      .panel.wide {{ margin-bottom: 16px; }}
      .section-heading {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; margin-bottom: 14px; }}
      .stack-row {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; padding: 10px 0; border-top: 1px solid var(--line); }}
      .stack-row:first-child {{ border-top: 0; padding-top: 0; }}
      .pill {{ display: inline-flex; align-items: center; min-height: 24px; padding: 2px 8px; border-radius: 999px; font-size: 0.75rem; font-weight: 700; white-space: nowrap; }}
      .pill.ok {{ background: var(--ok-bg); color: var(--ok-text); }}
      .pill.warn, .pill.warning {{ background: var(--warn-bg); color: var(--warn-text); }}
      .pill.danger {{ background: var(--danger-bg); color: var(--danger-text); }}
      .table-wrap {{ overflow-x: auto; }}
      table {{ width: 100%; border-collapse: collapse; }}
      th, td {{ padding: 10px 8px; border-top: 1px solid var(--line); vertical-align: top; text-align: left; }}
      thead th {{ border-top: 0; color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.02em; }}
      code {{ font-family: ui-monospace, SFMono-Regular, monospace; font-size: 0.82rem; }}
      ul {{ margin: 0; padding-left: 18px; }}
      .detail-card {{ padding-top: 10px; border-top: 1px solid var(--line); }}
      .detail-card:first-of-type {{ padding-top: 0; border-top: 0; }}
      summary {{ cursor: pointer; font-weight: 700; }}
      p {{ margin: 8px 0 0; }}
      @media (max-width: 1100px) {{
        .summary-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
        .hero, .grid-2 {{ grid-template-columns: minmax(0, 1fr); }}
      }}
      @media (max-width: 720px) {{
        main {{ padding: 16px; }}
        .summary-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      }}
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <div>
          <p class="eyebrow">Dev Health Status</p>
          <h1>Development Health and Release Status</h1>
          <p class="muted">{_escape(overall.get("detail", "Track canonical drift, split brain, branch drift, and release state in one place."))}</p>
          {generated_line}
        </div>
        <div class="state-card">
          <span>Status</span>
          <strong>{_escape(overall.get("label", "Healthy"))}</strong>
          <small>{_escape(release.get("aligned_count", 0))} / {_escape(len(release.get("items") or []))} release repos aligned</small>
        </div>
      </section>

      {load_warnings}

      <section class="summary-grid">{summary_cards}</section>

      <section class="grid-2">
        <section class="panel">
          <div class="section-heading">
            <div>
              <h2>Latest Release Validation</h2>
              <p class="muted">Most recent public release verification snapshot.</p>
            </div>
          </div>
          <div class="stack-row">
            <div>
              <strong>{_escape(validation.get("label", "Unavailable"))}</strong>
              <span class="muted">{_escape(validation.get("generated_at") or "No release validation artifact is available yet.")}</span>
            </div>
            <span class="pill {validation.get('tone', 'warn')}">{_escape(validation.get("label", "Unavailable"))}</span>
          </div>
          {_validation_target_row(validation)}
          {release_validation_steps}
          {release_images}
        </section>

        <section class="panel">
          <div class="section-heading">
            <div>
              <h2>Release Baseline</h2>
              <p class="muted">Prod tags must stay aligned with origin/main.</p>
            </div>
          </div>
          <div class="stack-row">
            <div>
              <strong>Aligned repos</strong>
              <span class="muted">{_escape(release.get("aligned_count", 0))} aligned, {_escape(release.get("drift_count", 0))} drifted</span>
            </div>
          </div>
          {release_stack}
        </section>
      </section>

      <section class="panel wide">
        <div class="section-heading">
          <div>
            <h2>Release Repo Tags</h2>
            <p class="muted">Current valid production tag baseline for each release repo.</p>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>Repo</th><th>Status</th><th>Valid Prod Tag</th><th>Latest Visible Tag</th><th>Main SHA</th><th>Drift</th></tr>
            </thead>
            <tbody>{release_table_rows}</tbody>
          </table>
        </div>
      </section>

      <section class="grid-2">
        <section class="panel">
          <div class="section-heading">
            <div>
              <h2>Canonical Findings</h2>
              <p class="muted">Active lanes missing canonical owner, stage, status, blocker, or next action.</p>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Item</th><th>Owner</th><th>Stage</th><th>Findings</th></tr></thead>
              <tbody>{canonical_rows}</tbody>
            </table>
          </div>
        </section>

        <section class="panel">
          <div class="section-heading">
            <div>
              <h2>Split Brain and Routing</h2>
              <p class="muted">Cross-plane disagreements and active items without exact next actions.</p>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Item</th><th>Owner</th><th>Stage</th><th>Issue</th></tr></thead>
              <tbody>{split_brain_and_routing}</tbody>
            </table>
          </div>
        </section>
      </section>

      <section class="grid-2">
        <section class="panel">
          <div class="section-heading">
            <div>
              <h2>Checkout Drift</h2>
              <p class="muted">Sibling repos that are off main, dirty, or stale-merged.</p>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Repo</th><th>Branch</th><th>State</th><th>Messages</th></tr></thead>
              <tbody>{checkout_rows}</tbody>
            </table>
          </div>
        </section>

        <section class="panel">
          <div class="section-heading">
            <div>
              <h2>Main-Targeted MRs</h2>
              <p class="muted">Outstanding merge work that still targets main.</p>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>MR</th><th>Status</th><th>Pipeline</th><th>Updated</th></tr></thead>
              <tbody>{mr_rows}</tbody>
            </table>
          </div>
        </section>
      </section>
    </main>
  </body>
</html>
"""


def _load_dev_health_report(*, force_refresh: bool, errors: list[str]) -> dict[str, Any] | None:
    report = None if force_refresh else _load_fresh_cached_json(DEV_HEALTH_CACHE_PATH, REPORT_REFRESH_SECONDS)
    if report is not None:
        return report
    try:
        _refresh_dev_health_report()
    except Exception as exc:
        errors.append(f"Unable to refresh dev health report: {exc}")
        fallback = _load_json_file(DEV_HEALTH_CACHE_PATH, errors=None, label="cached dev health report")
        if fallback is not None:
            return fallback
        return None
    report = _load_json_file(DEV_HEALTH_CACHE_PATH, errors=errors, label="dev health report")
    if report is None:
        errors.append("Dev health refresh completed without a readable JSON report.")
    return report


def _refresh_dev_health_report() -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(DEV_HEALTH_SCRIPT), "--json-report", str(DEV_HEALTH_CACHE_PATH)],
        cwd=str(VERIDATAOPS_ROOT),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=25,
    )
    if proc.returncode == 0:
        return
    detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
    raise RuntimeError(detail)


def _load_fresh_cached_json(path: Path, max_age_seconds: int) -> dict[str, Any] | None:
    if not path.exists():
        return None
    age_seconds = time.time() - path.stat().st_mtime
    if age_seconds > max_age_seconds:
        return None
    return _load_json_file(path, errors=None, label=path.name)


def _load_json_file(path: Path, *, errors: list[str] | None, label: str) -> dict[str, Any] | None:
    if not path.exists():
        if errors is not None:
            errors.append(f"Missing {label} at {path}.")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        if errors is not None:
            errors.append(f"Unable to read {label}: {exc}")
        return None


def _summary_cards(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    report = report or {}
    return [
        {"label": "Canonical Findings", "value": ((report.get("canonical_check") or {}).get("count") or 0)},
        {"label": "Split Brain", "value": ((report.get("split_brain") or {}).get("count") or 0)},
        {"label": "Routing Drift", "value": ((report.get("routing_drift") or {}).get("missing_next_action_count") or 0)},
        {"label": "Release Drift", "value": ((report.get("release_tag_drift") or {}).get("count") or 0)},
        {"label": "Checkout Drift", "value": ((report.get("checkout_branch_drift") or {}).get("count") or 0)},
        {"label": "Open Main MRs", "value": ((report.get("merge_requests") or {}).get("opened_main_target_count") or 0)},
    ]


def _release_context(report: dict[str, Any] | None) -> dict[str, Any]:
    items = []
    aligned_count = 0
    for item in (((report or {}).get("release_tag_drift") or {}).get("items") or []):
        latest_valid = item.get("latest_valid_tag")
        latest_any = item.get("latest_any_tag")
        main_ahead = int(item.get("main_ahead_of_valid_tag") or 0)
        tag_ahead = int(item.get("valid_tag_ahead_of_main") or 0)
        if not latest_valid:
            status = {"label": "No valid prod tag", "tone": "danger"}
        elif latest_any != latest_valid:
            status = {"label": "Off-main newer tag", "tone": "danger"}
        elif tag_ahead:
            status = {"label": "Prod tag ahead of main", "tone": "danger"}
        elif main_ahead:
            status = {"label": "Main ahead of prod tag", "tone": "warn"}
        else:
            status = {"label": "Aligned", "tone": "ok"}
            aligned_count += 1
        items.append(
            {
                "component": item.get("component"),
                "latest_valid_tag": latest_valid,
                "latest_any_tag": latest_any,
                "main_sha": item.get("main_sha"),
                "tag_sha": item.get("tag_sha"),
                "main_ahead_of_valid_tag": main_ahead,
                "valid_tag_ahead_of_main": tag_ahead,
                "status": status,
            }
        )
    return {"items": items, "aligned_count": aligned_count, "drift_count": max(len(items) - aligned_count, 0)}


def _release_validation_context(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {
            "available": False,
            "label": "Unavailable",
            "tone": "warn",
            "generated_at": None,
            "release_tag": None,
            "base_url": None,
            "steps": [],
            "images": [],
            "pressure_points": [],
        }
    steps = []
    for step in report.get("steps", []) if isinstance(report.get("steps"), list) else []:
        status = str(step.get("status") or "unknown").strip().lower()
        if status in {"pass", "passed", "success"}:
            tone = "ok"
        elif status in {"fail", "failed", "error"}:
            tone = "danger"
        else:
            tone = "warn"
        steps.append(
            {
                "name": step.get("name"),
                "status": step.get("status"),
                "tone": tone,
                "duration_seconds": step.get("duration_seconds"),
                "detail": step.get("detail"),
            }
        )
    images = [{"name": name.replace("_", " "), "tag": tag} for name, tag in (report.get("images") or {}).items() if tag]
    passed = bool(report.get("passed"))
    return {
        "available": True,
        "label": "Passed" if passed else "Failed",
        "tone": "ok" if passed else "danger",
        "generated_at": report.get("generated_at"),
        "release_tag": report.get("release_tag") or None,
        "base_url": report.get("base_url"),
        "steps": steps,
        "images": images,
        "pressure_points": report.get("pressure_points") or [],
    }


def _overall_state(health_report: dict[str, Any] | None, release_report: dict[str, Any] | None) -> dict[str, str]:
    split_brain_count = ((health_report or {}).get("split_brain") or {}).get("count") or 0
    release_drift_count = ((health_report or {}).get("release_tag_drift") or {}).get("count") or 0
    canonical_count = ((health_report or {}).get("canonical_check") or {}).get("count") or 0
    routing_count = ((health_report or {}).get("routing_drift") or {}).get("missing_next_action_count") or 0
    branch_count = ((health_report or {}).get("checkout_branch_drift") or {}).get("count") or 0
    mr_count = ((health_report or {}).get("merge_requests") or {}).get("opened_main_target_count") or 0
    release_failed = bool(release_report) and not bool(release_report.get("passed"))
    critical_reasons = []
    if split_brain_count:
        critical_reasons.append(f"{split_brain_count} split-brain item(s)")
    if release_drift_count:
        critical_reasons.append(f"{release_drift_count} release-tag drift item(s)")
    if release_failed:
        critical_reasons.append("latest release validation failed")
    warning_reasons = []
    if canonical_count:
        warning_reasons.append(f"{canonical_count} canonical finding(s)")
    if routing_count:
        warning_reasons.append(f"{routing_count} routing drift item(s)")
    if branch_count:
        warning_reasons.append(f"{branch_count} drifted checkout(s)")
    if mr_count:
        warning_reasons.append(f"{mr_count} open main-targeted MR(s)")
    if critical_reasons:
        return {"label": "Action Required", "tone": "danger", "detail": "; ".join(critical_reasons + warning_reasons[:2])}
    if warning_reasons:
        return {"label": "Needs Attention", "tone": "warning", "detail": "; ".join(warning_reasons)}
    return {"label": "Healthy", "tone": "ok", "detail": "Dev health and release baselines are aligned."}


def _finding_rows(items: list[dict[str, Any]], *, mode: str) -> str:
    rows = []
    for item in items:
        ref = _escape(item.get("ref"))
        owner = _escape(item.get("owner") or "-")
        stage = _escape(item.get("stage") or "-")
        if mode == "findings":
            issue = _bullets(item.get("findings") or [])
        else:
            issue = "Missing canonical next action."
        rows.append(f"<tr><td><code>{ref}</code></td><td>{owner}</td><td>{stage}</td><td>{issue}</td></tr>")
    return "".join(rows)


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _bullets(items: list[Any]) -> str:
    if not items:
        return "-"
    return "<ul>" + "".join(f"<li>{_escape(item)}</li>" for item in items) + "</ul>"


def _paragraph(text: Any) -> str:
    if not text:
        return ""
    return f'<p class="muted">{_escape(text)}</p>'


def _detail_row(label: str, value: Any) -> str:
    if value in {None, ""}:
        return ""
    return f'<p class="muted">{_escape(label)}: {_escape(value)}</p>'


def _validation_target_row(validation: dict[str, Any]) -> str:
    if not validation.get("base_url"):
        return ""
    release_tag = validation.get("release_tag")
    pill = f'<span class="pill warn">{_escape(release_tag)}</span>' if release_tag else ""
    return f"""
    <div class="stack-row">
      <div>
        <strong>Target</strong>
        <span class="muted">{_escape(validation.get("base_url"))}</span>
      </div>
      {pill}
    </div>
    """


def _latest_visible_suffix(item: dict[str, Any]) -> str:
    latest_any = item.get("latest_any_tag")
    latest_valid = item.get("latest_valid_tag")
    if latest_any and latest_any != latest_valid:
        return f"; latest={_escape(latest_any)}"
    return ""
