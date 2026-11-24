const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/manual_worm_annotation.js'), 'utf8');
const predict = source.slice(source.indexOf('async function predict()'), source.indexOf('function stroke('));

function fixture() {
  const worm = {points: [{label: 1}], mask: 'previous', token: 'prior', candidate: 2};
  const context = vm.createContext({
    busy: false, saved: false, active: () => worm, selected: 1, roi: null,
    state: {token: 'frame'}, keyListeners: new AbortController(), draw() {}, say() {},
    setBusy(value) { context.busy = value; },
    setTimeout: callback => setImmediate(callback),
    annotationPost: async (url, body) => {
      assert.equal(url, '/api/prediction');
      assert.equal(body.prediction_token, 'prior');
      assert.equal(body.previous_candidate, 2);
      return {job: 'work'};
    },
    decode: async mask => mask
  });
  let polls = 0;
  context.fetch = async (url, options) => {
    assert.equal(url, '/api/prediction?job=work');
    assert.equal(options.headers['X-Annotation-Token'], 'frame');
    return {ok: true, status: ++polls === 1 ? 202 : 200, json: async () =>
      polls === 1 ? {job: 'work'} : {mask: 'new', prediction_token: 'next', candidate: 0}};
  };
  return {worm, context, run: () => vm.runInContext(predict + '\npredict()', context)};
}

test('polling applies the completed mask and preserves refinement tokens', async () => {
  const {worm, context, run} = fixture();
  await run();
  assert.equal(worm.mask, 'new');
  assert.equal(worm.token, 'next');
  assert.equal(context.busy, false);
});

test('a mask decoded after leaving the annotation page is discarded', async () => {
  const {worm, context, run} = fixture();
  context.decode = async mask => { context.keyListeners.abort(); return mask; };
  await run();
  assert.equal(context.keyListeners.signal.aborted, true);
  assert.equal(worm.mask, 'previous');
  assert.equal(worm.token, 'prior');
});
