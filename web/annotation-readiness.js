'use strict';

// Shared by the step indicators, repair links, and save guard.
function annotationReadiness({scale, pixelMode = false, calibrationPending = false, worms, method, chamberMode = false, algorithmIssue = ''}) {
    const scaleReady = pixelMode || (Number.isFinite(Number(scale)) && Number(scale) > 0 && !calibrationPending);
    const confirmed = worms.filter(w => w.area >= 20 && w.confirmed && (!chamberMode || w.roi)).length;
    const masksReady = worms.length > 0 && confirmed === worms.length;
    const methodReady = ['body_sequence', 'self_contact', 'topology', 'wormtracer', 'niu', 'hybrid'].includes(method)
        && (method!=='body_sequence' || !algorithmIssue) && (method!=='body_sequence' || chamberMode || worms.length===1) && (method !== 'self_contact' || worms.length <= 1);
    const blockers = [];
    if (!scaleReady) blockers.push({
        key: 'scale', step: 1, title: calibrationPending ? '标尺测量尚未完成' : '尺寸标定未完成',
        action: '去完成尺寸标定'
    });
    if (!masksReady) blockers.push({
        key: 'masks', step: 2, title: '虫体掩膜还需确认',
        action: '去确认虫体掩膜'
    });
    if (!methodReady) blockers.push({
        key: 'method', step: 3, title: '分析准备尚未完成',
        detail: method === 'self_contact' && worms.length > 1 ? '每次分析一条线虫，请保留需要分析的一条。' : algorithmIssue || '分析组件暂不可用，请检查本地安装后重试。', action: '查看分析准备'
    });
    return {scaleReady, masksReady, methodReady, confirmed, blockers, ready: blockers.length === 0};
}

function measureCalibration(points, length) {
    if (points.length !== 2) return {error: points.length ? '已选第一个端点，请点击标尺另一端。' : '请在画面中依次点击标尺的两个端点。'};
    const distance = Math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1]);
    if (!Number.isFinite(distance) || distance < 1) return {error: '两个端点太近，请重新点击标尺两端。'};
    if (!Number.isFinite(Number(length)) || Number(length) <= 0) return {distance, error: '请填写标尺的实际长度（μm）。'};
    const scale = Number(length) / distance;
    if (!Number.isFinite(scale) || scale <= 0) return {error: '标尺比例无效，请检查长度和端点。'};
    return {scale, distance, calibration: {points: points.map(p => p.slice()), length_um: Number(length)}};
}

// Find the colored component under the click in unscaled source pixels. Use its
// horizontal or vertical projections to distinguish a straight bar from text.
function detectScaleBar({data, width, height}, [x, y]) {
    const seed = y * width + x;
    const color = data.subarray(seed * 4, seed * 4 + 3);
    const low = Math.min(...color), chroma = Math.max(...color) - low;
    if (chroma < 55) return null;
    const hue = Array.from(color, channel => (channel - low) / chroma);
    const visited = new Uint8Array(width * height);
    const queue = [seed];
    visited[seed] = 1;
    function visit(pixel) {
        if (visited[pixel]) return;
        visited[pixel] = 1;
        const offset = 4 * pixel;
        const r = data[offset], g = data[offset + 1], b = data[offset + 2];
        const base = Math.min(r, g, b), range = Math.max(r, g, b) - base;
        if (range >= 45 && Math.abs((r-base)/range - hue[0]) < .30 &&
            Math.abs((g-base)/range - hue[1]) < .30 && Math.abs((b-base)/range - hue[2]) < .30) {
            queue.push(pixel);
        }
    }
    const rows = new Uint32Array(height), columns = new Uint32Array(width);
    let left = x, right = x, top = y, bottom = y;
    for (let i = 0; i < queue.length; i++) {
        const pixel = queue[i], col = pixel % width, row = Math.floor(pixel / width);
        left = Math.min(left, col); right = Math.max(right, col);
        top = Math.min(top, row); bottom = Math.max(bottom, row);
        // Once even the narrow side is too large, no extension can form a bar.
        if (6 * Math.min(right-left+1, bottom-top+1) > Math.max(width, height)) return null;
        rows[row]++; columns[col]++;
        if (col > 0) visit(pixel - 1);
        if (col + 1 < width) visit(pixel + 1);
        if (row > 0) visit(pixel - width);
        if (row + 1 < height) visit(pixel + width);
    }
    const horizontal = right-left >= bottom-top;
    const along = horizontal ? columns : rows, across = horizontal ? rows : columns;
    const first = horizontal ? left : top, last = horizontal ? right : bottom;
    const span = last - first + 1;
    if (span < 20 || span < 6 * (horizontal ? bottom-top+1 : right-left+1)) return null;
    if (first === 0 || last === along.length - 1) return null;

    const shaft = [];
    for (let i = 0; i < across.length; i++) {
        if (across[i] >= span * .9) shaft.push(i);
    }
    if (!shaft.length || shaft.at(-1) - shaft[0] + 1 !== shaft.length) return null;
    const center = (shaft[0] + shaft.at(-1)) / 2;
    // End caps, when present, are measured from center to center.
    const capHeight = Math.max(shaft.length + 3, shaft.length * 1.45);
    let a = first, b = last;
    while (a <= last && along[a] >= capHeight) a++;
    while (b >= first && along[b] >= capHeight) b--;
    const capA = a-first, capB = last-b;
    if (Boolean(capA) !== Boolean(capB) || Math.max(capA, capB) > span * .15) return null;
    const start = capA ? (first+a-1)/2 : first;
    const end = capB ? (last+b+1)/2 : last;
    return horizontal ? [[start, center], [end, center]] : [[center, start], [center, end]];
}

if (typeof module !== 'undefined' && module.exports) module.exports = {annotationReadiness, measureCalibration, detectScaleBar};
