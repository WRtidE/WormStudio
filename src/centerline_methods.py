"""Internal routing and legacy result compatibility; Studio has one current profile."""
CURRENT_METHOD = 'tapnext'
CURRENT_HEAD_DETECTION = 'taper_seed'

METHODS = {
    'tapnext': {'label': 'SAM2 + TAPNext++（单虫）', 'source': 'centerlines-tapnext'},
    'topology': {'label': '自交保守模式', 'source': 'centerlines-topology'},
    'wormtracer': {'label': 'WormTracer', 'source': 'wormtracer-temporal'},
    'niu': {'label': 'NIU', 'source': 'centerlines-niu'},
    'hybrid': {'label': 'WormTracer + NIU', 'source': 'centerlines-hybrid'},
}


def validate_method(value):
    if not isinstance(value, str) or value not in METHODS:
        raise ValueError('中线方法必须为 tapnext、topology、wormtracer、niu 或 hybrid')
    return value


def method_source(value):
    return METHODS[validate_method(value)]['source']
