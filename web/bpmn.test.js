// .bpmn записанных примеров разбирается bpmn-moddle (парсер bpmn.io) без предупреждений: node --test web/
// bpmn-moddle — только devDependency для этого теста (npm ci в корне репо), в окно не попадает.
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const E = require('./engine.js');

const REC = path.join(__dirname, '..', 'app', 'recorded');
const NAMES = ['sales', 'invoice', 'returns', 'onboarding'];
const TYPE = { start: 'bpmn:StartEvent', end: 'bpmn:EndEvent', decision: 'bpmn:ExclusiveGateway' };

let moddle = null;
try { require.resolve('bpmn-moddle'); moddle = import('bpmn-moddle'); } catch (e) { moddle = null; }

test('.bpmn «Было» и «Стало» записанных примеров разбирается bpmn-moddle: все элементы и DI на месте',
  { skip: !moddle && 'нет bpmn-moddle — выполните npm ci в корне репо' }, async () => {
    const { BpmnModdle } = await moddle;
    for (const n of NAMES) {
      const f = JSON.parse(fs.readFileSync(path.join(REC, n + '.json'), 'utf8'));
      for (const [mode, p] of [['было', f.process], ['стало', E.after(f.process, f.improve)]]) {
        const name = n + ' ' + mode;
        const { rootElement: defs, warnings } = await new BpmnModdle().fromXML(E.toBpmn(p, { mode }));
        assert.deepEqual(warnings.map((w) => w.message), [], name);

        const collab = defs.rootElements.find((x) => x.$type === 'bpmn:Collaboration');
        const proc = defs.rootElements.find((x) => x.$type === 'bpmn:Process');
        assert.ok(collab && proc, name);
        assert.equal(collab.participants[0].processRef, proc, name + ': пул ссылается на процесс');

        const nodes = proc.flowElements.filter((x) => x.$type !== 'bpmn:SequenceFlow');
        const flows = proc.flowElements.filter((x) => x.$type === 'bpmn:SequenceFlow');
        assert.equal(nodes.length, p.steps.length, name);
        p.steps.forEach((s) => {
          const el = nodes.find((x) => x.id === s.id);
          assert.ok(el, name + ': ' + s.id);
          assert.equal(el.$type, TYPE[s.kind] || (s.auto ? 'bpmn:ServiceTask' : 'bpmn:Task'), name + ': тип ' + s.id);
          assert.equal(el.name, s.name);
        });
        const want = [];
        p.steps.forEach((s) => (s.kind === 'decision' ? s.branches.map((b) => [b.to, b.label]) : (s.next || []).map((t) => [t, '']))
          .forEach(([to, label]) => want.push(s.id + '>' + to + '>' + label)));
        assert.deepEqual(flows.map((x) => x.sourceRef.id + '>' + x.targetRef.id + '>' + (x.name || '')).sort(), want.sort(), name + ': рёбра');

        const lanes = proc.laneSets[0].lanes;
        const refs = lanes.flatMap((l) => l.flowNodeRef.map((x) => x.id));
        assert.deepEqual(refs.slice().sort(), p.steps.map((s) => s.id).sort(), name + ': каждый шаг ровно в одной дорожке');
        lanes.forEach((l) => l.flowNodeRef.forEach((x) => {
          const s = p.steps.find((st) => st.id === x.id);
          assert.equal(l.id, 'Lane_' + (s.auto ? E.SYS : s.role), name + ': дорожка ' + x.id);
        }));

        const plane = defs.diagrams[0].plane;
        assert.equal(plane.bpmnElement, collab);
        const shapes = plane.planeElement.filter((x) => x.$type === 'bpmndi:BPMNShape');
        const edges = plane.planeElement.filter((x) => x.$type === 'bpmndi:BPMNEdge');
        const drawn = new Set(shapes.map((x) => x.bpmnElement && x.bpmnElement.id));
        [collab.participants[0], ...lanes, ...nodes].forEach((el) => assert.ok(drawn.has(el.id), name + ': нет DI у ' + el.id));
        shapes.forEach((x) => ['x', 'y', 'width', 'height'].forEach((k) => assert.ok(Number.isFinite(x.bounds[k]), name + ': bounds ' + x.id)));
        assert.equal(edges.length, flows.length, name);
        edges.forEach((x) => {
          assert.ok(flows.includes(x.bpmnElement), name + ': ребро DI без потока');
          assert.ok(x.waypoint.length >= 2 && x.waypoint.every((w) => Number.isFinite(w.x) && Number.isFinite(w.y)), name + ': waypoints ' + x.id);
        });
      }
    }
  });
