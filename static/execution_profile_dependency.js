// Project dependency-egress presentation and editor fields; canonical policy remains server-owned.
export const dependencyFields = `<fieldset><legend>Public dependency downloads</legend>
          <label><input name="profile-dependency-egress" type="checkbox"> Allow approved public HTTPS dependency hosts</label>
          <label>Exact hostnames (one per line; HTTPS port 443 only)<textarea name="profile-dependency-hosts" rows="3"></textarea></label>
          <p>Project schema 1.4; explicit publication and fresh assignment required. Requires Codex with mutable repository access and danger-full-access inside the private worker namespace. OS network, host mutation, container sockets and direct DNS remain disabled. The separate download proxy is available to managed validation commands. Existing procedures requiring a no-egress seal remain ineligible.</p>
        </fieldset>`;

export function dependencySummary(profile, escapeHtml) {
  return profile.dependencyEgress
    ? ` Approved HTTPS dependency hosts: ${profile.dependencyEgress.allowed_hosts.map(escapeHtml).join(", ")}. Private OS network; no host/container socket access. No-egress procedures are unavailable.`
    : " Dependency egress disabled.";
}

export function dependencyEditor(field) {
  const editor = {
    constrain() {
      const scratch = field('profile-workspace').value === 'scratch';
      if (scratch) field('profile-access').value = 'none';
      else if (field('profile-access').value === 'none') field('profile-access').value = 'read-only';
      field('profile-access').disabled = scratch;
      field('cap-git').checked = !scratch;
      if (scratch) field('profile-dependency-egress').checked = false;
      field('profile-dependency-egress').disabled = scratch;
      const dependency = field('profile-dependency-egress').checked;
      if (dependency) field('profile-access').value = 'mutable';
      field('profile-access').disabled = scratch || dependency;
      field('profile-dependency-hosts').disabled = scratch || !dependency;
    },
    fill(profile) {
      field('profile-dependency-egress').checked = Boolean(profile.dependency_egress);
      field('profile-dependency-hosts').value = (profile.dependency_egress?.allowed_hosts || []).join('\n');
    },
    capture() {
      const enabled = field('profile-dependency-egress').checked;
      const allowedHosts = field('profile-dependency-hosts').value.split('\n')
        .map(host => host.trim()).filter(Boolean);
      return {
        envelope: enabled ? { dependency_egress: { allowed_hosts: allowedHosts } } : {},
        capabilities: enabled ? ['network'] : [],
      };
    },
  };
  field('profile-workspace').addEventListener('change', editor.constrain);
  field('profile-dependency-egress').addEventListener('change', editor.constrain);
  return editor;
}
