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
    $(selector) {
      assert.equal(selector, '#tracking-contrast');
      return {value: 'edge_refine'};
    },
    state: {token: 'frame'}, keyListeners: new AbortController(), draw() {}, say() {},
    setBusy(value) { context.busy = value; },
    setTimeout: callback => setImmediate(callback),
    annotationPost: async (url, body) => {
      assert.equal(url, '/api/prediction');
      assert.equal(body.prediction_token, 'prior');
      assert.equal(body.previous_candidate, 2);
      assert.equal(body.sam2_input_contrast, 'edge_refine');
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

test('alternating chambers preserve independent strokes, prompts and structural undo', async () => {
  let writes=0, reads=0;
  function canvas(pixels){
    return {pixels,toDataURL(){writes++;return this.pixels},getContext(){return this}};
  }
  const worm=pixels=>({mask:canvas(pixels),points:[],headPoint:null,confirmed:false});
  const other=worm('B0'), untouched=worm('C0');
  const context=vm.createContext({
    objects:new Map([[1,worm('A0')],[2,other],[3,untouched]]), selected:1, history:[],
    roi:null, poly:[], busy:false, saved:false, draftSaver:null, $:()=>({}),
    setBusy(v){context.busy=v}, refresh(){}, draw(){}, say(){},
    readMask(pixels){reads++;return canvas(pixels)}
  });
  const run=script=>vm.runInContext(script,context);
  run(source.slice(source.indexOf('const maskPNGs='),source.indexOf('function selectObject(')));
  run('decode=async value=>{const c=readMask(value);maskPNGs.set(c,value);return c}');
  run("remember();objects.get(1).points.push({x:1});remember();editMask(objects.get(1).mask).pixels='A1'");
  assert.equal(writes,1);
  assert.equal(context.history[0].before.length,1);
  run("selected=2;remember();editMask(objects.get(2).mask).pixels='B1'");
  run("remember([1],true);objects.delete(1)");
  await run('undo()');
  assert.deepEqual([...context.objects.keys()],[1,2,3]);
  assert.equal(context.objects.get(1).mask.pixels,'A1');
  assert.equal(context.objects.get(2),other);
  await run('undo()');
  assert.equal(context.objects.get(2).mask.pixels,'B0');
  assert.equal(context.objects.get(1).mask.pixels,'A1');
  await run('undo()');
  assert.equal(context.selected,1);
  assert.equal(context.objects.get(1).mask.pixels,'A0');
  assert.equal(context.objects.get(1).points.length,1);
  await run('undo()');
  assert.equal(context.objects.get(1).points.length,0);
  assert.equal(context.objects.get(3),untouched);
  assert.equal(writes,3);
  assert.equal(reads,3);
  run("remember([],true);objects.set(4,objects.get(3));selected=4");
  await run('undo()');
  assert.equal(context.selected,1);
  assert.deepEqual([...context.objects.keys()],[1,2,3]);
  run('for(let i=0;i<15;i++){remember();objects.get(1).points.push({x:i})}');
  assert.equal(context.history.length,12);
  for(let i=0;i<12;i++)await run('undo()');
  assert.deepEqual(Array.from(context.objects.get(1).points,p=>p.x),[0,1,2]);
  assert.equal(writes,3);
});
