'use strict';
// Public wording for current and historical progress. Raw diagnostics stay on disk.
(() => {
  function text(value) {
    return String(value??'')
      .replace(/SAM2\s*\+\s*TAPNext\+\+(?:（单虫）)?/gi,'自动分析')
      .replace(/NIU\s*\/\s*WormTracer|WormTracer\s*\+\s*NIU/gi,'姿态分析')
      .replace(/TAPNext(?:\+\+)?(?:\s*双向点跟踪与时序融合)?/gi,'姿态分析')
      .replace(/SAM2(?:\s*(?:Large|Tiny))?/gi,'虫体识别')
      .replace(/WormTracer|\bNIU\b/gi,'姿态分析')
      .replace(/twardis|taper_seed/gi,'头尾识别');
  }
  function stage(row) {
    if(row?.name?.startsWith('body_sequence-worm-'))return '多帧身体中线与头尾跟踪 · 线虫 '+row.name.split('-').pop();
    if (row?.name === 'crop-recovery') return '触边扩框与重跟踪';
    const phases={tracking:'虫体识别与跟踪',cleanup:'轮廓检查与清理',quality:'身份与接触检查',fit:'姿态分析',repair:'姿态检查与修正',temporal:'连续性检查',metrics:'表型指标计算',export:'视频导出与校验'};
    const worm=/-worm-(\d+)$/.exec(row?.name||'');
    return phases[row?.phase] ? phases[row.phase]+(worm?' · 线虫 '+worm[1]:'') : text(row?.label||'正在分析');
  }
  function error(value) {
    const message=text(value);
    if (/^export 失败/.test(message)) {
      const dest=message.match(/FileNotFoundError:[^\n]*?'([A-Za-z]:[^'\n]+\.mp4)'/);
      const longPath=/WinError 206/.test(message) || (dest && dest[1].replace(/\\\\/g,'\\').length>=260);
      return (longPath?'标注视频的保存路径过长。':'标注视频导出失败。')+
        '已保存跟踪和测量结果。可从历史任务选择「继续此任务」，重试视频导出。';
    }
    return /Traceback|CalledProcessError|Command\s*\[|File ".+", line/.test(message)
      ? '处理未完成，请检查视频文件、可用内存和结果目录权限。详细记录已保存在本次任务目录。' : message;
  }
  function totalsReady(data) {
    return data?.status==='complete' && !!data.sample && !!data.phenotypes?.worms?.length
      && !data.history_reopened && !data.batch?.running;
  }
  function sessionView(data) {
    if(data?.batch?.running)return 'progress';
    if(!data||data.status==='ready')return 'preview';
    if(data.status==='preparing')return 'annotation';
    return data.status==='complete'?'results':'progress';
  }
  const presentation={text,stage,error,totalsReady,sessionView};
  if(typeof window!=='undefined')window.AnalysisPresentation=presentation;
  if(typeof module!=='undefined'&&module.exports)module.exports=presentation;
})();
