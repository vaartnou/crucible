import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { layoutCluster } from '../src/clusterLayout.ts';

const cluster = JSON.parse(readFileSync(new URL('./fixtures/deepCluster.json', import.meta.url)));

test('all shared dependencies flow downward without overlapping service cards', () => {
  const { coords, width, height } = layoutCluster(cluster);
  assert.equal(coords.size, 23);
  for (const service of cluster) {
    const position = coords.get(service.name);
    assert.ok(position.x >= 71 && position.x <= width - 71);
    assert.ok(position.y >= 26 && position.y <= height - 26);
    for (const dependency of service.deps) {
      assert.ok(coords.get(dependency).y > position.y, `${service.name} → ${dependency}`);
    }
    for (const other of cluster) {
      if (other.name === service.name) continue;
      const otherPosition = coords.get(other.name);
      assert.ok(Math.abs(position.x - otherPosition.x) >= 142 || Math.abs(position.y - otherPosition.y) >= 52);
    }
  }
  assert.ok(new Set([...coords.values()].map(position => position.y)).size >= 5);
});

test('layout depends on topology rather than service response order or name suffixes', () => {
  const positions = services => [...layoutCluster(services).coords].sort();
  assert.deepEqual(positions(cluster), positions([...cluster].reverse()));
});

test('partial discovery and empty clusters remain renderable', () => {
  assert.equal(layoutCluster([]).coords.size, 0);
  const partial = [{ name: 'opaque-17', deps: ['undiscovered'] }];
  assert.equal(layoutCluster(partial).coords.size, 1);
  assert.equal(layoutCluster(partial).coords.has('undiscovered'), false);
});

test('a malformed cyclic observation cannot hide cards or hang layout', () => {
  const cycle = [{ name: 'a', deps: ['b'] }, { name: 'b', deps: ['a'] }];
  assert.equal(layoutCluster(cycle).coords.size, 2);
});
