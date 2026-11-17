'use strict';

window.createResultEventTrack = function (track, legend, picker, seek) {
  const types = {
    pause: ['Ⅱ', '停顿'], reversal: ['↶', '倒退'],
    bend: ['∿', '头部摆动'], review: ['!', '待复核']
  };
  let events = [], fps = 1, lastFrame = 0;
  const choices = picker.querySelector('.event-choices');
  function label(event) {
    const frames = event.start === event.end ? event.start : event.start + '–' + event.end;
    return `${types[event.kind][1]} · 第 ${frames} 帧 · ${(event.start / fps).toFixed(2)} 秒`;
  }
  function choose(group) {
    if (group.length === 1) { seek(group[0].start); return; }
    choices.replaceChildren();
    for (const event of group) {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.kind = event.kind;
      button.textContent = types[event.kind][0] + '  ' + label(event);
      button.onclick = () => { picker.close(); seek(event.start); };
      choices.append(button);
    }
    picker.showModal();
  }
  function paint() {
    const width = track.clientWidth;
    if (!width || !events.length) return;
    const groups = [];
    for (const event of events) {
      const fraction = lastFrame ? event.start / lastFrame : 0;
      const last = groups.at(-1);
      if (last && (fraction - last.fraction) * width < 28) last.events.push(event);
      else groups.push({fraction, events:[event]});
    }
    const fragment = document.createDocumentFragment();
    for (const group of groups) {
      const first = group.events[0], count = group.events.length;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'event-pin';
      button.style.left = (group.fraction * 100) + '%';
      button.dataset.kind = count === 1 ? first.kind : 'group';
      button.textContent = count === 1 ? types[first.kind][0] : String(count);
      button.title = count === 1 ? label(first) :
        `${count} 个关键节点 · 第 ${first.start}–${group.events.at(-1).start} 帧，点击选择`;
      button.setAttribute('aria-label', button.title);
      button.onclick = () => choose(group.events);
      fragment.append(button);
    }
    track.replaceChildren(fragment);
  }
  new ResizeObserver(paint).observe(track);
  return function update(data, worm, ranges = []) {
    if (picker.open) picker.close();
    fps = data?.fps || 1;
    lastFrame = (data?.frame_count || 1) - 1;
    const recorded = (data?.events || []).filter(e => e.worm_id === worm && Object.hasOwn(types, e.kind));
    events = recorded.concat(ranges.map(r => ({...r, kind:'review'}))).sort((a,b) => a.start - b.start);
    track.hidden = legend.hidden = !events.length;
    track.replaceChildren();
    legend.replaceChildren();
    const used = new Set(events.map(e => e.kind));
    for (const [kind, [symbol, name]] of Object.entries(types)) {
      if (!used.has(kind)) continue;
      const item = document.createElement('span');
      item.dataset.kind = kind;
      item.textContent = symbol + ' ' + name;
      legend.append(item);
    }
    paint();
  };
};
