from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Protocol

from codex_web.identity import TenantScope
from codex_web.skill_security import (
    SkillFindingSeverity,
    SkillScanMode,
    SkillScanProviderResult,
    SkillSecurityFinding,
)


class SkillScannerError(RuntimeError):
    pass


class SkillScannerProvider(Protocol):
    provider_id: str
    def metadata(self) -> dict[str, object]: ...
    def scan(self, *, content: str, mode: SkillScanMode) -> SkillScanProviderResult: ...


class SkillScannerRegistry:
    def __init__(self) -> None:
        self._providers: dict[tuple[str, str, str], SkillScannerProvider] = {}

    @staticmethod
    def _key(provider_id: str, tenant_scope: TenantScope | None) -> tuple[str, str, str]:
        normalized = str(provider_id).strip().casefold()
        if not normalized:
            raise ValueError("skill scanner provider id is required")
        if tenant_scope is None:
            return ("", "", normalized)
        return (tenant_scope.organization_id, tenant_scope.workspace_id, normalized)

    def register(self, provider: SkillScannerProvider, *, tenant_scope: TenantScope | None = None) -> None:
        key = self._key(provider.provider_id, tenant_scope)
        if key in self._providers and self._providers[key] is not provider:
            raise ValueError("skill scanner provider is already registered")
        self._providers[key] = provider

    def unregister(self, provider_id: str, *, tenant_scope: TenantScope | None = None) -> None:
        self._providers.pop(self._key(provider_id, tenant_scope), None)

    def get(self, provider_id: str, *, tenant_scope: TenantScope | None = None) -> SkillScannerProvider:
        if tenant_scope is not None:
            provider = self._providers.get(self._key(provider_id, tenant_scope))
            if provider is not None:
                return provider
        provider = self._providers.get(self._key(provider_id, None))
        if provider is None:
            raise SkillScannerError("skill scanner provider is unavailable")
        return provider

    def list(self, *, tenant_scope: TenantScope | None = None) -> list[dict[str, object]]:
        providers = {
            key[2]: value
            for key, value in self._providers.items()
            if key[0] == "" and key[1] == ""
        }
        if tenant_scope is not None:
            providers.update(
                {
                    key[2]: value
                    for key, value in self._providers.items()
                    if key[:2] == (tenant_scope.organization_id, tenant_scope.workspace_id)
                }
            )
        return [providers[key].metadata() for key in sorted(providers)]


class SkillSpectorJsonParser:
    MAX_FINDINGS = 500

    @staticmethod
    def _severity(value: object) -> SkillFindingSeverity:
        normalized = str(value or "info").strip().casefold()
        aliases = {"moderate": "medium", "warn": "medium", "error": "high"}
        try:
            return SkillFindingSeverity(aliases.get(normalized, normalized))
        except ValueError:
            return SkillFindingSeverity.INFO

    def parse(self, payload: str | bytes | dict[str, object]) -> SkillScanProviderResult:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="strict")
        if isinstance(payload, str):
            if len(payload) > 2_000_000:
                raise SkillScannerError("SkillSpector report exceeds the bounded output limit")
            try:
                value = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise SkillScannerError("SkillSpector returned invalid JSON") from exc
        else:
            value = payload
        if not isinstance(value, dict):
            raise SkillScannerError("SkillSpector report must be a JSON object")
        raw_findings = value.get("findings") or value.get("results") or []
        if not isinstance(raw_findings, list):
            raise SkillScannerError("SkillSpector findings must be a JSON array")
        findings = []
        for index, raw in enumerate(raw_findings[: self.MAX_FINDINGS]):
            if not isinstance(raw, dict):
                continue
            findings.append(
                SkillSecurityFinding(
                    rule_id=str(raw.get("rule_id") or raw.get("ruleId") or raw.get("id") or f"finding-{index}"),
                    severity=self._severity(raw.get("severity")),
                    category=str(raw.get("category") or raw.get("type") or "general"),
                    message=str(raw.get("message") or raw.get("description") or "SkillSpector finding"),
                    path=(str(raw.get("path") or raw.get("file")) if raw.get("path") or raw.get("file") else None),
                    line=(int(raw["line"]) if raw.get("line") is not None else None),
                    fingerprint=(str(raw.get("fingerprint")) if raw.get("fingerprint") else None),
                )
            )
        score_value = value.get("risk_score", value.get("riskScore", value.get("score", 0)))
        try:
            score = max(0.0, min(100.0, float(score_value or 0)))
        except (TypeError, ValueError) as exc:
            raise SkillScannerError("SkillSpector risk score is invalid") from exc
        return SkillScanProviderResult(
            scanner_version=str(value.get("scanner_version") or value.get("version") or "unknown"),
            risk_score=score,
            findings=tuple(findings),
            report_artifact_id=(str(value.get("report_artifact_id")) if value.get("report_artifact_id") else None),
        )


class SkillSpectorCliProvider:
    """Bounded adapter loaded only by a trusted isolated worker runtime."""

    provider_id = "nvidia-skillspector"

    def __init__(
        self,
        *,
        executable: str = "skillspector",
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 120.0,
        parser: SkillSpectorJsonParser | None = None,
    ) -> None:
        self.executable = executable
        self.run = run
        self.timeout_seconds = min(max(float(timeout_seconds), 1.0), 300.0)
        self.parser = parser or SkillSpectorJsonParser()

    def metadata(self) -> dict[str, object]:
        return {
            "providerId": self.provider_id,
            "transport": "isolated_worker_cli",
            "modes": [item.value for item in SkillScanMode],
            "structuredOutput": "json",
            "llmRequired": False,
        }

    def scan(self, *, content: str, mode: SkillScanMode) -> SkillScanProviderResult:
        if len(content) > 512_000:
            raise SkillScannerError("Skill content exceeds the scanner input limit")
        with tempfile.TemporaryDirectory(prefix="codex-web-skill-scan-") as directory:
            path = Path(directory) / "SKILL.md"
            path.write_text(content, encoding="utf-8")
            command = [self.executable, "scan", str(path), "--format", "json"]
            if mode == SkillScanMode.STATIC:
                command.append("--static-only")
            try:
                result = self.run(
                    command,
                    cwd=directory,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise SkillScannerError(f"SkillSpector execution failed: {type(exc).__name__}") from exc
            if len(result.stdout) > 2_000_000 or len(result.stderr) > 64_000:
                raise SkillScannerError("SkillSpector output exceeds the bounded output limit")
            if result.returncode not in {0, 1}:
                raise SkillScannerError(f"SkillSpector exited with status {result.returncode}")
            return self.parser.parse(result.stdout)
