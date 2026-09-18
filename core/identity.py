"""Observed health, member identity/epoch mapping and same-shop route validation.

No transport and no auto-repair. Two deliberately different levels exist:

* ``observe`` / ``health`` are diagnostic. They may report ``unknown`` and never
  authorize anything.
* ``resolve`` is the authorization gate used before dispatch, bind, message,
  removal, pause and recovery. It fails closed on missing/unknown identity or
  status and refuses members that pretend a transport epoch exists.

The pane endpoint is never authoritative for an agent name: Herdr's pane schema
has no ``name`` field, so the name always comes from the agent endpoint.
"""
import herdr

MEMBER_FIELDS = ('name', 'pane', 'launch_id', 'terminal_id', 'session_dir',
                 'transport_epoch', 'session_source')


def roster(state):
    return [('architect', state['architect']), ('lead', state['lead']),
            *[('auxiliary_lead', x) for x in state.get('extra_leads', [])],
            *[('worker', x) for x in state.get('workers', [])]]


def member_view(member):
    """Read-only mapping of one Shop member's identity/epoch fields.

    Persistent registration never owns runtime transport epochs. Null means
    unknown here; the ephemeral Pi endpoint registry reports current sessions.
    """
    if member.get('transport_epoch') is not None or member.get('transport') not in (None, False):
        raise herdr.HerdrIdentityError('member ' + str(member.get('name'))
                                       + ' carries a transport epoch in persistent registration; inspect live endpoint registry')
    return {'name': member.get('name'), 'pane': member.get('pane'),
            'launch_id': member.get('launch_id'), 'terminal_id': member.get('terminal_id'),
            'session_dir': member.get('session_dir'), 'session_source': member.get('session_source'),
            'transport': None, 'transport_epoch': None, 'transport_present': None}


def assign_identity(item, session_dir=None, launch_id=None, terminal_id=None, session_source=None):
    """Persist identity/epoch fields on a member record.

    Refuses persistent transport epochs: only the live Pi endpoint owns them.
    """
    if item.get('transport_epoch') is not None or item.get('transport') not in (None, False):
        raise herdr.HerdrIdentityError('refusing to overwrite a member that carries a transport epoch')
    for key, value in (('session_dir', session_dir), ('launch_id', launch_id),
                       ('terminal_id', terminal_id), ('session_source', session_source)):
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise herdr.HerdrSchemaError('member identity field ' + key + ' must be a nonempty string')
        item[key] = value
    item['transport'] = None
    item['transport_epoch'] = None
    return item


def observed_pane(api, pane_id, where='pane'):
    return herdr.pane_record(api('pane', 'get', pane_id)['pane'], where)


def observed_agent(api, target, where='agent'):
    return herdr.agent_record(api('agent', 'get', target)['agent'], where)


def _refuse_fake_transport(member, where):
    member_view(member)


def resolve(api, member, tab, allowed_status=herdr.SETTLED_STATUSES,
            require_terminal=False, require_status=True, where=None):
    """Strict, fail-closed identity resolution for any authorization decision."""
    where = where or ('member ' + str(member.get('name')))
    pane_id = herdr.require_text(member, 'pane', where)
    _refuse_fake_transport(member, where)
    pane = observed_pane(api, pane_id, where + ' pane')
    if pane.get('tab_id') != tab:
        raise herdr.HerdrIdentityError(where + ': pane is not in the registered tab')
    if pane.get('agent') is None:
        raise herdr.HerdrIdentityError(where + ': pane holds no agent')
    agent = observed_agent(api, pane_id, where + ' agent')
    if agent.get('pane_id') != pane_id:
        raise herdr.HerdrIdentityError(where + ': agent reports a different pane')
    if agent.get('tab_id') != tab:
        raise herdr.HerdrIdentityError(where + ': agent moved to another tab')
    if agent.get('agent') != 'pi':
        raise herdr.HerdrIdentityError(where + ': pane agent kind is not pi')
    name = agent.get('name')
    if not name or name != member.get('name'):
        raise herdr.HerdrIdentityError(where + ': agent name mismatch ('
                                       + repr(name) + ' != ' + repr(member.get('name')) + ')')
    terminal = agent.get('terminal_id')
    if member.get('terminal_id'):
        if not terminal or terminal != member['terminal_id']:
            raise herdr.HerdrIdentityError(where + ': terminal identity changed; old writer is not this occupant')
    if require_terminal and not terminal:
        raise herdr.HerdrIdentityError(where + ': occupant has no terminal identity')
    status = herdr.require_status(agent, allowed_status if require_status else None, where)
    return {'name': name, 'pane': pane_id, 'tab_id': agent.get('tab_id'),
            'terminal_id': terminal, 'status': status, 'agent': agent, 'pane_record': pane}


def health(api, state, member, pane=None):
    """Diagnostic health for one member. Never used to authorize an action.

    ``pane`` may be supplied by a caller that already listed panes; otherwise the
    pane endpoint is read. Missing/unreadable endpoints report ``unknown``.
    """
    try:
        if pane is None:
            pane = observed_pane(api, member['pane'], 'member ' + str(member.get('name')))
    except Exception:
        return 'unknown'
    if pane.get('tab_id') != state.get('tab'):
        return 'moved'
    try:
        agent = observed_agent(api, member['pane'], 'member ' + str(member.get('name')))
    except Exception:
        return 'unknown'
    name = agent.get('name')
    if not name:
        return 'unknown'
    if name != member.get('name') or agent.get('agent') != 'pi':
        return 'identity_mismatch'
    if member.get('terminal_id') and agent.get('terminal_id') != member['terminal_id']:
        return 'identity_mismatch'
    return 'present'


def observe(api, state):
    rows = []
    panes = []
    for workspace in api('workspace', 'list')['workspaces']:
        panes.extend(api('pane', 'list', '--workspace', workspace['workspace_id'])['panes'])
    for role, member in roster(state):
        live = next((p for p in panes if p['pane_id'] == member['pane']), None)
        if not live:
            state_name = 'missing'
        else:
            state_name = health(api, state, member, live)
        rows.append({'role': role, 'name': member['name'], 'pane': member['pane'],
                     'identity': member_view(member), 'health': state_name})
    return {'health': 'healthy' if all(r['health'] == 'present' for r in rows) else 'degraded',
            'members': rows, 'note': 'Missing pane is not proof that all background writers stopped. '
                                     'Health is diagnostic; authorization uses resolve().'}


def build_envelope(state, member, role, ticket_id, attempt):
    """Protocol-1 ticket envelope. Carries the launch epoch that is actually
    registered; runtime transport epochs live in a separate endpoint registry."""
    view = member_view(member)
    return {'protocol': 1, 'shop_id': state['shop_id'], 'run_id': state['run_id'],
            'sender': state['lead']['name'], 'sender_launch_id': state['lead'].get('launch_id'),
            'recipient': view['name'], 'recipient_role': role,
            'recipient_launch_id': view['launch_id'],
            'recipient_terminal_id': view['terminal_id'],
            'ticket_id': ticket_id, 'attempt': attempt}


def validate_route(state, envelope, recipient_name):
    if envelope.get('protocol') != 1 or not state.get('run_id'):
        raise herdr.HerdrIdentityError('Bound protocol-1 route required')
    if envelope.get('transport_epoch') is not None or envelope.get('transport') is not None:
        raise herdr.HerdrIdentityError('Legacy ticket route carries a transport epoch; use live endpoint discovery')
    for key in ('shop_id', 'run_id'):
        if envelope.get(key) != state.get(key):
            raise herdr.HerdrIdentityError('Route mismatch: ' + key)
    sender = next((m for _, m in roster(state) if m['name'] == envelope.get('sender')), None)
    target = next(((r, m) for r, m in roster(state) if m['name'] == recipient_name), None)
    if not sender or not sender.get('launch_id') or sender['launch_id'] != envelope.get('sender_launch_id'):
        raise herdr.HerdrIdentityError('Stale or unknown sender launch')
    if not target or target[0] != envelope.get('recipient_role') or envelope.get('recipient') != recipient_name:
        raise herdr.HerdrIdentityError('Wrong recipient')
    if not target[1].get('launch_id') or target[1]['launch_id'] != envelope.get('recipient_launch_id'):
        raise herdr.HerdrIdentityError('Stale recipient launch')
    for role, member in (('sender', sender), ('recipient', target[1])):
        viewed = member_view(member)
        if not viewed.get('pane'):
            raise herdr.HerdrIdentityError('Registered ' + role + ' lacks a pane identity')
        if envelope.get(role + '_terminal_id') is not None:
            if envelope[role + '_terminal_id'] != viewed.get('terminal_id'):
                raise herdr.HerdrIdentityError('Route ' + role + ' terminal epoch mismatch')
        if envelope.get(role + '_session_dir') is not None:
            if envelope[role + '_session_dir'] != viewed.get('session_dir'):
                raise herdr.HerdrIdentityError('Route ' + role + ' session epoch mismatch')
    return target[1]
