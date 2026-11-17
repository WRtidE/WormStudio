'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

// Exercise the real startup dispatch, rather than only the readiness predicate.
const source=fs.readFileSync(path.join(__dirname,'../web/manual_worm_annotation.js'),'utf8');
const startup=source.match(/^init\(\)\.then\([^\n]+/m)?.[0];
assert.ok(startup,'annotation initialization dispatch is present');
for(const method of ['body_sequence','tapnext','self_contact']){
  test(`startup checks the dependencies required by ${method}`,async()=>{
    let calls=0;
    await vm.runInNewContext(startup,{
      init:()=>Promise.resolve(),state:{},analysisMethod:()=>method,
      checkOptionalMethods:()=>{calls++;}
    });
    assert.equal(calls,['body_sequence','tapnext'].includes(method)?1:0);
  });
}
