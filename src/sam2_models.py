"""Shared SAM2 model selection for annotation, tracking and cache checks."""


def model_spec(size='large'):
    if size not in ('large', 'tiny'):
        raise ValueError('sam2_model 必须是 large 或 tiny')
    return dict(size=size, label=f'SAM2 Hiera {size.title()}',
                config=f'configs/sam2/sam2_hiera_{"l" if size=="large" else "t"}.yaml',
                checkpoint=f'sam2_hiera_{size}.pt')


def tracking_model_matches(summary, size='large'):
    spec=model_spec(size)
    return (summary.get('model')==spec['label'] and
            summary.get('sam2_model',size)==size and
            summary.get('model_config',spec['config'])==spec['config'] and
            summary.get('checkpoint',spec['checkpoint'])==spec['checkpoint'])
