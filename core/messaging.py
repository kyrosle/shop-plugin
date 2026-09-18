"""Light same-shop communication; no task creation, files, retry, or interruption."""
import json
import uuid
import coordination as co
import identity
import transport


def send(api, state, caller, target, text, allow_busy=False):
    if state.get('phase') != 'ready':
        raise RuntimeError('Ready shop required')
    co.binding(state)
    repo, rid = state['cwd'], state['run_id']
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise RuntimeError('Message must contain 1..4000 characters')
    roster = identity.roster(state)
    source = next((m for _, m in roster if m['pane'] == caller['pane_id']), None)
    candidates = [(r, m) for r, m in roster if m['name'] == target]
    if not candidates:
        candidates = [(r, m) for r, m in roster if r == target]
    if not source or len(candidates) != 1:
        raise RuntimeError('Sender not registered or target ambiguous; use exact member name from status')
    role, recipient = candidates[0]
    if source['pane'] == recipient['pane']:
        raise RuntimeError('Cannot message self')
    envelope = {'protocol': 1, 'message_id': uuid.uuid4().hex, 'shop_id': state['shop_id'],
                'run_id': state['run_id'], 'sender': source['name'], 'sender_launch_id': source.get('launch_id'),
                'recipient': recipient['name'], 'recipient_role': role,
                'recipient_launch_id': recipient.get('launch_id')}
    identity.validate_route(state, envelope, recipient['name'])
    for member in (source, recipient):
        live = api('agent', 'get', member['pane'])['agent']
        if (live.get('name') != member['name'] or live.get('agent') != 'pi' or live.get('tab_id') != state['tab']
                or not member.get('terminal_id') or live.get('terminal_id') != member['terminal_id']):
            raise RuntimeError('Runtime identity changed; message not sent')
        if member is recipient:
            status = live.get('agent_status')
            if status == 'working' and not allow_busy:
                return {'submitted': False, 'reason': 'recipient_working', 'retry_automatically': False,
                        'note': 'Use allow_busy only for explicit mid-task communication. It is not guaranteed steering or cancellation.'}
            if status not in ('idle', 'done', 'working'):
                raise RuntimeError('Recipient blocked/unknown; inspect before messaging')
    request = transport.prepare(repo, rid, state, source, recipient, 'note', text)
    return {'submitted': False, 'message_id': request['message_id'], 'recipient': recipient['name'],
            'acknowledged': False, 'tickets_created': 0, 'transport_request': request,
            'note': 'Prepared only. Calling Pi must submit via built-in transport; no prompt fallback.'}
