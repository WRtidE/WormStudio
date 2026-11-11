/* Serialize writes so an older response can never mark newer edits as saved. */
(function(root) {
  'use strict';
  function createDraftAutosave({capture, write, onState = () => {}, delay = 900}) {
    let revision = 0, persisted = 0, timer = null, flight = null, stopped = false;
    const dirty = () => revision > persisted;
    const report = (status, error) => onState({status, dirty:dirty(), error});
    function schedule() {
      clearTimeout(timer);
      if (!stopped) timer = setTimeout(() => flush().catch(() => {}), delay);
    }
    async function flush() {
      clearTimeout(timer);
      if (flight) { await flight; return flush(); }
      if (stopped || !dirty()) return;
      const version = revision;
      flight = Promise.resolve().then(() => {
        const body = capture(); report('saving'); return write(body);
      }).then(() => { persisted = version; report(dirty() ? 'pending' : 'saved'); })
        .catch(error => { report('error', error); throw error; });
      try { await flight; } finally { flight = null; }
      if (dirty()) return flush();
    }
    return {
      mark() { if (stopped) return; revision++; report('pending'); schedule(); },
      dirty, flush,
      stop() { stopped = true; clearTimeout(timer); },
    };
  }
  if (typeof module === 'object' && module.exports) module.exports = {createDraftAutosave};
  else root.createDraftAutosave = createDraftAutosave;
})(globalThis);
