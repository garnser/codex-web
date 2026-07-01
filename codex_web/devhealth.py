from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Any


def build_context(
    health: dict[str, Any],
    *,
    active_turns: int = 0,
    queued_turns: int = 0,
    status_context: dict[str, Any] | None = None,
    work_item_stats: dict[str, Any] | None = None,
    refresh_url: str = "/devhealth?refresh=1",
) -> dict[str, Any]:
    status_context = status_context or {}
    work_item_stats = work_item_stats or {}
    problems = [str(item) for item in health.get("problems") or []]
    runtime_status = list(health.get("runtimeStatus") or [])
    error_count = sum(1 for item in runtime_status if str(item.get("status") or "").lower() == "error")
    active_count = sum(1 for item in runtime_status if str(item.get("status") or "").lower() in {"connected", "running", "ok"})
    canonical_items = list(status_context.get("canonical_items") or [])
    split_brain_items = list(status_context.get("split_brain_items") or [])
    release = status_context.get("release") or {}
    release_items = list(release.get("items") or [])
    release_drift = int(release.get("drift_count") or 0)
    release_aligned = int(release.get("aligned_count") or 0)
    overall_ok = bool(health.get("ok"))
    overall_label = "Healthy" if overall_ok else "Needs Attention"
    overall_tone = "ok" if overall_ok else "warning"
    overall_detail = (
        "Codex daemon and runtime connections look healthy."
        if overall_ok
        else f"{len(problems)} problem(s) detected across daemon and runtime connections."
    )
    generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    return {
        "generated_at": generated_at,
        "overall": {
            "label": overall_label,
            "tone": overall_tone,
            "detail": overall_detail,
        },
        "summary": [
            {"label": "Codex Ready", "value": "Yes" if health.get("codexReady") else "No", "tone": "ok" if health.get("codexReady") else "warning"},
            {"label": "Active Turns", "value": int(active_turns), "tone": "neutral"},
            {"label": "Queued Turns", "value": int(queued_turns), "tone": "warning" if queued_turns else "ok"},
            {"label": "Canonical Findings", "value": len(canonical_items), "tone": "warning" if canonical_items else "ok"},
            {"label": "Split Brain", "value": len(split_brain_items), "tone": "warning" if split_brain_items else "ok"},
            {"label": "Prod Cut Drift", "value": release_drift, "tone": "warning" if release_drift else "ok"},
            {"label": "Runtime Connections", "value": int(health.get("runtimeConnections") or 0), "tone": "neutral"},
            {"label": "Runtime Errors", "value": error_count, "tone": "warning" if error_count else "ok"},
            {"label": "Open Problems", "value": len(problems), "tone": "warning" if problems else "ok"},
        ],
        "daemon": {
            "ok": overall_ok,
            "codex_ready": bool(health.get("codexReady")),
            "codex_pid": health.get("codexPid"),
            "active_turns": int(active_turns),
            "queued_turns": int(queued_turns),
            "runtime_connections": int(health.get("runtimeConnections") or 0),
            "active_connections": active_count,
        },
        "work_item_stats": [
            {"label": "Open Work Items", "value": int(work_item_stats.get("open_count") or 0), "tone": "neutral"},
            {"label": "Blocked", "value": int(work_item_stats.get("blocked_count") or 0), "tone": "warning" if int(work_item_stats.get("blocked_count") or 0) else "ok"},
            {"label": "Pending Handoffs", "value": int(work_item_stats.get("pending_handoff_count") or 0), "tone": "warning" if int(work_item_stats.get("pending_handoff_count") or 0) else "ok"},
            {"label": "Release Gate", "value": int(work_item_stats.get("release_gate_count") or 0), "tone": "warning" if int(work_item_stats.get("release_gate_count") or 0) else "ok"},
            {"label": "Ready for Validation", "value": int(work_item_stats.get("ready_for_validation_count") or 0), "tone": "neutral"},
            {"label": "Implementation Active", "value": int(work_item_stats.get("implementation_active_count") or 0), "tone": "neutral"},
        ],
        "divergence": {
            "canonical_items": canonical_items,
            "split_brain_items": split_brain_items,
        },
        "release": {
            "items": release_items,
            "aligned_count": release_aligned,
            "drift_count": release_drift,
        },
        "problems": problems,
        "refresh_url": refresh_url,
        "runtime_status": runtime_status,
    }


def _badge(label: str, tone: str) -> str:
    return f'<span class="badge {escape(tone)}">{escape(label)}</span>'


def _runtime_connection_label(item: dict[str, Any], index: int) -> str:
    for key in ("connectionId", "connection_id", "botId", "bot_id", "id"):
        value = item.get(key)
        if value:
            return str(value)
    return f"runtime-{index}"


def _runtime_provider_label(item: dict[str, Any]) -> str:
    for key in ("provider", "botProvider", "bot_provider"):
        value = item.get(key)
        if value:
            return str(value)
    return "unknown"


def _runtime_updated_label(item: dict[str, Any]) -> str:
    for key in ("updatedAt", "lastErrorAt", "startedAt"):
        value = item.get(key)
        if value:
            return str(value)
    return "n/a"


def _finding_bullets(items: list[str]) -> str:
    if not items:
        return ""
    return "<ul class=\"problem-list\">" + "".join(f"<li>{escape(str(item))}</li>" for item in items) + "</ul>"


def render_html(context: dict[str, Any]) -> str:
    overall = context["overall"]
    refresh_url = str(context.get("refresh_url") or "/devhealth?refresh=1")
    summary_cards = "".join(
        f"""
        <article class="card">
          <div class="card-label">{escape(str(item["label"]))}</div>
          <div class="card-value">{escape(str(item["value"]))}</div>
          <div class="card-tone">{_badge(str(item["tone"]).replace("_", " ").title(), str(item["tone"]))}</div>
        </article>
        """
        for item in context["summary"]
    )
    problems = context["problems"]
    if problems:
        problems_html = "<ul class=\"problem-list\">" + "".join(
            f"<li>{escape(problem)}</li>" for problem in problems
        ) + "</ul>"
    else:
        problems_html = '<p class="empty-state">No active daemon or runtime problems were reported.</p>'

    runtime_rows = []
    for index, item in enumerate(context["runtime_status"], start=1):
        status_label = str(item.get("status") or "unknown").replace("_", " ")
        tone = "warning" if status_label.lower() == "error" else "ok" if status_label.lower() in {"connected", "running", "ok"} else "neutral"
        runtime_rows.append(
            f"""
            <tr>
              <td>{escape(_runtime_connection_label(item, index))}</td>
              <td>{escape(_runtime_provider_label(item))}</td>
              <td>{_badge(status_label.title(), tone)}</td>
              <td>{escape(_runtime_updated_label(item))}</td>
            </tr>
            """
        )
    runtime_table = (
        """
        <table>
          <thead>
            <tr>
              <th>Connection</th>
              <th>Provider</th>
              <th>Status</th>
              <th>Last Update</th>
            </tr>
          </thead>
          <tbody>
        """
        + "".join(runtime_rows)
        + """
          </tbody>
        </table>
        """
        if runtime_rows
        else '<p class="empty-state">No runtime connections are currently registered.</p>'
    )

    daemon = context["daemon"]
    divergence = context["divergence"]
    release = context["release"]
    work_item_stats = context["work_item_stats"]
    divergence_rows = []
    for item in divergence["canonical_items"]:
        divergence_rows.append(
            f"""
            <tr>
              <td>{escape(str(item.get("ref") or "-"))}</td>
              <td>{escape(str(item.get("owner") or "-"))}</td>
              <td>{escape(str(item.get("stage") or "-"))}</td>
              <td>{_finding_bullets([str(entry) for entry in item.get("findings") or []]) or '-'}</td>
            </tr>
            """
        )
    for item in divergence["split_brain_items"]:
        divergence_rows.append(
            f"""
            <tr>
              <td>{escape(str(item.get("ref") or "-"))}</td>
              <td>{escape(str(item.get("owner") or "-"))}</td>
              <td>{escape(str(item.get("stage") or "-"))}</td>
              <td>{_finding_bullets([str(entry) for entry in item.get("findings") or []]) or '-'}</td>
            </tr>
            """
        )
    divergence_table = (
        """
        <table>
          <thead>
            <tr>
              <th>Ref</th>
              <th>Owner</th>
              <th>Stage</th>
              <th>Findings</th>
            </tr>
          </thead>
          <tbody>
        """
        + "".join(divergence_rows)
        + """
          </tbody>
        </table>
        """
        if divergence_rows
        else '<p class="empty-state">No canonical or split-brain findings are currently open.</p>'
    )
    release_rows = []
    for item in release["items"]:
        status = item.get("status") or {}
        release_rows.append(
            f"""
            <tr>
              <td>{escape(str(item.get("component") or "-"))}</td>
              <td>{_badge(str(status.get("label") or "Unknown"), str(status.get("tone") or "neutral"))}</td>
              <td><code>{escape(str(item.get("latest_valid_tag") or "none"))}</code></td>
              <td><code>{escape(str(item.get("latest_any_tag") or "none"))}</code></td>
              <td><code>{escape(str(item.get("main_sha") or "unknown"))}</code></td>
            </tr>
            """
        )
    release_table = (
        """
        <table>
          <thead>
            <tr>
              <th>Component</th>
              <th>Status</th>
              <th>Current Prod Cut</th>
              <th>Latest Seen Tag</th>
              <th>Main SHA</th>
            </tr>
          </thead>
          <tbody>
        """
        + "".join(release_rows)
        + """
          </tbody>
        </table>
        """
        if release_rows
        else '<p class="empty-state">No release prod-cut data is currently available.</p>'
    )
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>VeridataOps Dev Health</title>
    <style>
      :root {{
        color-scheme: dark;
        --bg: #0b1220;
        --panel: #111a2b;
        --panel-border: #22314d;
        --text: #e5edf7;
        --muted: #93a4bd;
        --ok: #1f9d67;
        --warning: #d2a53a;
        --neutral: #4f6b95;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: linear-gradient(180deg, #0b1220 0%, #0f172a 100%);
        color: var(--text);
      }}
      main {{
        max-width: 1200px;
        margin: 0 auto;
        padding: 32px 20px 48px;
      }}
      .hero {{
        display: grid;
        gap: 12px;
        margin-bottom: 24px;
      }}
      .hero-head {{
        display: flex;
        justify-content: space-between;
        gap: 16px;
        align-items: flex-start;
      }}
      h1, h2 {{ margin: 0; }}
      h1 {{ font-size: 2rem; }}
      h2 {{ font-size: 1.1rem; }}
      .meta, .subtle {{ color: var(--muted); }}
      .refresh-link {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 40px;
        padding: 0 14px;
        border-radius: 8px;
        border: 1px solid rgba(79, 107, 149, 0.5);
        color: var(--text);
        background: rgba(79, 107, 149, 0.15);
        font-weight: 600;
      }}
      .status-line {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
      }}
      .badge {{
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        padding: 0.25rem 0.65rem;
        font-size: 0.8rem;
        font-weight: 600;
        border: 1px solid transparent;
      }}
      .badge.ok {{ background: rgba(31, 157, 103, 0.16); color: #8ee2bc; border-color: rgba(31, 157, 103, 0.3); }}
      .badge.warning {{ background: rgba(210, 165, 58, 0.16); color: #f5d488; border-color: rgba(210, 165, 58, 0.3); }}
      .badge.neutral {{ background: rgba(79, 107, 149, 0.18); color: #a8bedf; border-color: rgba(79, 107, 149, 0.35); }}
      .grid {{
        display: grid;
        gap: 16px;
      }}
      .summary-grid {{
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        margin-bottom: 24px;
      }}
      .card, .panel {{
        background: rgba(17, 26, 43, 0.92);
        border: 1px solid var(--panel-border);
        border-radius: 8px;
      }}
      .card {{
        padding: 16px;
      }}
      .card-label {{
        color: var(--muted);
        font-size: 0.82rem;
        margin-bottom: 8px;
      }}
      .card-value {{
        font-size: 1.8rem;
        font-weight: 700;
        margin-bottom: 10px;
      }}
      .panel {{
        padding: 18px;
      }}
      .panel-grid {{
        grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
        margin-bottom: 16px;
      }}
      .stat-grid {{
        display: grid;
        gap: 12px;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        margin-top: 14px;
      }}
      .stat-tile {{
        padding: 14px;
        border: 1px solid rgba(147, 164, 189, 0.14);
        border-radius: 8px;
        background: rgba(11, 18, 32, 0.28);
      }}
      .stat-tile-label {{
        color: var(--muted);
        font-size: 0.8rem;
        margin-bottom: 8px;
      }}
      .stat-tile-value {{
        font-size: 1.55rem;
        font-weight: 700;
        margin-bottom: 10px;
      }}
      .kv {{
        display: grid;
        grid-template-columns: minmax(140px, 180px) 1fr;
        gap: 8px 12px;
        margin-top: 14px;
      }}
      .kv dt {{
        color: var(--muted);
      }}
      .kv dd {{
        margin: 0;
      }}
      .problem-list {{
        margin: 14px 0 0;
        padding-left: 1.1rem;
      }}
      .problem-list li + li {{
        margin-top: 8px;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        margin-top: 14px;
      }}
      th, td {{
        text-align: left;
        padding: 10px 12px;
        border-top: 1px solid rgba(147, 164, 189, 0.18);
        vertical-align: top;
      }}
      thead th {{
        color: var(--muted);
        font-weight: 600;
        border-top: none;
        padding-top: 0;
      }}
      .empty-state {{
        color: var(--muted);
        margin: 14px 0 0;
      }}
      @media (max-width: 700px) {{
        main {{ padding-inline: 14px; }}
        .hero-head {{ flex-direction: column; }}
        .kv {{ grid-template-columns: 1fr; }}
      }}
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <div class="hero-head">
          <div class="status-line">
            <h1>VeridataOps Dev Health</h1>
            {_badge(str(overall["label"]), str(overall["tone"]))}
          </div>
          <a class="refresh-link" href="{escape(refresh_url)}">Live refresh</a>
        </div>
        <div class="subtle">{escape(str(overall["detail"]))}</div>
        <div class="meta">Rendered at {escape(str(context["generated_at"]))}</div>
      </section>

      <section class="grid summary-grid">
        {summary_cards}
      </section>

      <section class="grid panel-grid">
        <article class="panel">
          <h2>Daemon Status</h2>
          <dl class="kv">
            <dt>Health State</dt>
            <dd>{_badge("Healthy" if daemon["ok"] else "Degraded", "ok" if daemon["ok"] else "warning")}</dd>
            <dt>Codex Ready</dt>
            <dd>{_badge("Ready" if daemon["codex_ready"] else "Not Ready", "ok" if daemon["codex_ready"] else "warning")}</dd>
            <dt>Codex PID</dt>
            <dd>{escape(str(daemon["codex_pid"] or "n/a"))}</dd>
            <dt>Active Turns</dt>
            <dd>{escape(str(daemon["active_turns"]))}</dd>
            <dt>Queued Turns</dt>
            <dd>{escape(str(daemon["queued_turns"]))}</dd>
            <dt>Runtime Connections</dt>
            <dd>{escape(str(daemon["runtime_connections"]))}</dd>
            <dt>Active Connections</dt>
            <dd>{escape(str(daemon["active_connections"]))}</dd>
          </dl>
        </article>

        <article class="panel">
          <h2>Problems</h2>
          {problems_html}
        </article>
      </section>

      <section class="panel">
        <h2>Work Item Stats</h2>
        <div class="subtle">Top-line counts from canonical work-item state, alongside the existing queue and drift views.</div>
        <div class="stat-grid">
          {"".join(
              f'''
              <article class="stat-tile">
                <div class="stat-tile-label">{escape(str(item["label"]))}</div>
                <div class="stat-tile-value">{escape(str(item["value"]))}</div>
                <div>{_badge(str(item["tone"]).replace("_", " ").title(), str(item["tone"]))}</div>
              </article>
              '''
              for item in work_item_stats
          )}
        </div>
      </section>

      <section class="panel">
        <h2>Runtime Connections</h2>
        {runtime_table}
      </section>

      <section class="grid panel-grid" style="margin-top: 16px;">
        <article class="panel">
          <h2>Divergence and Split Brain</h2>
          <div class="subtle">Canonical drift and split-brain findings from the existing devstatus checks.</div>
          {divergence_table}
        </article>

        <article class="panel">
          <h2>Current Release Prod Cuts</h2>
          <div class="subtle">{escape(str(release["aligned_count"]))} aligned; {escape(str(release["drift_count"]))} drifted.</div>
          {release_table}
        </article>
      </section>
    </main>
  </body>
</html>
"""
