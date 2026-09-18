"""Package resources are read-only; runtime/config live outside either checkout."""
import json
import os
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
PROTOCOL = 1
LOCATOR = Path(os.environ.get('SHOP_LOCATOR', str(Path.home() / '.config/shop-workstation/bridge.json'))).expanduser()


def locator():
    if not LOCATOR.exists():
        return {}
    data = json.loads(LOCATOR.read_text())
    if data.get('protocol') != PROTOCOL:
        raise RuntimeError('Shop bridge protocol mismatch')
    if Path(data['core_root']).resolve() != PACKAGE:
        raise RuntimeError('Another Shop core owns bridge; use configured core or explicitly configure this checkout')
    return data


BRIDGE = locator()
STATE = Path(os.environ.get('SHOP_STATE_DIR') or BRIDGE.get('state_dir') or
             str(Path.home() / '.local/state/shop-workstation')).expanduser()
CONFIG = Path(os.environ.get('SHOP_CONFIG_DIR') or BRIDGE.get('config_dir') or
              str(Path.home() / '.config/shop-workstation')).expanduser()


THINKING_LEVELS = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
MODEL_SEATS = ('lead', 'lead-2', 'worker', 'worker-2')


def _profile(value, label):
    """Normalize one layer; null thinking explicitly inherits Pi's own default."""
    if isinstance(value, str):
        value = {'model': value}
    if not isinstance(value, dict) or set(value) - {'model', 'thinking'}:
        raise RuntimeError(label + ' must be a model string or {model, thinking} object')
    if 'model' in value:
        model = value['model']
        if (not isinstance(model, str) or len(model) > 512 or '/' not in model or model.startswith('-')
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in model)
                or not all(model.split('/', 1))):
            raise RuntimeError(label + '.model requires a provider/model string (max 512 characters)')
    if 'thinking' in value and value['thinking'] is not None:
        if not isinstance(value['thinking'], str) or value['thinking'] not in THINKING_LEVELS:
            raise RuntimeError(label + '.thinking must be null or one of: ' + ', '.join(THINKING_LEVELS))
    return dict(value)


def _model_layers(data):
    if not isinstance(data, dict) or set(data) - {'defaults', 'lead', 'worker', 'seats'}:
        raise RuntimeError('models.json accepts only defaults, lead, worker and seats; Architect uses Pi /model and /thinking')
    seats = data.get('seats', {})
    if not isinstance(seats, dict) or set(seats) - set(MODEL_SEATS):
        raise RuntimeError('models.json seats accepts only: ' + ', '.join(MODEL_SEATS))
    return ({key: _profile(data.get(key, {}), key) for key in ('defaults', 'lead', 'worker')},
            {key: _profile(value, 'seats.' + key) for key, value in seats.items()})


def models(path=None):
    explicit = path is not None
    if not explicit and (CONFIG / 'settings.json').exists():
        from configuration import read_document, document_models
        document, _ = read_document(CONFIG / 'settings.json')
        if not document:
            raise RuntimeError('Missing Shop settings version')
        return document_models(document)
    path = Path(path).expanduser() if explicit else CONFIG / 'models.json'
    if not explicit and not path.exists():
        return {}
    if path.stat().st_size > 65536:
        raise RuntimeError('Model configuration exceeds 64 KiB')
    data = json.loads(path.read_text())
    _model_layers(data)
    return data


def resolve_models(data):
    """Field-wise precedence: seat > role > defaults. Resolve before creating panes."""
    layers, seats = _model_layers(data)
    result = {}
    for seat in MODEL_SEATS:
        role = seat.split('-')[0]
        profile = {**layers['defaults'], **layers[role], **seats.get(seat, {})}
        if 'model' not in profile:
            raise RuntimeError('Missing model configuration for ' + seat)
        result[seat] = profile
    return result


def role_text(role):
    override = CONFIG / 'roles' / (role + '.md')
    text = (override if override.exists() else PACKAGE / 'roles' / (role + '.md')).read_text()
    return text.replace('{{WORKFLOW}}', str(PACKAGE / 'docs/WORKFLOW.md'))
