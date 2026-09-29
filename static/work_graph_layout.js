export function layout(nodes, edges) {
  const refs = new Set(nodes.map((node) => node.ref));
  const incoming = new Map(nodes.map((node) => [node.ref, 0]));
  const outgoing = new Map(nodes.map((node) => [node.ref, []]));
  for (const edge of edges) {
    if (!refs.has(edge.source_ref) || !refs.has(edge.target_ref)) continue;
    incoming.set(edge.target_ref, (incoming.get(edge.target_ref) || 0) + 1);
    outgoing.get(edge.source_ref)?.push(edge.target_ref);
  }

  const queue = [...nodes.map((node) => node.ref).filter((ref) => incoming.get(ref) === 0)].sort();
  const depth = new Map(nodes.map((node) => [node.ref, 0]));
  while (queue.length) {
    const current = queue.shift();
    const children = [...(outgoing.get(current) || [])].sort();
    for (const child of children) {
      depth.set(child, Math.max(depth.get(child) || 0, (depth.get(current) || 0) + 1));
      incoming.set(child, (incoming.get(child) || 0) - 1);
      if (incoming.get(child) === 0) queue.push(child);
    }
    queue.sort();
  }

  const groups = new Map();
  for (const node of nodes) {
    const layer = depth.get(node.ref) || 0;
    if (!groups.has(layer)) groups.set(layer, []);
    groups.get(layer).push(node);
  }
  for (const group of groups.values()) {
    group.sort((left, right) => left.ref.localeCompare(right.ref));
  }

  const positions = new Map();
  let maxRows = 1;
  for (const [layer, group] of groups.entries()) {
    maxRows = Math.max(maxRows, group.length);
    group.forEach((node, row) => {
      positions.set(node.ref, {
        x: 30 + layer * 240,
        y: 30 + row * 112,
      });
    });
  }
  const maxLayer = Math.max(0, ...groups.keys());
  return {
    positions,
    width: Math.max(520, 240 * (maxLayer + 1) + 70),
    height: Math.max(220, maxRows * 112 + 60),
  };
}

