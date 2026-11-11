'use strict';

// Shared by the step indicators, repair links, and save guard.
function annotationReadiness({scale, pixelMode = false, calibrationPending = false, worms, method, tapnextIssue = ''}) {
    const scaleReady = pixelMode || (Number.isFinite(Number(scale)) && Number(scale) > 0 && !calibrationPending);
    const confirmed = worms.filter(w => w.area >= 20 && w.confirmed).length;
    const masksReady = worms.length > 0 && confirmed === worms.length;
    const methodReady = ['topology', 'wormtracer', 'niu', 'hybrid', 'tapnext'].includes(method)
        && (method !== 'tapnext' || !tapnextIssue);
    const blockers = [];
    if (!scaleReady) blockers.push({
        key: 'scale', step: 1, title: calibrationPending ? '标尺测量尚未完成' : '尺寸标定未完成',
        detail: '填写真实的 μm/px 比例，或用已知长度的标尺量取。虫体掩膜和头部标记会保留。',
        action: '去完成尺寸标定'
    });
    if (!masksReady) blockers.push({
        key: 'masks', step: 2, title: '虫体掩膜还需确认',
        detail: `已确认 ${confirmed} / ${worms.length} 条。检查绿色轮廓后，点击底部操作栏的“确认虫体”。头部标记不能代替掩膜确认。`,
        action: '去确认虫体掩膜'
    });
    if (!methodReady) blockers.push({
        key: 'method', step: 3, title: '分析准备尚未完成',
        detail: tapnextIssue || '分析组件暂不可用，请检查本地安装后重试。', action: '查看分析准备'
    });
    return {scaleReady, masksReady, methodReady, confirmed, blockers, ready: blockers.length === 0};
}

function measureCalibration(points, length) {
    if (points.length !== 2) return {error: points.length ? '已选第一个端点，请点击标尺另一端。' : '请在画面中依次点击标尺的两个端点。'};
    const distance = Math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1]);
    if (!Number.isFinite(distance) || distance < 1) return {error: '两个端点太近，请重新点击标尺两端。'};
    if (!Number.isFinite(Number(length)) || Number(length) <= 0) return {error: '两个端点已选好，请填写标尺注明的真实长度（μm）。'};
    const scale = Number(length) / distance;
    if (!Number.isFinite(scale) || scale <= 0) return {error: '标尺比例无效，请检查长度和端点。'};
    return {scale, distance, calibration: {points: points.map(p => p.slice()), length_um: Number(length)}};
}

if (typeof module !== 'undefined' && module.exports) module.exports = {annotationReadiness, measureCalibration};
