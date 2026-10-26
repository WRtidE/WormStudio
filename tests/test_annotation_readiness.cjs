'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {annotationReadiness, measureCalibration} = require('../web/annotation-readiness.js');
const fixture = {scale: '', worms: [{area: 300, confirmed: true, headPoint: [10, 20]}], method: 'topology'};

test('confirmed mask and head cannot replace missing scale; identifies the repair step', () => {
  const check = annotationReadiness(fixture);
  assert.deepEqual(check.blockers.map(b => [b.key, b.step]), [['scale', 1]]);
  assert.equal(check.masksReady, true);
  assert.equal(annotationReadiness({...fixture, scale: 1.25}).ready, true);
});
test('invalid or unfinished scale never enables saving', () => {
  for (const scale of ['', 0, -1, NaN, Infinity, 'bad']) assert.equal(annotationReadiness({...fixture, scale}).scaleReady, false);
  assert.equal(annotationReadiness({...fixture, scale: 1.25, calibrationPending: true}).ready, false);
});
test('every worm needs a nonempty reviewed mask; head annotation is optional', () => {
  for (const worms of [[], [{area: 19, confirmed: true}], [{area: 100, confirmed: false}], [...fixture.worms, {area: 100, confirmed: false}]]) {
    assert.equal(annotationReadiness({...fixture, scale: 1, worms}).masksReady, false);
  }
  assert.equal(annotationReadiness({...fixture, scale: 1, worms: [{area: 100, confirmed: true}]}).ready, true);
});
test('explicit pixel mode needs no fabricated physical scale', () => {
  assert.equal(annotationReadiness({...fixture,pixelMode:true,scale:'',calibrationPending:true}).ready,true);
  assert.equal(annotationReadiness({...fixture,pixelMode:false,scale:''}).ready,false);
});
test('optional method failure blocks only the selected optional method', () => {
  const input = {...fixture, scale: 1, tapnextIssue: '依赖不可用'};
  assert.equal(annotationReadiness(input).ready, true);
  const check = annotationReadiness({...input, method: 'tapnext'});
  assert.deepEqual(check.blockers.map(b => b.key), ['method']);
  assert.equal(annotationReadiness({...input, method: undefined}).methodReady, false);
});
test('all missing requirements remain visible together', () => {
  const check = annotationReadiness({...fixture, worms: [], method: 'tapnext', tapnextIssue: '不可用'});
  assert.deepEqual(check.blockers.map(b => b.step), [1, 2, 3]);
});
test('ruler computes physical scale and recomputes when its real length changes', () => {
  const points = [[20, 40], [120, 40]];
  assert.equal(measureCalibration(points, 250).scale, 2.5);
  assert.equal(measureCalibration(points, 500).scale, 5);
  assert.deepEqual(measureCalibration(points, 500).calibration, {points, length_um: 500});
});
test('incomplete ruler, coincident points, and invalid lengths have no usable scale', () => {
  const points = [[20, 40], [120, 40]];
  for (const [p, length] of [[[], 100], [[[0, 0]], 100], [[[0, 0], [0, 0]], 100], [points, ''], [points, 0], [points, -1], [points, Infinity]]) {
    const result = measureCalibration(p, length);
    assert.ok(result.error);
    assert.equal(result.scale, undefined);
    assert.equal(result.calibration, undefined);
  }
});
