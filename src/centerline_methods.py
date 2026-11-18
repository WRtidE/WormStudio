"""One current analysis profile plus read-only labels for archived result folders.

Earlier releases shipped six other centerline methods. Their results can still be
opened, exported and re-viewed, but this build cannot recompute them: the engines
were removed so the current path has no CUDA-only or duplicated implementation.
"""

CURRENT_METHOD = 'body_sequence'
CURRENT_HEAD_DETECTION = 'taper_seed'

METHODS = {
    'body_sequence': {'label': 'SAM2 + 多帧身体连续跟踪', 'source': 'centerlines-body-sequence'},
}

# Read-only: keeps historical result folders addressable by their original name.
ARCHIVED = {
    'tapnext': {'label': 'SAM2 + TAPNext++（单虫）', 'source': 'centerlines-tapnext'},
    'self_contact': {'label': '全身中线与交叠复核', 'source': 'centerlines-self-contact'},
    'topology': {'label': '自交保守模式', 'source': 'centerlines-topology'},
    'wormtracer': {'label': 'WormTracer', 'source': 'wormtracer-temporal'},
    'niu': {'label': 'NIU', 'source': 'centerlines-niu'},
    'hybrid': {'label': 'WormTracer + NIU', 'source': 'centerlines-hybrid'},
}


def analysis_method(value):
    """Translate a recorded method into a computable one, or refuse explicitly."""
    if value is None:
        return CURRENT_METHOD
    if value == 'tapnext':
        # TAPNext++ was retired in 1.0.0; its tasks continue with the current method.
        return CURRENT_METHOD
    if value in METHODS:
        return value
    if value in ARCHIVED:
        raise ValueError(f'“{ARCHIVED[value]["label"]}”已归档，只能查看历史结果，不能重新计算；'
                         '如需新结果请从原视频新建任务')
    raise ValueError(f'未知的中线方法：{value}')


def display_method(value):
    """Map any recorded method name to its label/source for read-only views."""
    if value in METHODS:
        return METHODS[value]
    if value in ARCHIVED:
        return ARCHIVED[value]
    raise ValueError(f'未知的中线方法：{value}')


def method_source(value):
    return display_method(value)['source']
