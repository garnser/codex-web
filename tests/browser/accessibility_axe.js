const BLOCKING_IMPACTS = new Set(["serious", "critical"]);

function blockingViolations(results) {
  return results.violations
    .filter((violation) => BLOCKING_IMPACTS.has(violation.impact))
    .map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      help: violation.help,
      helpUrl: violation.helpUrl,
      nodes: violation.nodes.map((node) => ({
        target: node.target,
        failureSummary: node.failureSummary,
        html: node.html,
      })),
    }));
}

async function expectNoBlockingViolations(expect, page, {
  surface,
  state,
  include = "body",
}) {
  const AxeBuilder = require("@axe-core/playwright").default;
  const results = await new AxeBuilder({ page }).include(include).analyze();
  const violations = blockingViolations(results);
  const diagnostic = [
    `axe accessibility gate failed for ${surface} (${state}).`,
    "Only serious and critical violations block CI; each node lists its DOM target and failure summary.",
    JSON.stringify(violations, null, 2),
  ].join("\n");
  expect(violations, diagnostic).toEqual([]);
}

module.exports = { blockingViolations, expectNoBlockingViolations };
