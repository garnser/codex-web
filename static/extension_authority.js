export function canMutateExtensions(actor) {
  if (!actor) return false;
  if (actor.principal_kind === "service") {
    return (actor.service_scopes || []).includes("extensions:admin");
  }
  return ["mfa", "local_trusted"].includes(actor.assurance)
    && (actor.roles || []).some((role) => ["owner", "admin"].includes(role));
}

export function extensionMutationAuthorityText(actor) {
  if (!actor) return "Extension mutation authority is unavailable.";
  if (canMutateExtensions(actor)) {
    return `Extension administration allowed for ${actor.identity_id} · ${actor.principal_kind} · assurance ${actor.assurance}.`;
  }
  if (actor.principal_kind === "service") {
    return `Extension administration blocked for ${actor.identity_id}: extensions:admin service scope required.`;
  }
  return `Extension administration blocked for ${actor.identity_id}: tenant admin/owner plus MFA or local-trusted assurance required; current assurance ${actor.assurance}.`;
}
