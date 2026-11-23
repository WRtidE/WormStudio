'use strict';
// Headless release verification; the desktop uses the same Setup implementation.
const fs = require('node:fs');
const path = require('node:path');
const {Setup} = require('../electron/online-setup.cjs');
const platform = require('../../src/platforms/online.cjs');
async function main() {
  const [manifestFile,directory,backend] = process.argv.slice(2);
  if (!manifestFile || !directory || (backend && !['cpu','gpu'].includes(backend))) throw Error('Usage: node prepare_online.cjs MANIFEST COMPONENT_DIRECTORY [cpu|gpu]');
  let phase;
  const setup = new Setup({manifest:JSON.parse(fs.readFileSync(manifestFile,'utf8')),
    directory:path.resolve(directory),platform,fetch,gpu:backend === 'gpu',notify:state => {
      if (phase !== state.phase) { phase=state.phase;console.error(state.title); }
    }});
  const result = await setup.start(backend === 'cpu');
  if (!result) throw Error(setup.state.error || setup.state.detail);
  console.log(JSON.stringify(result));
}
if (require.main === module) main().catch(error => { console.error(error.message);process.exitCode=1; });
