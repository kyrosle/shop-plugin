import copy
import json
from pathlib import Path
import tempfile
import unittest
import herdr
import repair
import shutdown as sd
import contracts as ct


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'state.json'
        self.state={'cwd':str(self.root),'tab':'t1','phase':'ready','shop_id':'s1',
                    'architect':{'pane':'p1','name':'s-architect'},
                    'lead':{'pane':'p2','name':'s-lead'},
                    'workers':[{'pane':'p3','name':'s-worker'}]}
        self.caller={'pane_id':'p1','tab_id':'t1','agent':'pi'}
        self.panes=[self.caller];self.calls=[];self.starts=[]
    def api(self,*a):
        self.calls.append(a)
        if a[:2]==('pane','layout'):return {'layout':{'panes':[self.caller],'area':{'width':214,'height':54}}}
        if a[:2]==('workspace','list'):return {'workspaces':[{'workspace_id':'w1'}]}
        if a[:2]==('pane','list'):return {'panes':self.panes}
        if a[:2]==('agent','list'):return {'agents':[]}
        if a[:2]==('pane','split'):return {'pane':{'pane_id':'new'+str(len(self.calls))}}
        if a[:2]==('agent','rename'):return {}
        raise AssertionError(a)
    def plan(self):
        ct.atomic(self.path,self.state)
        return sd.recovery_plan(self.api,self.path,repo=str(self.root),persist=True)

    def run_restore(self,dry=False,plan=None):
        return repair.restore(self.api,lambda *a:self.starts.append(a),ct.atomic,self.path,self.state,self.caller,
                              dry, self.plan() if plan is None else plan)
    def test_incomplete_setup_refuses_before_host_calls_or_state_changes(self):
        cases = [({}, []), (None, []),
                 (self.state['lead'], []), (self.state['lead'], [None]),
                 (self.state['lead'], [{'name': 's-worker'}])]
        for lead, workers in cases:
            for dry in (True, False):
                with self.subTest(lead=lead, workers=workers, dry=dry):
                    state = copy.deepcopy(self.state)
                    state.update(phase='partial', workers=copy.deepcopy(workers),
                                 error='original setup failure')
                    if lead is None:
                        state.pop('lead')
                    else:
                        state['lead'] = copy.deepcopy(lead)
                    before = copy.deepcopy(state)
                    with self.assertRaisesRegex(RuntimeError, 'Incomplete setup registration'):
                        repair.restore(self.api, lambda *a: self.starts.append(a), ct.atomic,
                                       self.path, state, self.caller, dry)
                    self.assertEqual(state, before)
                    self.assertEqual(self.calls, [])
                    self.assertEqual(self.starts, [])
                    self.assertFalse(self.path.exists())

    def test_dry_run_no_mutations(self):
        r=self.run_restore(True)
        self.assertEqual(r['restore'],['s-lead','s-worker'])
        self.assertEqual(self.starts,[]);self.assertFalse((self.root/'repairs').exists())
    def test_restore_preserves_old_evidence_and_starts_in_place(self):
        r=self.run_restore()
        self.assertEqual(len(self.starts),2)
        self.assertEqual(self.state['phase'],'ready')
        self.assertEqual(self.state['architect']['pane'],'p1')
        self.assertEqual(ct.load(r['archive'])['lead']['pane'],'p2')
        self.assertNotEqual(self.state['lead']['pane'],'p2')
        self.assertFalse(any(c[:2]==('agent','prompt') for c in self.calls))
    def test_moved_pane_refused(self):
        self.panes.append({'pane_id':'p2','tab_id':'elsewhere'})
        with self.assertRaises(RuntimeError):self.run_restore()
        self.assertEqual(self.starts,[])
    def test_wrong_caller_refused(self):
        self.caller['pane_id']='other'
        with self.assertRaises(RuntimeError):self.run_restore()

    def test_restore_refuses_unknown_pane_field(self):
        original = self.api
        def api(*a):
            value = original(*a)
            if a[:2] == ('pane', 'list'):
                value['panes'][0] = dict(value['panes'][0], bogus_field=1)
            return value
        with self.assertRaises(herdr.HerdrSchemaError):
            repair.restore(api, lambda *a: self.starts.append(a), ct.atomic, self.path,
                           self.state, self.caller, True, self.plan())
        self.assertEqual(self.starts, [])

    def test_apply_requires_a_matching_persisted_recovery_plan(self):
        def attempt(plan, expected):
            with self.assertRaisesRegex(RuntimeError, expected):
                repair.restore(self.api, lambda *a: self.starts.append(a), ct.atomic, self.path,
                               self.state, self.caller, False, plan)
        attempt(None, 'previewed recovery plan')
        attempt({'decision':'recovery_required','shop_id':'s1'}, 'plan_invalid')
        attempt({'decision':'blocked','shop_id':'s1','schema':1,'plan_id':'x'}, 'plan_invalid')
        attempt({'decision':'blocked','shop_id':'s1','schema':1,'plan_id':'x','plan_digest':'0'*64},
                'plan_not_persisted')
        valid = self.plan()
        tampered = json.loads(json.dumps(valid))
        tampered['decision'] = 'no_action_required'
        tampered['actions'] = [{'kind':'injected'}]
        attempt(tampered, 'plan_tampered')

        # A genuinely stale plan: the state changed after the preview was produced.
        stale_plan = self.plan()
        self.state['workers'].append({'pane':'p9','name':'s-worker-9'})
        attempt(stale_plan, 'plan_stale')
        self.state['workers'].pop()

        # A genuinely expired plan: rewrite the stored plan (matching digest) with a past expiry.
        expired_plan = self.plan()
        expired_plan['expires_at'] = '2000-01-01T00:00:00+00:00'
        expired_plan['plan_digest'] = sd.plan_digest(expired_plan)
        ct.atomic(sd.recovery_plan_file(self.root/'shutdown'/'recovery'/'s1', expired_plan['plan_id'],
                                        expired_plan['plan_id']) if False else
                  Path(expired_plan['plan_path']), expired_plan)
        attempt(expired_plan, 'plan_expired')
        self.assertEqual(self.starts, [])

        # The genuine persisted plan is accepted.
        accepted = self.plan()
        repair.restore(self.api, lambda *a: self.starts.append(a), ct.atomic, self.path,
                       self.state, self.caller, False, accepted)
        self.assertEqual(len(self.starts), 2)


class PendingAddRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.path = self.root / 'state.json'
        self.worker_cwd = self.root / 'worker'; self.worker_cwd.mkdir()
        self.state = {
            'cwd': str(self.root), 'tab': 't1', 'phase': 'partial',
            'error': "{'code': 'agent_pane_busy'}", 'shop_id': 's1',
            'architect': {'pane': 'p1', 'name': 's-architect', 'terminal_id': 'ta'},
            'lead': {'pane': 'p2', 'name': 's-lead', 'terminal_id': 'tl'},
            'workers': [
                {'pane': 'p3', 'name': 's-worker', 'terminal_id': 'tw'},
                {'pane': 'p4', 'name': 's-worker-2', 'cwd': str(self.worker_cwd)},
            ],
        }
        self.caller = {'pane_id': 'p1', 'tab_id': 't1', 'agent': 'pi',
                       'name': 's-architect'}
        self.agents = [
            {'pane_id': 'p1', 'name': 's-architect', 'agent': 'pi',
             'tab_id': 't1', 'terminal_id': 'ta'},
            {'pane_id': 'p2', 'name': 's-lead', 'agent': 'pi',
             'tab_id': 't1', 'terminal_id': 'tl'},
            {'pane_id': 'p3', 'name': 's-worker', 'agent': 'pi',
             'tab_id': 't1', 'terminal_id': 'tw'},
        ]
        self.starts = []

    def api(self, *args):
        if args[:2] == ('pane', 'layout'):
            return {'layout': {'panes': [
                {'pane_id': 'p1'}, {'pane_id': 'p2'},
                {'pane_id': 'p3'}, {'pane_id': 'p4'},
            ], 'area': {'width': 214, 'height': 54}}}
        if args == ('pane', 'get', 'p4'):
            return {'pane': {'pane_id': 'p4', 'tab_id': 't1',
                             'foreground_cwd': str(self.worker_cwd)}}
        if args[:2] == ('agent', 'list'):
            return {'agents': copy.deepcopy(self.agents)}
        if args == ('pane', 'process-info', '--pane', 'p4'):
            return {'process_info': {'shell_pid': 42,
                    'foreground_processes': [{'pid': 42, 'name': 'zsh'}]}}
        raise AssertionError(args)

    def start(self, item, role, state):
        self.starts.append((item['name'], role))
        item['terminal_id'] = 'new-terminal'

    def run_resume(self, dry=False):
        return repair.resume_pending_add(
            self.api, self.start, ct.atomic, self.path,
            self.state, self.caller, dry)

    def test_resume_pending_add_starts_registered_shell_only(self):
        result = self.run_resume()
        self.assertEqual(result['resumed_pending_add'], 's-worker-2')
        self.assertEqual(self.starts, [('s-worker-2', 'worker')])
        self.assertEqual(self.state['phase'], 'ready')
        self.assertNotIn('error', self.state)
        self.assertEqual(ct.load(self.path)['workers'][1]['terminal_id'],
                         'new-terminal')

    def test_dry_run_does_not_start_or_save(self):
        result = self.run_resume(True)
        self.assertEqual(result['would_resume_pending_add'], 's-worker-2')
        self.assertEqual(self.starts, [])
        self.assertFalse(self.path.exists())

    def test_refuses_non_shell_or_wrong_cwd_or_existing_agent(self):
        original_api = self.api
        cases = ['busy', 'cwd', 'agent']
        for case in cases:
            def api(*args, case=case):
                value = original_api(*args)
                if case == 'busy' and args[:2] == ('pane', 'process-info'):
                    value['process_info']['foreground_processes'].append({'pid': 99})
                elif case == 'cwd' and args == ('pane', 'get', 'p4'):
                    value['pane']['foreground_cwd'] = str(self.root)
                elif case == 'agent' and args[:2] == ('agent', 'list'):
                    value['agents'].append({'pane_id': 'p4', 'name': 'other'})
                return value
            with self.subTest(case=case), self.assertRaises(RuntimeError):
                repair.resume_pending_add(
                    api, self.start, ct.atomic, self.path,
                    copy.deepcopy(self.state), self.caller)
        self.assertEqual(self.starts, [])

    def test_refuses_ambiguous_or_wrong_error(self):
        for mutate in ('terminal', 'error'):
            state = copy.deepcopy(self.state)
            if mutate == 'terminal':
                state['workers'][1]['terminal_id'] = 'already'
            else:
                state['error'] = 'some other failure'
            with self.subTest(mutate=mutate), self.assertRaises(RuntimeError):
                repair.resume_pending_add(
                    self.api, self.start, ct.atomic, self.path,
                    state, self.caller)

    def test_pane_name_is_not_identity_and_is_dropped(self):
        original = self.api
        def with_hostile_name(*args):
            value = original(*args)
            if args == ('pane', 'get', 'p4'):
                value['pane']['name'] = 'hostile-pane-name'
            return value
        result = repair.resume_pending_add(with_hostile_name, self.start, ct.atomic, self.path,
                                           copy.deepcopy(self.state), self.caller)
        self.assertEqual(result['resumed_pending_add'], 's-worker-2')
        self.assertEqual(self.starts, [('s-worker-2', 'worker')])

    def test_unknown_pane_field_fails_closed(self):
        original = self.api
        def with_typo(*args):
            value = original(*args)
            if args == ('pane', 'get', 'p4'):
                value['pane']['agent_name'] = 'guessed'
            return value
        with self.assertRaises(herdr.HerdrSchemaError):
            repair.resume_pending_add(with_typo, self.start, ct.atomic, self.path,
                                      copy.deepcopy(self.state), self.caller)
        self.assertEqual(self.starts, [])

    def test_agent_entry_without_name_is_refused(self):
        original = self.api
        def api(*args):
            value = original(*args)
            if args[:2] == ('agent', 'list'):
                for entry in value['agents']:
                    if entry['pane_id'] == 'p3':
                        entry.pop('name')
            return value
        with self.assertRaises(RuntimeError):
            repair.resume_pending_add(api, self.start, ct.atomic, self.path,
                                      copy.deepcopy(self.state), self.caller)
        self.assertEqual(self.starts, [])


if __name__=='__main__':unittest.main()
