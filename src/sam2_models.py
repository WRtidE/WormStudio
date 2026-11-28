"""Shared SAM2 model selection for annotation, tracking and cache checks."""
import hashlib
import json
from importlib.util import find_spec

from resource_paths import PROJECT_ROOT, sam2_checkpoint


WORM21 = 'sam2.1-large-worm-v1'


def input_contrast(value):
    if value not in ('none', 'clahe', 'static_background', 'edge_refine'):
        raise ValueError('SAM2 跟踪输入设置无效')
    return value


def model_spec(size='large'):
    if size == WORM21:
        return dict(size='large', label='SAM 2.1 Large 线虫适配 v1',
                    config='configs/sam2.1/sam2.1_hiera_l.yaml', checkpoint='sam2.1_hiera_large_worm_v1.pt',
                    sha256='5d19e7092446c1f71b3f6c7e2d0ea6411319135e51d1c8698c69557b08b7f969')
    if size not in ('large', 'tiny'):
        raise ValueError(f'未知的 SAM2 模型：{size}')
    return dict(size=size, label=f'SAM2 Hiera {size.title()}',
                config=f'configs/sam2/sam2_hiera_{"l" if size=="large" else "t"}.yaml',
                checkpoint=f'sam2_hiera_{size}.pt')


def single_worm_default(root=PROJECT_ROOT):
    path = root / 'config/single_worm_model.local.json'
    if not path.is_file():
        return 'large'
    settings = json.loads(path.read_text(encoding='utf-8-sig'))
    model = settings.get('sam2_model') if isinstance(settings, dict) else None
    if model not in ('large', WORM21):
        raise ValueError(f'{path.name} 的 sam2_model 必须是 large 或 {WORM21}')
    return model


def input_model(config, path=None, *, root=PROJECT_ROOT):
    """Resolve a new input once; snapshots without a model predate the adapter."""
    if 'sam2_model' in config:
        return config['sam2_model']
    if config.get('_resume_saved') or (path and path.name == 'pipeline-config.json' and path.parent.name == config.get('sample')):
        return 'large'
    if sum(len(g.get('worms', [])) for g in config.get('groups', [])) > 1:
        return 'large'
    return single_worm_default(root)


def checkpoint_path(spec, *, verify=False):
    if 'sha256' not in spec:
        return sam2_checkpoint(spec['checkpoint'])
    path = sam2_checkpoint(spec['checkpoint'], override='WORMSTUDIO_SAM21_WORM_CHECKPOINT')
    if verify:
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != spec['sha256']:
                raise ValueError(f'{spec["label"]} 权重 SHA-256 不匹配：{path}')
    return path


def availability(model):
    spec = model_spec(model)
    missing = [name for name in ('cv2','numpy','scipy','skimage','torch','sam2') if find_spec(name) is None]
    if not checkpoint_path(spec).is_file():
        missing.append(spec['label'] + ' 权重')
    return dict(available=not missing, detail='缺少：'+'、'.join(missing) if missing else '分析准备已完成', method='body_sequence')


def tracking_model_matches(summary, size='large'):
    spec=model_spec(size)
    if size == WORM21:
        return (summary.get('sam2_model') == size and summary.get('model') == spec['label']
                and summary.get('checkpoint_sha256') == spec['sha256']
                and summary.get('model_config') == spec['config'] and summary.get('checkpoint') == spec['checkpoint'])
    return (summary.get('model')==spec['label'] and
            summary.get('sam2_model',size)==size and
            summary.get('model_config',spec['config'])==spec['config'] and
            summary.get('checkpoint',spec['checkpoint'])==spec['checkpoint'])
