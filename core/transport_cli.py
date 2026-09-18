#!/usr/bin/env python3
"""Shop transport CLI used by the Pi extension (no subprocess, no transport I/O).

Commands:
  receive  --repo <main> --run <run> --state <state.json> --self <member> --file <envelope.json>
  receipt  --repo <main> --run <run> --message-id <id> --status <s> [--detail <text>]
  handoff  --repo <main> --run <run> --action <a> --handoff-id <id> [--ticket <id>] [--summary <t>]
  status   --repo <main> --run <run>
  pending  --repo <main> --run <run>

`receive` never injects anything: it verifies identity, records durably and
prints the decision JSON (inject / duplicate / reject). The caller must not
inject when the decision is not `inject`, and must never re-inject `received`
or `unknown` records.
"""
from language import ArgumentParser, t
import json
import sys

import contracts as ct
import handoff as ho
import transport as tr


def emit(value, code=0):
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return code


def main(argv=None):
    parser = ArgumentParser(description=t(__doc__))
    parser.add_argument('--repo', required=True, help=t('Main repository root (absolute)'))
    parser.add_argument('--run', required=True, help=t('Bound run id'))
    sub = parser.add_subparsers(dest='command', required=True)

    receive = sub.add_parser('receive')
    receive.add_argument('--state', required=True, help=t('Workstation state JSON file'))
    receive.add_argument('--self', required=True, help=t('Receiving member name'))
    receive.add_argument('--self-pane', default=None, help=t('Receiving pane id for cross-check'))
    receive.add_argument('--file', required=True, help=t('Business envelope JSON file'))

    receipt = sub.add_parser('receipt')
    receipt.add_argument('--message-id', required=True)
    receipt.add_argument('--status', required=True,
                         choices=['receiver_received', 'injected', 'rejected', 'unknown'])
    receipt.add_argument('--detail', default=None)

    outgoing = sub.add_parser('outgoing')
    outgoing.add_argument('--state', required=True)
    outgoing.add_argument('--self', required=True)
    outgoing.add_argument('--message-id', required=True)
    outgoing.add_argument('--status', required=True)

    handoff = sub.add_parser('handoff')
    handoff.add_argument('--action', required=True, choices=sorted(ho.ACTIONS))
    handoff.add_argument('--handoff-id', required=True)
    handoff.add_argument('--ticket', default=None)
    handoff.add_argument('--message-id', default=None)
    handoff.add_argument('--summary', default='')
    handoff.add_argument('--detail', default=None)
    handoff.add_argument('--actor', default=None)
    handoff.add_argument('--state', default=None)

    sub.add_parser('status')
    sub.add_parser('pending')

    args = parser.parse_args(argv)

    if args.command == 'receive':
        envelope = ct.load(args.file)
        state = ct.load(args.state)
        if state.get('run_id') != args.run:
            raise tr.TransportReject('E_RUN_MISMATCH', 'state file is not bound to run ' + str(args.run))
        try:
            decision = tr.decide(args.repo, args.run, state, envelope, args.self, args.self_pane)
        except tr.TransportReject as reject:
            return emit({'decision': 'reject', 'code': reject.code, 'detail': reject.detail,
                         'message_id': envelope.get('message_id')}, 0)
        return emit(decision, 0)

    if args.command == 'receipt':
        record = tr.record_receipt(args.repo, args.run, args.message_id, args.status, args.detail)
        return emit({'message_id': args.message_id, 'state': record.get('state')}, 0)

    if args.command == 'outgoing':
        import os
        import shop
        from workbench import Workbench
        workbench = Workbench(shop.api, args.state, os.environ.get('HERDR_ACTIVE_PANE_ID') or os.environ.get('HERDR_PANE_ID'))
        import coordination as co
        with co.repo_lock(workbench.repo):
            workbench.authorize()
            if (workbench.actor['name'] != args.self or workbench.rid != args.run
                    or __import__('pathlib').Path(workbench.repo).resolve() != __import__('pathlib').Path(args.repo).resolve()):
                raise tr.TransportReject('E_RUN_MISMATCH', 'outgoing caller or bound repository mismatch')
            record = tr.outgoing(args.repo, args.run, args.message_id, args.status, workbench.actor)
            for ticket in ct.tickets(args.repo, args.run):
                if ticket.get('dispatch', {}).get('message_id') == args.message_id:
                    ticket['dispatch'].update(delivery=record['submission'], receipt=record['receipt'])
                    ct.atomic(ct.ticket_path(args.repo, args.run, ticket['ticket_id']), ticket)
            return emit(record)

    if args.command == 'handoff':
        if not args.state or args.actor or args.action == 'propose':
            raise ho.HandoffError('Use /shop-ui or shop_handoff with verified member identity; free-form --actor is refused')
        import os
        import shop
        import coordination as co
        from pathlib import Path
        from workbench import Workbench
        workbench = Workbench(shop.api, args.state, os.environ.get('HERDR_ACTIVE_PANE_ID') or os.environ.get('HERDR_PANE_ID'))
        if workbench.rid != args.run or Path(workbench.repo).resolve() != Path(args.repo).resolve():
            raise ho.HandoffError('Bound repository/run mismatch')
        with co.repo_lock(workbench.repo):
            return emit(workbench.execute({'action': 'handoff-transition', 'id': args.handoff_id,
                                          'transition': args.action, 'detail': args.detail}), 0)

    if args.command == 'status':
        return emit(tr.status(args.repo, args.run), 0)

    if args.command == 'pending':
        return emit({'run_id': args.run, 'unresolved': tr.unresolved(args.repo, args.run)}, 0)

    parser.error('unreachable')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (tr.TransportReject, ho.HandoffError) as error:
        print('shop-transport: ' + str(error), file=sys.stderr)
        sys.exit(2)
    except Exception as error:  # noqa: BLE001 - CLI boundary: report, never fake success
        print('shop-transport: ' + str(error), file=sys.stderr)
        sys.exit(1)
