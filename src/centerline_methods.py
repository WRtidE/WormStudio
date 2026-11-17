"""Internal routing and legacy result compatibility; Studio has one current profile."""
CURRENT_METHOD = 'body_sequence'
SELF_CONTACT_VERSION = 'self-contact-101-v3'
CURRENT_HEAD_DETECTION = 'taper_seed'

METHODS = {
    'body_sequence': {'label': 'SAM2 + 多帧身体连续跟踪', 'source': 'centerlines-body-sequence'},
    'self_contact': {'label': '全身中线与交叠复核', 'source': 'centerlines-self-contact'},
    'tapnext': {'label': 'SAM2 + TAPNext++（单虫）', 'source': 'centerlines-tapnext'},
    'topology': {'label': '自交保守模式', 'source': 'centerlines-topology'},
    'wormtracer': {'label': 'WormTracer', 'source': 'wormtracer-temporal'},
    'niu': {'label': 'NIU', 'source': 'centerlines-niu'},
    'hybrid': {'label': 'WormTracer + NIU', 'source': 'centerlines-hybrid'},
}


def validate_method(value):
    if not isinstance(value, str) or value not in METHODS:
        raise ValueError(f'未知的中线方法：{value}')
    return value


def method_source(value):
    return METHODS[validate_method(value)]['source']
