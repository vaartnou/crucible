/** Enumerate every observed branch without inventing hidden topology. */
export function edgePaths<T extends { caller: string; callee: string }>(edges: T[]): T[][] {
  const byCaller = new Map<string, T[]>();
  for (const edge of edges) byCaller.set(edge.caller, [...(byCaller.get(edge.caller) ?? []), edge]);
  const callees = new Set(edges.map(edge => edge.callee));
  const gatewayEdges = byCaller.get('edge-gateway');
  const roots = gatewayEdges ?? edges.filter(edge => !callees.has(edge.caller));
  if (!roots.length) return edges.map(edge => [edge]);
  const paths: T[][] = [];
  const pending = [...roots].reverse().map(edge => [edge]);
  while (pending.length) {
    const path = pending.pop()!;
    const seen = new Set([path[0].caller, ...path.map(edge => edge.callee)]);
    const next = (byCaller.get(path[path.length - 1].callee) ?? []).filter(edge => !seen.has(edge.callee));
    if (!next.length) paths.push(path);
    else pending.push(...[...next].reverse().map(edge => [...path, edge]));
  }
  return paths;
}
