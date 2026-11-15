'use strict';

// Both channel viewers read small windows from one sequential video decoder.
window.createChannelFrames = function ({request, route, run}) {
  const cache = new Map(), batchSize = 6;
  const controller = new AbortController();
  let pending = null;

  function fetchBatch(number, direction, size = batchSize) {
    const start = direction < 0 ? Math.max(1, number-size+1) : number;
    const limit = direction < 0 ? number-start+1 : size;
    pending = request(route('frames', {run, start, limit}), {signal:controller.signal})
      .then(data => {
        if (controller.signal.aborted) return;
        for (const photo of data.frames) {
          cache.delete(photo.frame);
          cache.set(photo.frame, photo);
          if (cache.size > 4*batchSize) cache.delete(cache.keys().next().value);
        }
      }).finally(() => { pending = null; });
    return pending;
  }

  async function read(number, direction, single = false) {
    while (!cache.has(number) && pending) await pending.catch(() => {});
    if (!cache.has(number) && !controller.signal.aborted) await fetchBatch(number,direction,single ? 1 : batchSize);
    const photo = cache.get(number);
    if (!photo || controller.signal.aborted) return null;
    const image = new Image();
    image.src = photo.image;
    await image.decode();
    return controller.signal.aborted ? null : photo;
  }

  function prefetch(number, direction, last) {
    if (pending || controller.signal.aborted) return;
    for (let i=1; i<=3; i++) {
      const next = number+i*direction;
      if (next<1 || next>last) return;
      if (!cache.has(next)) {
        // A failed speculative read is retried when the frame is actually requested.
        fetchBatch(next,direction).catch(() => {});
        return;
      }
    }
  }

  return {read, prefetch, close() { controller.abort(); cache.clear(); }};
};

window.createChannelReview = function ({request, route}) {
  const el = id => document.getElementById('cross-' + id);
  const stage = el('photo').parentElement;
  let task, channel, available, records = [], event = null, eventPhoto = null;
  let wantedFrame = 1, displayedFrame = 0, selectedId = null, eventRequest, frameSource;
  let navigationRevision = 0, reading = false, jumpToTarget = false;

  function notice(id, text) {
    el(id).textContent = text;
    el(id).hidden = !text;
  }

  function showPhoto(photo, bounds, line, number) {
    displayedFrame = number;
    el('photo').hidden = false;
    el('full').src = photo.image;
    el('photo').style.aspectRatio = photo.width + ' / ' + photo.height;
    el('photo').style.maxWidth = photo.width / photo.height * 49 + 'vh';
    el('band').hidden = !bounds;
    if (bounds) {
      el('band').style.left = 100 * bounds[0] / photo.width + '%';
      el('band').style.width = 100 * (bounds[1] - bounds[0]) / photo.width + '%';
    }
    el('line').style.top = 100 * line / photo.height + '%';
    el('frame').textContent = number + ' / ' + task.frame + ' 帧';
    stage.setAttribute('aria-busy','false');
    notice('photo-status', '');
  }

  function triplet(photo) {
    const linePercent = 100 * (event.line_y - photo.crop[1]) / (photo.crop[3] - photo.crop[1]);
    const fragment = document.createDocumentFragment();
    const previous = event.prev_frame ?? event.frame - 1;
    for (const [number, label] of [[previous, previous === event.frame - 1 ? '前一帧' : '前次观测'],
                                   [event.frame, '越线帧'], [event.frame + 1, '后一帧']]) {
      const source = photo.frames.find(item => item.frame === number);
      const figure = document.createElement('figure'), caption = document.createElement('figcaption');
      if (source) {
        const box = document.createElement('div'), image = new Image(), line = document.createElement('i');
        image.alt = '通道 ' + event.channel + '，第 ' + number + ' 帧';
        image.src = source.image;
        box.style.aspectRatio = (photo.crop[2] - photo.crop[0]) + ' / ' + (photo.crop[3] - photo.crop[1]);
        line.className = 'cross-line';
        line.style.top = linePercent + '%';
        box.append(image, line);
        figure.append(box);
      }
      figure.classList.toggle('cross-center', number === event.frame);
      caption.textContent = source ? label : '无' + label;
      if (source && (number === event.frame || (number === previous && previous !== event.frame - 1))) {
        caption.textContent += ' · ' + number;
      }
      if (source) figure.title = label + ' · 第 ' + number + ' 帧';
      figure.append(caption);
      fragment.append(figure);
    }
    el('triplet').replaceChildren(fragment);
  }

  function frameControls(number) {
    wantedFrame = number;
    el('seek').value = number;
    el('seek').setAttribute('aria-valuetext', '第 ' + number + ' 帧');
    el('seek').disabled = !available || task.frame <= 1;
    el('before').disabled = !available || number <= 1;
    el('after').disabled = !available || number >= task.frame;
    el('return').hidden = !event || number === event.frame;
  }

  function cancelFrame() {
    navigationRevision++;
    reading = false;
    jumpToTarget = false;
  }

  async function browse() {
    if (reading || !frameSource) return;
    const revision = navigationRevision;
    reading = true;
    try {
      while (displayedFrame !== wantedFrame && revision === navigationRevision) {
        const direction = wantedFrame < displayedFrame ? -1 : 1;
        const jump = jumpToTarget || !displayedFrame;
        const number = jump ? wantedFrame : displayedFrame+direction;
        jumpToTarget = false;
        const eventFrame = number === event?.frame && eventPhoto;
        let photo = eventFrame || (number === task.frame && task.picture);
        if (!photo) {
          if (!available) { notice('photo-status','原视频不可用'); break; }
          stage.setAttribute('aria-busy','true');
          photo = await frameSource.read(number,direction,jump);
          if (!photo || revision !== navigationRevision) break;
        }
        await new Promise(requestAnimationFrame);
        if (revision !== navigationRevision) break;
        const ch = channel ?? event?.channel;
        const bounds = eventFrame ? event.bounds : photo.bounds[task.counts.length-ch];
        const line = eventFrame ? event.line_y : Math.floor(photo.height*task.settings.tripwire_ratio);
        showPhoto(photo,bounds,line,number);
        if (available && !jump && !jumpToTarget) frameSource.prefetch(number,direction,task.frame);
      }
    } catch (error) {
      if (revision === navigationRevision) {
        frameControls(displayedFrame || wantedFrame);
        notice('photo-status',error.message);
      }
    } finally {
      if (revision === navigationRevision) {
        reading = false;
        stage.setAttribute('aria-busy','false');
      }
    }
  }

  function seek(number, jump = false) {
    frameControls(Math.min(task.frame,Math.max(1,number)));
    jumpToTarget = jump;
    void browse();
  }

  async function select(position) {
    const chosen = records[position] ?? null;
    const keepPhoto = chosen && chosen === event && eventPhoto;
    eventRequest?.abort();
    cancelFrame();
    event = chosen;
    eventPhoto = keepPhoto || null;
    el('choose').value = position;
    if (event) {
      selectedId = event.id;
      el('choose').title = '第 ' + (position+1) + ' 条，共 ' + records.length + ' 条';
    }
    el('prev').disabled = position <= 0;
    el('next').disabled = position + 1 >= records.length;
    el('controls').hidden = !event;
    if (!event) {
      el('return').hidden = true;
      eventRequest = null;
      el('triplet').replaceChildren();
      el('photo').hidden = true;
      el('frame').textContent = '';
      notice('status', task.events == null ? '旧记录仅有汇总。重新计数可生成越线明细。' :
        channel ? '通道 ' + String(channel).padStart(2, '0') + ' 没有越线记录。' : '本次没有越线记录。');
      displayedFrame = 0;
      seek(task.frame,true);
      return;
    }
    displayedFrame = event.frame;
    frameControls(event.frame);
    if (keepPhoto) {
      showPhoto(eventPhoto, event.bounds, event.line_y,event.frame);
      return;
    }
    el('triplet').replaceChildren();
    el('photo').hidden = true;
    el('frame').textContent = '';
    stage.setAttribute('aria-busy',String(!!available));
    notice('photo-status','');
    notice('status', available ? '读取越线原帧…' : '原视频不可用');
    if (!available) return;
    const controller = new AbortController();
    eventRequest = controller;
    try {
      const photo = await request(route('event', {run:task.id, id:event.id}), {signal:controller.signal});
      if (controller.signal.aborted) return;
      eventPhoto = photo;
      if (wantedFrame === event.frame) showPhoto(photo, event.bounds, event.line_y,event.frame);
      triplet(photo);
      notice('status', '');
    } catch (error) {
      if (!controller.signal.aborted) {
        if (wantedFrame === event.frame) stage.setAttribute('aria-busy','false');
        notice('status', error.message);
      }
    } finally {
      if (eventRequest === controller) eventRequest = null;
    }
  }

  function stepEvent(step) { void select(el('choose').selectedIndex + step); }
  el('choose').onchange = () => select(el('choose').selectedIndex);
  el('prev').onclick = () => stepEvent(-1);
  el('next').onclick = () => stepEvent(1);
  el('seek').oninput = () => seek(Number(el('seek').value),true);
  el('before').onclick = () => seek(wantedFrame - 1);
  el('after').onclick = () => seek(wantedFrame + 1);
  stage.onkeydown = el('seek').onkeydown = e => {
    if (!['ArrowLeft','ArrowRight'].includes(e.key) || e.altKey || e.metaKey || e.ctrlKey || e.shiftKey || !available) return;
    e.preventDefault();
    const next = wantedFrame + (e.key === 'ArrowLeft' ? -1 : 1);
    if (next>=1 && next<=task.frame) seek(next);
  };
  el('return').onclick = () => { cancelFrame(); seek(event.frame,true); };
  el('markers').onchange = () => el('results').classList.toggle('cross-unmarked', !el('markers').checked);

  return function render(result, file, filter) {
    const pane = el('results');
    if (!result) {
      if (!pane.hidden) {
        cancelFrame();
        frameSource?.close(); frameSource=null;
        eventRequest?.abort();
      }
      pane.hidden = true;
      return;
    }
    const same = task?.id === result.id && task?.file === result.file;
    if (same && !pane.hidden && channel === filter && available === file.available) return;
    if (!same || !frameSource) {
      frameSource?.close();
      frameSource=window.createChannelFrames({request,route,run:result.id});
    }
    if (!same) { event = null; eventPhoto = null; selectedId = null; }
    available = file.available;
    pane.hidden = false;
    task = result;
    channel = filter;
    el('seek').max = task.frame;
    if (task.picture) {
      const {width, height} = task.picture;
      el('results').style.setProperty('--overview-ratio', width + ' / ' + height);
    }
    const events = task.events || [];
    records = channel == null ? events : events.filter(item => item.channel === channel);
    const options = document.createDocumentFragment();
    records.forEach((item, i) => {
      const when = item.time_s == null ? '第 ' + item.frame + ' 帧' : item.time_s.toFixed(2) + ' 秒';
      const label = '#' + item.id + ' · 通道 ' + String(item.channel).padStart(2, '0') + ' · ' + when;
      options.append(new Option(label,i));
    });
    const multiple = records.length > 1;
    for (const id of ['choose','prev','next']) el(id).hidden = !multiple;
    el('single').hidden = records.length !== 1;
    el('single').textContent = records.length === 1 ? options.firstChild.textContent : '';
    el('choose').replaceChildren(options);
    void select(Math.max(0, records.findIndex(item => item.id === selectedId)));
  };
};
