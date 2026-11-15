import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { edgePaths } from '../src/edgePaths.ts';

test('discovered waterfalls cover secondary and shared dependencies', () => {
  const services = JSON.parse(readFileSync(new URL('./fixtures/deepCluster.json', import.meta.url)));
  const edges = services.flatMap(service => service.deps.map(callee => ({ caller: service.name, callee })));
  const paths = edgePaths(edges);
  const key = edge => `${edge.caller}->${edge.callee}`;
  assert.deepEqual(new Set(paths.flat().map(key)), new Set(edges.map(key)));
  assert.ok(paths.length > services.find(service => service.name === 'edge-gateway').deps.length);
  assert.ok(paths.every(path => path[0].caller === 'edge-gateway'));
  for (const path of paths) {
    for (let index = 1; index < path.length; index++) {
      assert.equal(path[index].caller, path[index - 1].callee);
    }
  }
});

test('partial and cyclic observations remain bounded and contain only discovered edges', () => {
  assert.deepEqual(edgePaths([]), []);
  const partial = [{ caller: 'a', callee: 'b' }, { caller: 'a', callee: 'c' }];
  assert.equal(edgePaths(partial).length, 2);
  const cycle = [{ caller: 'a', callee: 'b' }, { caller: 'b', callee: 'a' }];
  assert.equal(edgePaths(cycle).length, 2);
});
