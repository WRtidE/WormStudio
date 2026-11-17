'use strict';
// Navigate the submitted sequence, never the global history catalogue.
(() => {
  function build(progress) {
    let items = (progress?.batch?.result_items || []).filter(item => item.status === 'complete');
    let index = items.findIndex(item => item.sample === progress?.sample);
    const grouped = index >= 0;
    // A separately opened historical result has no inferred neighbours.
    if (!grouped && progress?.status === 'complete' && progress.sample) {
      items = [{sample:progress.sample, name:progress.video_name}];
      index = 0;
    }
    return {items, index, grouped, previous: index > 0 ? items[index - 1] : null,
      next: index >= 0 && index < items.length - 1 ? items[index + 1] : null};
  }
  function chambers(progress) {
    const items = (progress?.batch?.result_items || []).filter(item => item.chamber_id != null);
    const belongs = items.some(item => item.sample === progress?.sample);
    return belongs || (progress?.status === 'ready' && !progress.sample) ? items : [];
  }
  function group(progress) {
    const batch=progress?.batch, items=batch?.result_items || [];
    if (!items.some(item=>item.chamber_id!=null) && items.length<2) return [];
    if (batch.running || items.some(item=>item.sample===progress.sample) || (!progress.sample && progress.status==='ready')) return items;
    return [];
  }
  const api = {build, chambers, group};
  if (typeof window !== 'undefined') window.ResultNavigation = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
