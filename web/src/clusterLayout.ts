export interface LayoutService {
  name: string;
  deps: string[];
}

/** Layer a discovered DAG by its actual dependencies, including shared callees. */
export function layoutCluster(services: LayoutService[]) {
  const byName = new Map(services.map(service => [service.name, service]));
  const incoming = new Map(services.map(service => [service.name, 0]));
  const layers = new Map(services.map(service => [service.name, 0]));
  for (const service of services) {
    for (const dep of new Set(service.deps)) {
      if (byName.has(dep)) incoming.set(dep, incoming.get(dep)! + 1);
    }
  }
  const ready = services.filter(service => incoming.get(service.name) === 0);
  const placed = new Set<string>();
  while (ready.length) {
    const service = ready.shift()!;
    placed.add(service.name);
    for (const dep of new Set(service.deps)) {
      if (!byName.has(dep)) continue;
      layers.set(dep, Math.max(layers.get(dep)!, layers.get(service.name)! + 1));
      incoming.set(dep, incoming.get(dep)! - 1);
      if (incoming.get(dep) === 0) ready.push(byName.get(dep)!);
    }
  }
  // Preserve visibility if a partial or malformed observation contains a cycle.
  const lastLayer = Math.max(0, ...layers.values());
  for (const service of services) {
    if (!placed.has(service.name)) layers.set(service.name, lastLayer + 1);
  }
  const rows = new Map<number, string[]>();
  for (const service of services) {
    const layer = layers.get(service.name)!;
    rows.set(layer, [...(rows.get(layer) ?? []), service.name]);
  }
  const width = Math.max(880, ...Array.from(rows.values(), row => row.length * 185 + 120));
  const height = Math.max(430, 170 + Math.max(0, ...rows.keys()) * 110);
  const coords = new Map<string, { x: number; y: number }>();
  for (const [layer, names] of rows) {
    names.sort((a, b) => a.localeCompare(b));
    names.forEach((name, index) => coords.set(name, {
      x: width / 2 + (index - (names.length - 1) / 2) * 185,
      y: 85 + layer * 110,
    }));
  }
  return { coords, width, height };
}
