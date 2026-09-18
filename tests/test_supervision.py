import copy
from pathlib import Path
import unittest
from unittest.mock import patch
import contracts as ct
import coordination as co
import supervision as su
import test_contracts as fixtures


class SupervisionTests(unittest.TestCase):
    setUp = fixtures.ContractTests.setUp
    api = fixtures.ContractTests.api
    bind = fixtures.ContractTests.bind
    dispatch = fixtures.ContractTests.dispatch
    def test_patrol_snapshot_no_effects(self):
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc);self.dispatch()
        self.calls=[]
        report=su.patrol(self.api,self.sp,{'pane_id':'p2'},0)
        self.assertEqual(report['capacity']['worker'],1)
        self.assertFalse(any(a[:2] in [('agent','prompt'),('agent','send-keys'),('pane','close')] for a in self.calls))

    def test_patrol_refreshes_dynamic_membership(self):
        self.bind()
        def add(_):
            state=ct.load(self.sp)
            state['extra_leads']=[{'name':'s-lead-2','pane':'p4'}]
            state['workers'].append({'name':'s-worker-2','pane':'p5'})
            for p,n in [('p4','s-lead-2'),('p5','s-worker-2')]:
                self.live[p]=dict(self.live['p3'],name=n,pane_id=p)
            ct.atomic(self.sp,state)
        with patch.object(su.time,'sleep',add):
            report=su.patrol(self.api,self.sp,{'pane_id':'p2'},60)
        self.assertEqual(report['reason'],'changed')
        self.assertEqual(report['capacity'],{'lead':2,'worker':2,'max_each':2})
        self.assertTrue(report['members'][0]['removable_candidate'])
        def remove(_):
            state=ct.load(self.sp);state['extra_leads']=[];state['workers']=state['workers'][:1];ct.atomic(self.sp,state)
        with patch.object(su.time,'sleep',remove):
            report=su.patrol(self.api,self.sp,{'pane_id':'p2'},60)
        self.assertEqual(len(report['members']),1)

    def test_interval_bounded_and_membership_changes_stop_patrol(self):
        self.bind()
        with self.assertRaises(RuntimeError):su.patrol(self.api,self.sp,{'pane_id':'p2'},121)
        with self.assertRaises(RuntimeError):su.patrol(self.api,self.sp,{'pane_id':'p3'},0)
        def change(_):
            state=ct.load(self.sp);state['phase']='creating';ct.atomic(self.sp,state)
        with patch.object(su.time,'sleep',change):
            report=su.patrol(self.api,self.sp,{'pane_id':'p2'},60)
        self.assertEqual(report['reason'],'membership_operation_in_progress')

    def test_pause_sends_only_esc_and_records_uncertainty(self):
        self.bind();self.live['p3']['agent_status']='working'
        calls=[]
        def api(*a):
            if a[:2]==('agent','send-keys'): calls.append(a);return {}
            return self.api(*a)
        r=su.pause(api,self.state,{'pane_id':'p2'},'s-worker','wrong source directory observed')
        self.assertEqual(calls,[('agent','send-keys','s-worker','esc')])
        self.assertEqual(r['status'],'esc_sent_not_confirmed_stopped')
        self.live['p3']['agent_status']='blocked'
        with self.assertRaises(RuntimeError):su.pause(api,self.state,{'pane_id':'p2'},'s-worker','slow')
        with self.assertRaises(RuntimeError):su.pause(api,self.state,{'pane_id':'p2'},'s-lead','reason')

    def test_revision_reassignment_requires_ready_attempt(self):
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc);self.dispatch()
        with self.assertRaises(RuntimeError):ct.revise(self.repo,self.rid,'T1',{'owner':'s-worker-2'})
        ct.retry(self.repo,self.rid,'T1',True)
        t=ct.revise(self.repo,self.rid,'T1',{'owner':'s-worker-2','objective':'narrowed task'})
        self.assertEqual(t['attempt'],2)
        self.assertEqual(t['owner'],'s-worker-2')
        self.assertEqual(t['status'],'ready')
        with self.assertRaises(RuntimeError):ct.revise(self.repo,self.rid,'T1',{'attempt':1})

    def test_snapshot_exposes_member_identity_mapping_without_transport(self):
        self.bind()
        report=su.patrol(self.api,self.sp,{'pane_id':'p2'},0)
        row=report['members'][0]
        self.assertEqual(row['identity']['pane'],row['pane'])
        self.assertIsNone(row['identity']['transport_epoch'])
        self.assertFalse(row['identity']['transport_present'])
        self.assertEqual(row['identity']['launch_id'],'launch-p3')
        self.assertEqual(report['schema'],'shop.snapshot/v1')
        self.assertEqual(report['scope']['kind'],'execution-members')
        self.assertEqual(row['observed']['status'],row['status'])

    def test_snapshot_refuses_pretend_transport_instead_of_crashing(self):
        self.bind()
        state=ct.load(self.sp)
        state['workers'][0]['transport_epoch']=1
        ct.atomic(self.sp,state)
        report=su.patrol(self.api,self.sp,{'pane_id':'p2'},0)
        self.assertEqual(report['members'][0]['status'],'identity_refused')
        # The refusal is rendered explicitly (never displayed as healthy) with the reason kept.
        self.assertIn('refused',report['members'][0]['identity'])
        self.assertIn('transport epoch',report['members'][0]['identity']['refused'])
        self.assertFalse(report['members'][0]['identity']['transport_present'])
        self.assertFalse(report['members'][0]['removable_candidate'])

    def test_patrol_and_full_snapshot_share_one_schema_and_producer(self):
        import snapshot as snapshot_module
        self.bind()
        ct.new_ticket(self.repo, self.rid, self.doc)
        ticket = self.dispatch()
        ct.atomic(ct.attempt_path(self.repo, ticket, 'checkpoint'),
                  {'run_id': self.rid, 'ticket_id': 'T1', 'attempt': 1, 'owner': 's-worker',
                   'base_commit': None, 'progress': 'reading sources', 'next_steps': ['inspect imports'],
                   'saved_at': ct.stamp()})
        report = su.patrol(self.api, self.sp, {'pane_id': 'p2'}, 0)
        state = ct.load(self.sp)
        full = snapshot_module.build(state, ct.tickets(self.repo, self.rid), api=self.api,
                                     repo=self.repo, run_id=self.rid)
        self.assertEqual(report['schema'], full['schema'])
        self.assertEqual(report['schema'], snapshot_module.SCHEMA)
        self.assertEqual(report['limits'], full['limits'])
        self.assertEqual(report['redaction'], full['redaction'])
        self.assertEqual(report['scope']['kind'], 'execution-members')
        self.assertEqual(full['scope']['kind'], 'all-members')
        # Patrol keeps its existing per-ticket paths (only additive fields).
        entry = report['members'][0]['tickets'][0]
        for key in ('ticket_id', 'attempt', 'status', 'delivery', 'checkpoint', 'inspect_output'):
            self.assertIn(key, entry)
        self.assertIsInstance(entry['checkpoint']['age_seconds'], int)

    def test_dispatch_refuses_member_with_pretend_transport(self):
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc)
        state=ct.load(self.sp)
        state['workers'][0]['transport_epoch']=1
        ct.atomic(self.sp,state)
        with self.assertRaises(RuntimeError):
            co.dispatch(self.api,state,{'pane_id':'p2'},'T1')
        self.assertEqual(ct.load(ct.ticket_path(self.repo,self.rid,'T1'))['status'],'ready')

    def test_pause_refuses_member_with_pretend_transport(self):
        self.bind()
        self.live['p3']['agent_status']='working'
        state=ct.load(self.sp)
        state['workers'][0]['transport_epoch']=1
        ct.atomic(self.sp,state)
        with self.assertRaises(RuntimeError):
            su.pause(self.api,state,{'pane_id':'p2'},'s-worker','wrong source directory observed')


if __name__=='__main__':unittest.main()
