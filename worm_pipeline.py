"""通用线虫视频处理入口：修改开头设置后直接运行，或传入 --config。"""
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from resource_paths import PROJECT_ROOT as ROOT, RESOURCE_ROOT, ensure_runtime, sam2_checkpoint, motion_constraints_path

if __name__ == '__main__':
    ensure_runtime()

# ==================== 在这里修改本次视频的设置 ====================
VIDEO_PATH = str(ROOT / 'input_videos' / 'example.mp4')
SAMPLE_NAME = "experiment-01"  # 新视频或新参数建议使用新名字

# True：外部浏览器中点击虫体分割、人工修正，再选择中心线方法。
# False：使用下方 GROUPS 中的点标记或已有掩膜。
MANUAL_ANNOTATION = True
CENTERLINE_METHOD = "tapnext"  # 本轮单虫默认路线；可在界面切换已有方法
SAM2_MODEL = "large"            # 点选分割和全视频跟踪统一使用：large / tiny
ANNOTATION_FRAME = 0            # 选择较容易区分线虫的一帧，帧号从 0 开始
HEAD_DETECTION = 'taper_seed'  # 首帧人工优先，否则按起始形状初始化；正式头摆仍需人工
OPEN_BROWSER = True             # 自动打开外部浏览器；False 时打印网址

# None：显示完整画面，在浏览器中选分析区域；也可填写 [左, 上, 右, 下]。
CROP_XYXY = None
# None：在浏览器中量标尺或填写 μm/px；填写正数可预设比例。
UM_PER_PIXEL = None
SCALE_SOURCE = "User-supplied calibration"

# 人工模式不需要填写虫数或坐标；页面中逐条添加并点选线虫。
# 点标记模式（MANUAL_ANNOTATION=False）才需要填写 GROUPS；格式见使用说明。
GROUPS = []
REVIEW_INTERVALS = []           # 人工复核后，明确需要排除出指标汇总的帧区间

# 初始拟合缩放比例与损失权重；0.2 比 0.3 节省显存。
WORMTRACER = {'rescale': 0.3, 'rescale_by_worm': {},
              'continuity_weight': 100, 'smoothness_weight': 5000, 'length_weight': 5}

RESUME = True                   # True 复用人工标注和已完成结果；False 重新标注并全算
CHECK_ONLY = False              # True 只检查设置，不打开标注或运行计算
STOP_AFTER = 'export'           # tracking / cleanup / quality / fit / repair / temporal / metrics / export
PHENOTYPE_CONFIG = ROOT / 'config' / 'phenotype_config.json'
# ==================== 以下为处理流程，一般无需修改 ====================

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from centerline_methods import validate_method, method_source
from sam2_models import model_spec, tracking_model_matches

PHASES = ('tracking', 'cleanup', 'quality', 'fit', 'repair', 'temporal', 'metrics', 'export')
FIT_DEFAULTS = dict(rescale=.3, continuity_weight=100, smoothness_weight=5000, length_weight=5,
                    rescale_by_worm={})


def video_config():
    return dict(sample=SAMPLE_NAME, video=VIDEO_PATH, crop_xyxy=CROP_XYXY,
                scale_um_per_px=UM_PER_PIXEL, scale_source=SCALE_SOURCE,
                groups=GROUPS, review_intervals=REVIEW_INTERVALS, wormtracer=WORMTRACER,
                centerline_method=CENTERLINE_METHOD, sam2_model=SAM2_MODEL,
                head_detection=HEAD_DETECTION)


def validate_config(config, relative_to=ROOT):
    """Validate geometry before loading a model; all frames are zero-based."""
    import cv2
    c = json.loads(json.dumps(config))
    name = c.get('sample', '')
    reserved = {'all', 'con', 'prn', 'aux', 'nul'} | {f'{p}{i}' for p in ('com', 'lpt') for i in range(1, 10)}
    if not re.fullmatch(r'[\w][\w.-]{0,79}', name) or name.endswith('.') or name.split('.')[0].lower() in reserved:
        raise ValueError('sample 必须是安全的文件夹名，例如 experiment_01，不能含路径分隔符')
    path = Path(c.get('video', ''))
    path = path if path.is_absolute() else relative_to / path
    if not path.is_file():
        raise ValueError(f'视频不存在：{path}')
    c['video'] = str(path.resolve())
    cap = cv2.VideoCapture(c['video'])
    n, fps, width, height = int(cap.get(7)), cap.get(5), int(cap.get(3)), int(cap.get(4))
    opened = cap.isOpened()
    cap.release()
    if not opened or n < 5 or not math.isfinite(fps) or fps <= 0:
        raise ValueError('视频必须可读取，至少 5 帧，并且具有有效帧率')

    def rect(value, bounds, label):
        if not isinstance(value, list) or len(value) != 4 or any(type(v) is not int for v in value):
            raise ValueError(f'{label} 必须为四个整数 [x0,y0,x1,y1]')
        a, b, d, e = value
        x, y, x1, y1 = bounds
        if not (x <= a < d <= x1 and y <= b < e <= y1):
            raise ValueError(f'{label} 超出范围或面积为零：{value}')

    def frame(value, label):
        if type(value) is not int or not 0 <= value < n:
            raise ValueError(f'{label} 应在 0–{n - 1} 之间')

    rect(c.get('crop_xyxy'), [0, 0, width, height], '分析裁剪范围')
    from measurement_units import validate_scale
    validate_scale(c)
    c.setdefault('scale_source', 'User-supplied calibration')
    if not isinstance(c.get('groups'), list) or not c['groups']:
        raise ValueError('groups 至少需要一个分组')
    ids, names = [], set()
    for group in c['groups']:
        name = group.get('name', '')
        if not re.fullmatch(r'[\w][\w-]{0,50}', name) or name in names:
            raise ValueError('分组 name 必须唯一，且只含字母、数字、下划线或连字符')
        names.add(name)
        rect(group.get('crop_xyxy'), c['crop_xyxy'], f'{name} 裁剪范围')
        frame(group.get('anchor_frame'), f'{name} anchor_frame')
        frame(group.get('propagate_from', group['anchor_frame']), f'{name} propagate_from')
        worms = group.get('worms', [])
        if not worms:
            raise ValueError(f'{name} 没有设置线虫')
        local_ids = [w.get('id') for w in worms]
        if any(type(w) is not int or w <= 0 for w in local_ids) or len(set(local_ids)) != len(local_ids) or set(ids) & set(local_ids):
            raise ValueError('所有线虫 id 必须是全局唯一的正整数')
        ids.extend(local_ids)
        x, y, x1, y1 = group['crop_xyxy']
        bounds = [0, 0, x1 - x, y1 - y]
        anchors = [dict(frame=group['anchor_frame'], worms=worms)] + group.get('additional_anchors', [])
        if len({a['frame'] for a in anchors}) != len(anchors):
            raise ValueError(f'{name} 包含重复锚帧')
        for anchor in anchors:
            frame(anchor['frame'], f'{name} 锚帧')
            if len({w['id'] for w in anchor['worms']}) != len(anchor['worms']):
                raise ValueError('同一锚帧中有重复线虫 id')
            for worm in anchor['worms']:
                if worm['id'] not in local_ids:
                    raise ValueError('补充锚帧只能引用当前分组内的 id')
                if 'mask_path' in worm:
                    import numpy as np
                    mask_path=Path(worm['mask_path'])
                    if not mask_path.is_absolute():mask_path=relative_to/mask_path
                    if not mask_path.is_file():raise ValueError(f'人工掩膜不存在：{mask_path}')
                    mask=cv2.imdecode(np.frombuffer(mask_path.read_bytes(),np.uint8),cv2.IMREAD_GRAYSCALE)
                    if mask is None or mask.shape!=(bounds[3],bounds[2]) or np.count_nonzero(mask)<20:
                        raise ValueError('人工掩膜尺寸必须与分组裁剪区域一致，且非空')
                    worm['mask_path']=str(mask_path.resolve())
                    continue
                points, labels = worm.get('points', []), worm.get('labels', [])
                if not points or len(points) != len(labels) or 1 not in labels or any(v not in (0, 1) for v in labels):
                    raise ValueError('每条虫需配套的 points/labels，且至少一个正点（1）')
                for point in points + worm.get('guide_path', []):
                    if len(point) != 2 or any(not isinstance(v, (float, int)) or not math.isfinite(v) for v in point) or not (0 <= point[0] < bounds[2] and 0 <= point[1] < bounds[3]):
                        raise ValueError('标记点必须位于所在分组的局部坐标范围内')
                rect(worm.get('box'), bounds, f'虫 {worm["id"]} box')
                if 'guide_path' in worm and (len(worm['guide_path']) < 4 or worm.get('guide_radius', 0) <= 0):
                    raise ValueError('guide_path 至少 4 个点，guide_radius 必须为正')
                if 'mask_index' in worm and worm['mask_index'] not in (0, 1, 2):
                    raise ValueError('mask_index 只能为 0、1、2')
    if c.get('head_detection','twardis') not in ('twardis','manual_only','taper_seed','manual_seed'):
        raise ValueError('head_detection 必须是 taper_seed/manual_seed/twardis/manual_only')
    annotations=c.get('head_annotations',{})
    if not isinstance(annotations,dict) or not set(annotations)<=set(map(str,ids)):
        raise ValueError('头部标注必须使用当前线虫编号')
    from head_tail import normalize_anchors
    for item in [a for values in annotations.values() for a in normalize_anchors(values)]:
        frame(item.get('frame'),'头部标注帧')
        point=item.get('point')
        crop=c['crop_xyxy']
        if (not isinstance(point,list) or len(point)!=2 or
            any(type(v) not in (int,float) or not math.isfinite(v) for v in point) or
            not (0<=point[0]<crop[2]-crop[0] and 0<=point[1]<crop[3]-crop[1])):
            raise ValueError('头部点必须位于分析裁剪画面内')
    for interval in c.get('review_intervals', []):
        if not interval.get('worm_ids') or not set(interval['worm_ids']) <= set(ids) or len(set(interval['worm_ids'])) != len(interval['worm_ids']):
            raise ValueError('复核区间的 worm_ids 必须来自配置中的线虫')
        lo, hi = interval['frames']
        frame(lo, '复核开始帧'); frame(hi, '复核结束帧')
        if lo > hi:
            raise ValueError('复核开始帧不能大于结束帧')
    fit = dict(FIT_DEFAULTS, **c.get('wormtracer', {}))
    if not isinstance(fit['rescale_by_worm'], dict) or not set(fit['rescale_by_worm']) <= set(map(str, ids)):
        raise ValueError('rescale_by_worm 的键必须是配置中的虫编号字符串')
    for key in ('rescale', 'continuity_weight', 'smoothness_weight', 'length_weight'):
        if not isinstance(fit[key], (int, float)) or not math.isfinite(fit[key]) or fit[key] <= 0:
            raise ValueError(f'WormTracer 参数 {key} 必须为正数')
    if fit['rescale'] > 1 or any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 < v <= 1 for v in fit['rescale_by_worm'].values()):
        raise ValueError('缩放比例必须大于 0 且不大于 1')
    c['centerline_method'] = validate_method(c.get('centerline_method','wormtracer'))
    c['sam2_model']=model_spec(c.get('sam2_model','large'))['size']
    if c['centerline_method'] == 'tapnext':
        from tapnext_support import validate_single_worm, resources, dependency_path
        validate_single_worm(len(ids), c['sam2_model'])
        resources()
        dependency_path()
    c['wormtracer'] = fit
    from quality_schema import VERSION as QUALITY_VERSION
    from multiworm_quality import validate_reviews, VERSION as GUARD_VERSION
    validate_reviews(c, ids, n)
    from identity_reassociation import validate_reviews as validate_fragment_reviews
    validate_fragment_reviews(c,ids,n)
    c['quality_schema_version'] = QUALITY_VERSION
    c['quality_rules_version'] = GUARD_VERSION
    return c, dict(frames=n, fps=fps, width=width, height=height, ids=ids)


def build_stages(config, info, snapshot, phenotype_path):
    sample = config['sample']
    base = ROOT / 'outputs' / sample
    tracking = base / 'sam2-tracking'
    stages = []
    def add(name, phase, script, arguments, outputs):
        stages.append(dict(name=name, phase=phase, command=[sys.executable, '-u', str(ROOT / 'src' / script), *map(str, arguments)],
                           outputs=[str(p) for p in outputs]))
    masks = [tracking / 'masks' / f'worm-{w}' / f'{f:06d}.png' for w in info['ids'] for f in range(info['frames'])]
    add('tracking', 'tracking', 'track_configured_video.py', ['--config', snapshot],
        [tracking / 'summary.json', tracking / 'tracking-quality.csv', *masks])
    add('cleanup', 'cleanup', 'clean_tracking_components.py', ['--sample', sample], [tracking / 'component-cleanup.json'])
    add('quality', 'quality', 'quality_stage.py', ['--sample', sample], [tracking / 'quality-guards.json'])
    method = config.get('centerline_method','wormtracer')
    source = method_source(method)
    if method in ('wormtracer','hybrid'):
        for phase, folder, script in [('fit', 'wormtracer-tuned', 'run_wormtracer.py'),
                                       ('repair', 'wormtracer-corrected', 'repair_wormtracer.py'),
                                       ('temporal', 'wormtracer-temporal', 'temporal_wormtracer.py')]:
            for worm in info['ids']:
                args = ['--sample', sample, '--worm', worm]
                if phase == 'fit':
                    fit = config['wormtracer']
                    args += ['--output-name', folder, '--crop-to-track', '--overwrite', '--rescale', fit['rescale_by_worm'].get(str(worm), fit['rescale'])]
                    for key in ('continuity_weight', 'smoothness_weight', 'length_weight'):
                        args += ['--' + key.replace('_', '-'), fit[key]]
                out = base / folder / f'worm-{worm}'
                outputs = [out / 'centerlines.npy', out / 'run.json']
                outputs.append(out / 'quality.json')
                add(f'{phase}-worm-{worm}', phase, script, args, outputs)
    if method in ('topology', 'tapnext'):
        for worm in info['ids']:
            out=base/source/f'worm-{worm}'
            add(f'{method}-worm-{worm}','fit',f'extract_{method}.py',['--sample',sample,'--worm',worm],
                [out/'centerlines.npy',out/'run.json',out/'quality.json'])
    if method in ('niu','hybrid'):
        for worm in info['ids']:
            out=base/source/f'worm-{worm}'
            add(f'{method}-worm-{worm}', 'fit' if method=='niu' else 'temporal',
                'extract_centerlines.py', ['--sample',sample,'--method',method,'--worm',worm],
                [out/'centerlines.npy',out/'run.json',out/'quality.json'])
    results = base / ('phenotypes-'+source)
    add('metrics', 'metrics', 'analyze_phenotypes.py', ['--sample', sample, '--config', phenotype_path,
        '--centerline-method',method,'--wormtracer-source',source],
        [results / name for name in ('summary.json', 'per_frame_metrics.csv', 'worm_summary.csv', 'centerlines.npz',
            'phenotypes-overlay.mp4', '报告.md', 'quality.json', 'per_frame_metrics.json',
            'diagnostic-metrics.json', 'diagnostic-centerlines.npz', 'quality-audit.json','quality-gates.csv',
            'tracklets.json','tracklet-metrics.csv','identity-links.json','display-centerlines.npz')])
    add('export', 'export', 'review_configured_video.py', ['--sample', sample,'--centerline-source',source],
        [results / f'{sample}_annotated_full-frame.mp4', results / 'verification.json'])
    return stages


def compatible_tracking(config, info, base):
    path = base / 'sam2-tracking' / 'summary.json'
    if not path.exists(): return
    old = json.loads(path.read_text(encoding='utf-8'))
    if not tracking_model_matches(old,config.get('sam2_model','large')):
        raise ValueError('已有跟踪结果的 SAM2 模型与当前选择不同，需要重新跟踪')
    equal = (Path(old['video']).resolve() == Path(config['video']) and old['frames'] == info['frames']
             and abs(old['fps'] - info['fps']) < .001 and old['crop_xyxy'] == config['crop_xyxy']
             and old.get('worm_ids') == info['ids'] and old.get('initialization') == config['groups']
             and old.get('review_intervals', []) == config.get('review_intervals', [])
             and old.get('measurement_mode','physical') == config.get('measurement_mode','physical')
             and old.get('scale_um_per_px') == config.get('scale_um_per_px'))
    if not equal:
        raise ValueError('已有分割结果与当前配置不匹配。修改 SAMPLE_NAME 使用新目录，或设置 RESUME=False 全部重算。')


def complete(stage, config, info):
    if not all(Path(p).is_file() and Path(p).stat().st_size for p in stage['outputs']): return False
    if stage['phase']=='tracking':
        try:
            summary=json.loads(Path(stage['outputs'][0]).read_text(encoding='utf-8'))
            if not tracking_model_matches(summary,config.get('sam2_model','large')):return False
        except (OSError,ValueError):return False
    if stage['phase']=='quality':
        from quality_stage import load_guards
        try: load_guards(Path(stage['outputs'][0]).parent, info['ids'], info['frames'], required=True)
        except (OSError, ValueError, KeyError, TypeError): return False
    if stage['phase']=='metrics':
        from head_tail import VERSION
        from quality_schema import VERSION as QUALITY_VERSION
        try:
            report=json.loads(Path(stage['outputs'][0]).read_text(encoding='utf-8'))
            if report.get('head_detection_version')!=VERSION: return False
            from display_centerlines import VERSION as DISPLAY_VERSION
            if report.get('display_version')!=DISPLAY_VERSION:return False
            if report.get('quality_schema_version')!=QUALITY_VERSION: return False
            if report.get('metric_definition_version')!='ppt-20261021-v2': return False
            from local_tracklets import VERSION as TRACKLET_VERSION
            if report.get('tracklet_version')!=TRACKLET_VERSION: return False
            from identity_reassociation import VERSION as ASSOCIATION_VERSION
            if report.get('identity_reassociation_version')!=ASSOCIATION_VERSION:return False
        except (OSError,ValueError): return False
    if stage['phase'] in ('fit', 'repair', 'temporal'):
        import numpy as np
        try:
            try:p = np.load(stage['outputs'][0], allow_pickle=False)
            except (ValueError,EOFError):return False
            if p.shape != (info['frames'], 101, 2): return False
            adapter = stage['name'].startswith(('niu-worm-', 'hybrid-worm-', 'topology-worm-', 'tapnext-worm-'))
            if adapter or len(stage['outputs']) > 2:
                quality=json.loads(Path(stage['outputs'][2]).read_text(encoding='utf-8'))
                if len(quality)!=len(p): return False
                for f,line in enumerate(p):
                    if not np.isfinite(line).all() and not (np.isnan(line).all() and not quality[f]['centerline_valid']): return False
            elif not np.isfinite(p).all(): return False
            meta = json.loads(Path(stage['outputs'][1]).read_text(encoding='utf-8'))
            if stage['name'].startswith('tapnext-worm-'):
                from tapnext_support import signature
                if meta.get('algorithm_signature') != signature(): return False
            if stage['name'].startswith('topology-worm-'):
                from crossing_centerline import VERSION
                if meta.get('version')!=VERSION:return False
            if stage['phase'] == 'fit' and not adapter:
                worm = int(stage['name'].rsplit('-', 1)[1])
                fit = config['wormtracer']
                wanted = dict(rescale=fit['rescale_by_worm'].get(str(worm), fit['rescale']),
                              continuity_loss_weight=fit['continuity_weight'], smoothness_loss_weight=fit['smoothness_weight'],
                              length_loss_weight=fit['length_weight'])
                if any(meta['params'].get(k) != v for k, v in wanted.items()):
                    raise ValueError(f'虫 {worm} 已有拟合参数不同，请换 SAMPLE_NAME 或设置 RESUME=False')
        except (OSError, KeyError): return False
    return True


def save_state(path, state):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def compatible_identity(previous, current):
    """Compare computational inputs; method outputs have separate directories.

    scale_source describes provenance, not the numeric calibration. Reopening
    annotation must not invalidate all tracks because this description changes.
    Mask hashes, scale values, video identity and all other inputs still match.
    """
    def inputs(identity):
        identity = json.loads(json.dumps(identity))
        config = identity.get('config', {})
        config.setdefault('head_detection','twardis')
        config.setdefault('head_annotations',{})
        config.setdefault('identity_confirmations',[])
        config.setdefault('source_invalid_frames',[])
        for key in ('centerline_method', 'scale_source', 'previous_sample'):
            config.pop(key, None)
        return identity
    return inputs(previous) == inputs(current)


def run_stage(stage, log, progress=None):
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
    env['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + env.get('PYTHONPATH', '')
    if progress and progress.event_path: env['WORM_PROGRESS_FILE'] = str(progress.event_path)
    started = time.monotonic()
    with log.open('w', encoding='utf-8') as handle:
        process = subprocess.Popen(stage['command'], cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT)
        try:
            while True:
                if progress and progress.cancel_requested.is_set():
                    raise RuntimeError('用户取消了当前处理；已完成阶段保留，可继续任务')
                try:
                    code = process.wait(timeout=.6 if progress else 30)
                    if progress: progress.poll()
                    break
                except subprocess.TimeoutExpired:
                    if progress: progress.poll()
                    else: print(f'  {stage["name"]} 运行中，{time.monotonic() - started:.0f} 秒；日志：{log.name}', flush=True)
        except BaseException:
            if os.name == 'nt' and process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if process.poll() is None:
                process.terminate()
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            raise
    if code:
        tail = '\n'.join(log.read_text(encoding='utf-8', errors='replace').splitlines()[-12:])
        raise RuntimeError(f'{stage["name"]} 失败（退出码 {code}），查看 {log}\n{tail}')


def _main(progress, raw_override=None):
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'): sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='改用 JSON 视频配置；省略时使用本文件开头设置')
    parser.add_argument('--video', type=Path, help='在标注界面打开指定视频')
    parser.add_argument('--sample', help='指定本次样本名称')
    parser.add_argument('--annotate', action='store_true', help='重新打开人工标注，并从分割开始处理')
    parser.add_argument('--no-resume', action='store_true', help='从分割开始重新计算')
    parser.add_argument('--check', action='store_true', help='只检查配置、视频和 CUDA')
    parser.add_argument('--dry-run', action='store_true', help='检查配置并列出命令，不写入结果')
    parser.add_argument('--no-progress-ui', action='store_true', help='批处理时不启动进度网页，完成后直接退出')
    parser.add_argument('--stop-after', choices=PHASES, default=STOP_AFTER)
    args = parser.parse_args()
    raw = raw_override or (json.loads(args.config.read_text(encoding='utf-8')) if args.config else video_config())
    if raw_override is None:
        if args.video: raw['video'] = str(args.video.resolve())
        if args.sample: raw['sample'] = args.sample
    if not args.dry_run:
        import torch
        if not torch.cuda.is_available(): raise RuntimeError('本流程需要 CUDA，请检查 Conda WormStudio 中的 GPU 版 PyTorch')
        checkpoint=model_spec(raw.get('sam2_model','large'))['checkpoint']
        required = sam2_checkpoint(checkpoint)
        if not required.is_file(): raise RuntimeError(f'缺少 SAM2 模型：{required}；请参阅 docs/INSTALL.md')
        import imageio_ffmpeg
        print(f'Python: {sys.executable}', flush=True)
        print(f'GPU: {torch.cuda.get_device_name()}；FFmpeg: {imageio_ffmpeg.get_ffmpeg_version()}', flush=True)
    resume_saved = raw.pop('_resume_saved', False)
    if (raw_override and not resume_saved) or args.annotate or (not args.config and MANUAL_ANNOTATION and not resume_saved):
        from manual_worm_annotation import collect_annotation
        input_path=Path(raw['video'])
        if not input_path.is_absolute():raw['video']=str((args.config.resolve().parent if args.config else ROOT)/input_path)
        raw=collect_annotation(raw,frame=ANNOTATION_FRAME,reuse=RESUME and not args.no_resume and not args.annotate,
                               check_only=args.check or CHECK_ONLY or args.dry_run,open_browser=OPEN_BROWSER,
                               progress=None if args.no_progress_ui else progress)
        if raw is None:return
    config, info = validate_config(raw, args.config.resolve().parent if args.config else ROOT)
    if not args.dry_run and config['centerline_method'] in ('wormtracer','hybrid'):
        for relative in ('WormTracer/WormTracer19_5/WormTracer/wt.py','WormTracer/WormTracer19_1/WormTracer/config.yaml'):
            try:
                with (RESOURCE_ROOT/relative).open('rb') as handle:handle.read(1)
            except OSError as exc:raise ValueError(f'所选 WormTracer 方法的资源不可读取：{relative}；{exc}') from exc
    phenotype = Path(PHENOTYPE_CONFIG).resolve()
    if not phenotype.is_file(): raise ValueError(f'指标配置不存在：{phenotype}')
    base = ROOT / 'outputs' / config['sample']
    resume = RESUME and not args.no_resume and not args.annotate
    state = {}
    notice = (f'已保存为新的分析任务，旧结果及原始标注保留在 {config["previous_sample"]}。'
              if config.get('previous_sample') else '')
    from run_storage import input_identity, preserve_run
    identity = input_identity(config, phenotype, motion_constraints_path(ROOT))
    if not (args.dry_run or args.check or CHECK_ONLY):
        state_path = base / 'pipeline-state.json'
        reason = ''
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding='utf-8'))
                if not resume: reason = '已选择重新计算'
                elif not compatible_identity(state.get('identity', {}), identity): reason = '输入、掩膜或分析参数已更新'
            except (ValueError, OSError): reason = '旧处理记录无法复用'
        elif (base/'sam2-tracking').exists():
            reason = '旧结果缺少可核对的处理记录'
        if not reason and resume:
            try: compatible_tracking(config, info, base)
            except (ValueError, KeyError): reason = '当前标注与旧跟踪结果不同'
        if reason:
            old_name = config['sample']
            config = preserve_run(config, ROOT/'outputs')
            base = ROOT/'outputs'/config['sample']
            identity = input_identity(config, phenotype, motion_constraints_path(ROOT))
            state = {}; resume = False
            notice = f'{reason}，已自动创建新结果目录并重新计算。旧结果保留在 {old_name}。'
            print(notice, flush=True)
    snapshot = base / 'pipeline-config.json'
    all_stages = build_stages(config, info, snapshot, phenotype)
    stages = [s for s in all_stages if PHASES.index(s['phase']) <= PHASES.index(args.stop_after)]
    print(f'{config["sample"]}: {len(info["ids"])} 条虫，{info["frames"]} 帧，{info["fps"]:g} fps', flush=True)
    if args.dry_run:
        for stage in stages: print(subprocess.list2cmdline(stage['command']))
        return
    if args.check or CHECK_ONLY:
        print('配置、视频、GPU 和编码依赖检查通过。')
        return
    progress.configure(config, info, stages, base, torch.cuda.get_device_name())
    progress.data['notice'] = notice
    if not args.no_progress_ui and progress.server is None:
        import webbrowser
        from manual_worm_annotation import create_server
        progress.bind_server(create_server(progress=progress))
        print(f'处理进度：{progress.url}', flush=True)
        if OPEN_BROWSER: webbrowser.open(progress.url, new=2)
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    state_path = base / 'pipeline-state.json'
    state = state or dict(sample=config['sample'], stages={})
    state.update(fingerprint=fingerprint,identity=identity)
    base.mkdir(parents=True, exist_ok=True)
    logs = base / 'pipeline-logs'; logs.mkdir(exist_ok=True)
    snapshot.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    dirty = not resume
    from cache_integrity import snapshot as output_snapshot, matches as outputs_match
    if dirty:
        for stage in all_stages: state['stages'][stage['name']] = dict(status='pending')
    for index, stage in enumerate(stages):
        if progress.cancel_requested.is_set(): raise RuntimeError('用户取消了当前处理')
        name = stage['name']
        cached = state['stages'].get(name, {}).get('status')
        record=state['stages'].get(name,{})
        if not dirty and cached not in ('pending', 'running', 'failed', 'cancelled') and complete(stage, config, info) and outputs_match(stage,base,record):
            print(f'[{index + 1}/{len(stages)}] 已有结果：{name}', flush=True)
            state['stages'].setdefault(name, {}).update(status='complete', reused=True)
            if 'output_sha256' not in record:
                state['stages'][name].update(output_sha256=output_snapshot(stage,base),integrity_origin='legacy_structural_check_then_snapshot')
            save_state(state_path, state)
            progress.done(index, reused=True)
            continue
        if not dirty:
            for later in all_stages[index:]: state['stages'][later['name']] = dict(status='pending')
        dirty = True
        state['stages'][name] = dict(status='running', command=stage['command'])
        save_state(state_path, state)
        print(f'[{index + 1}/{len(stages)}] 开始：{name}', flush=True)
        started = time.monotonic()
        progress.begin(index, logs / f'{name}.log')
        try:
            run_stage(stage, logs / f'{name}.log', progress)
            if not complete(stage, config, info): raise RuntimeError(f'{name} 输出不完整')
        except BaseException as exc:
            state['stages'][name].update(status='failed', error=str(exc))
            save_state(state_path, state)
            raise
        state['stages'][name].update(status='complete', seconds=round(time.monotonic() - started, 2))
        state['stages'][name].update(output_sha256=output_snapshot(stage,base),integrity_origin='stage_completed')
        if stage['phase']=='cleanup':
            # Cleanup deliberately changes tracking masks: snapshot the resulting cache.
            tracking_stage=next(s for s in all_stages if s['phase']=='tracking')
            state['stages'][tracking_stage['name']]['output_sha256']=output_snapshot(tracking_stage,base)
        save_state(state_path, state)
        progress.done(index)
    state['completed_through'] = args.stop_after
    save_state(state_path, state)
    progress.finish(base, config, args.stop_after)
    if args.stop_after == 'export':
        print(f'处理完成：{base / ("phenotypes-" + method_source(config["centerline_method"])) / (config["sample"] + "_annotated_full-frame.mp4")}', flush=True)
    else:
        print(f'已完成至 {args.stop_after}：{base}', flush=True)


def main():
    from pipeline_progress import PipelineProgress
    progress = PipelineProgress()
    next_settings = None
    try:
        while True:
            error = None
            try:
                _main(progress, next_settings)
            except KeyboardInterrupt:
                progress.fail('用户在终端中断了处理', cancelled=True)
                raise
            except Exception as exc:
                error = exc
                progress.fail(exc)
                print(f'处理停止：{exc}', file=sys.stderr, flush=True)
            next_settings = progress.wait()
            if next_settings is None:
                if error: raise error
                break
    finally: progress.close()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print(f'处理停止：{error}', file=sys.stderr)
        sys.exit(1)
