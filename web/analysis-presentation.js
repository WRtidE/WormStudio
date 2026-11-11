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
    const phases={tracking:'虫体识别与跟踪',cleanup:'轮廓检查与清理',quality:'身份与接触检查',fit:'姿态分析',repair:'姿态检查与修正',temporal:'连续性检查',metrics:'表型指标计算',export:'视频导出与校验'};
    const worm=/-worm-(\d+)$/.exec(row?.name||'');
    return phases[row?.phase] ? phases[row.phase]+(worm?' · 线虫 '+worm[1]:'') : text(row?.label||'正在分析');
  }
  function error(value) {
    const message=text(value);
    return /Traceback|CalledProcessError|Command\s*\[|File ".+", line/.test(message)
      ? '处理未完成，请检查视频文件、可用内存和结果目录权限。详细记录已保存在本次任务目录。' : message;
  }
  const presentation={text,stage,error};
  if(typeof window!=='undefined')window.AnalysisPresentation=presentation;
  if(typeof module!=='undefined'&&module.exports)module.exports=presentation;
})();
