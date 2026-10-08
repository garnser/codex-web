# Contributing to codex-web

Thank you for contributing to codex-web. Before starting substantial work,
read [AGENTS.md](AGENTS.md) and check the canonical GitHub issues, milestones,
and project metadata for the relevant scope and dependencies.

## License and Developer Certificate of Origin

codex-web is licensed under the [Apache License 2.0](LICENSE). Contributions
submitted for inclusion in the project are provided under the same license.

This project uses the [Developer Certificate of Origin 1.1](https://developercertificate.org/).
By adding a `Signed-off-by` line to each commit, you certify that you have the
right to submit the contribution under the project's license and agree to the
Developer Certificate of Origin.

Sign a commit with Git's `-s` option:

```bash
git commit -s -m "Describe the change"
```

The resulting commit message must include a line matching the contributor's
real name and an email address they control:

```text
Signed-off-by: Contributor Name <contributor@example.com>
```

If a pull request contains unsigned commits, amend or rebase those commits and
force-push the corrected branch. Do not add another person's sign-off.

## Development and delivery

- Keep each change scoped to its GitHub issue and acceptance criteria.
- Preserve the architecture and safety invariants in [AGENTS.md](AGENTS.md).
- Add focused tests for behavioral changes.
- Include the applicable validation evidence in the pull request.
- Identify UI impact, or explain why no UI adaptation is required.
- Do not close an issue until its implementation is merged and the required CI
  and documentation are current.

Use GitHub issues for bugs, feature requests, and independently deliverable
follow-up work. Security-sensitive reports should not be posted publicly; use
the repository's private security-reporting channel when one is available.
