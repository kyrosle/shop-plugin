import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import contracts as ct
import coordination as co
import run


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.r = run.Runs(self.repo)
        self.rid = self.r.new('test')['created']
        self.state = {'shop_id':'shop1', 'tab':'t1','cwd':str(self.repo),'phase':'ready',
                      'architect':{'name':'s-architect','pane':'p1'},
                      'lead':{'name':'s-lead','pane':'p2'},
                      'workers':[{'name':'s-worker','pane':'p3','cwd':str(self.repo)}]}
        self.sp = self.repo/'shop-state.json'
        self.calls=[]
        self.live={p: {'name':n, 'agent':'pi','tab_id':'t1','pane_id':p,
                      'agent_status':'idle','terminal_id':'term-'+p}
                   for p,n in [('p1','s-architect'),('p2','s-lead'),('p3','s-worker')]}
        for member in [self.state['architect'], self.state['lead'], *self.state['workers']]:
            member.update(launch_id='launch-' + member['pane'], terminal_id='term-' + member['pane'])
        self.doc={'ticket_id':'T1','owner':'s-worker','objective':'inspect', 'kind':'analysis',
                  'worktree':str(self.repo),'scope':['src'],'checks':[],'depends_on':[]}

    def api(self,*a):
        self.calls.append(a)
        if a[:2] in [('agent','get'),('pane','get')]: return {('agent' if a[0]=='agent' else 'pane'):copy.deepcopy(self.live[a[2]])}
        if a[:2]==('pane','process-info'):return {'process_info':{'shell_pid':1,'foreground_processes':[{'pid':1}]}}
        if a[:2]==('agent','prompt'):return {}
        raise AssertionError(a)

    def bind(self):
        return co.bind(self.api,ct.atomic,self.sp,self.state,{'pane_id':'p1'},self.rid)

    def dispatch(self):
        return co.dispatch(self.api,self.state,{'pane_id':'p2'},'T1')

    def result(self,t):
        d={k:t.get(k) for k in ('run_id','ticket_id','attempt','owner','base_commit')}
        d.update(status='completed', summary='read sources', result_commit=None,
                 changed_files=[],checks=[],remaining_work=[])
        return d

    def test_binding_survives_pointer_change(self):
        self.bind()
        (self.r.shop/'current.json').unlink()
        other=self.r.new('other')['created']
        self.assertEqual(self.r.current(),other)
        self.assertEqual(co.binding(self.state)['shop_id'],'shop1')
        with self.assertRaises(RuntimeError): co.bind(self.api,ct.atomic,self.sp,self.state,{'pane_id':'p1'},other)
        other_state=copy.deepcopy(self.state);other_state.pop('run_id');other_state['shop_id']='other'
        with self.assertRaises(RuntimeError):co.bind(self.api,ct.atomic,self.sp,other_state,{'pane_id':'p1'},self.rid)

    def test_bound_blocks_cleanup_finish(self):
        self.bind()
        p=self.r.path(self.rid)
        for f in ['SUMMARY.md','REVIEW.md']:(p/f).write_text('done')
        with self.assertRaises(RuntimeError):self.r.finish(self.rid,True)
        meta=self.r.meta(self.rid);meta.update(status='completed',completed_at=ct.stamp());ct.atomic(p/'run.json',meta)
        self.assertEqual(self.r.gc_plan()[0]['reason'],'bound-to-workstation')
        with self.assertRaises(RuntimeError):self.r.delete(self.rid,True,self.rid)

    def test_full_flow_and_unbind(self):
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc)
        t=self.dispatch()
        with self.assertRaises(RuntimeError):self.dispatch()
        cp=self.result(t);cp.update(progress='read entry',next_steps=['inspect imports'])
        ct.publish(self.repo,self.rid,'T1',cp,True)
        ct.publish(self.repo,self.rid,'T1',self.result(t))
        with self.assertRaises(RuntimeError):co.unbind(self.api,ct.atomic,self.sp,self.state,{'pane_id':'p2'},True)
        review=self.repo/'review.md';review.write_text('checked paths')
        ct.accept(self.repo,self.rid,'T1',review)
        co.unbind(self.api,ct.atomic,self.sp,self.state,{'pane_id':'p2'},True)
        self.assertNotIn('run_id',self.state)

    def test_uncertain_dispatch_never_resends(self):
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc)
        # Crash after preparation, before Pi send: assigned stays fenced. No prompt fallback.
        prepared = co.dispatch(self.api,self.state,{'pane_id':'p2'},'T1')
        self.assertIn('transport_request', prepared)
        self.assertFalse(any(call[:2] == ('agent', 'prompt') for call in self.calls))
        t=ct.load(ct.ticket_path(self.repo,self.rid,'T1'))
        self.assertEqual(t['dispatch']['delivery'],'prepared')
        with self.assertRaises(RuntimeError):self.dispatch()

    def test_only_primary_lead_dispatches_and_busy_refused(self):
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc)
        with self.assertRaises(RuntimeError):co.dispatch(self.api,self.state,{'pane_id':'p1'},'T1')
        self.live['p3']['agent_status']='working'
        with self.assertRaises(RuntimeError):self.dispatch()
        self.assertEqual(ct.load(ct.ticket_path(self.repo,self.rid,'T1'))['status'],'ready')

    def test_attempt_and_owner_fencing(self):
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc);t=self.dispatch()
        bad=self.result(t);bad['owner']='other'
        with self.assertRaises(RuntimeError):ct.publish(self.repo,self.rid,'T1',bad)
        old=self.result(t)
        ct.retry(self.repo,self.rid,'T1',True)
        self.dispatch()
        with self.assertRaises(RuntimeError):ct.publish(self.repo,self.rid,'T1',old)
        self.assertTrue(ct.attempt_path(self.repo,t,'ticket').is_file())

    def test_immutable_result_and_failed_not_accepted(self):
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc);t=self.dispatch()
        doc=self.result(t);doc['status']='blocked'
        ct.publish(self.repo,self.rid,'T1',doc)
        with self.assertRaises(RuntimeError):ct.publish(self.repo,self.rid,'T1',doc)
        f=self.repo/'review';f.write_text('review')
        with self.assertRaises(RuntimeError):ct.accept(self.repo,self.rid,'T1',f)

    def test_result_schema_and_scope(self):
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc);t=self.dispatch()
        for change in [{'changed_files':['outside.txt']},{'summary':''},{'checks':[{'command':'x','exit_code':0,'evidence':'missing'}]}]:
            d=self.result(t);d.update(change)
            with self.assertRaises(RuntimeError):ct.publish(self.repo,self.rid,'T1',d)

    def test_resume_does_not_prompt_or_start(self):
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc);self.dispatch();self.calls=[]
        result=co.resume_report(self.api,self.state)
        self.assertTrue(Path(result['report']).exists())
        self.assertFalse(any(a[:2] in [('agent','prompt'),('agent','start')] for a in self.calls))

    def test_recover_lead_blocks_live_agent(self):
        self.bind()
        with self.assertRaises(RuntimeError):co.recover_lead(self.api,lambda *a:None,ct.atomic,self.sp,self.state,{'pane_id':'p1'},True)

    def test_recover_shell_starts_only_on_apply(self):
        self.bind()
        self.live['p2'].pop('agent')
        starts=[]
        def start(*args): starts.append(args)
        co.recover_lead(self.api,start,ct.atomic,self.sp,self.state,{'pane_id':'p1'},False)
        self.assertEqual(starts,[])
        self.calls=[]
        co.recover_lead(self.api,start,ct.atomic,self.sp,self.state,{'pane_id':'p1'},True)
        self.assertEqual(len(starts),1)
        prompts=[a for a in self.calls if a[:2]==('agent','prompt')]
        self.assertEqual(len(prompts),1)
        self.assertIn('RECOVERY.json',prompts[0][-1])

    def test_cli_authorization_writer_occupant(self):
        import shop, os
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc);self.dispatch()
        with patch.dict(os.environ,{'HERDR_PANE_ID':'p3','HERDR_ACTIVE_PANE_ID':''}), patch.object(shop,'api',self.api):
            run.authorize_ticket(self.repo,self.rid,'T1','publish')
            with self.assertRaises(RuntimeError):run.authorize_ticket(self.repo,self.rid,'T1','accept')
            self.live['p3']['terminal_id']='replacement-process'
            with self.assertRaises(RuntimeError):run.authorize_ticket(self.repo,self.rid,'T1','publish')

    def test_retry_authorization_reads_identity_from_agent_endpoint(self):
        import shop, os
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc);self.dispatch()

        def pane_without_identity(*args):
            if args[:2] == ('pane','layout'):
                return {'layout':{'panes':[{'pane_id':'p3'}]}}
            if args[:2] == ('pane','get'):
                live = copy.deepcopy(self.live[args[2]])
                live.pop('name', None)
                return {'pane':live}
            return self.api(*args)

        env={'HERDR_PANE_ID':'p2','HERDR_ACTIVE_PANE_ID':''}
        with patch.dict(os.environ,env), patch.object(shop,'api',pane_without_identity):
            run.authorize_ticket(self.repo,self.rid,'T1','retry')
            self.live['p3']['agent_status']='working'
            with self.assertRaises(RuntimeError):
                run.authorize_ticket(self.repo,self.rid,'T1','retry')
            self.live['p3']['agent_status']='idle'
            self.live['p3']['name']='replacement-worker'
            with self.assertRaises(RuntimeError):
                run.authorize_ticket(self.repo,self.rid,'T1','retry')

    def test_dependency_blocks_dispatch(self):
        self.bind();ct.new_ticket(self.repo,self.rid,self.doc)
        doc=dict(self.doc,ticket_id='T2',depends_on=['T1'])
        ct.new_ticket(self.repo,self.rid,doc)
        with self.assertRaises(RuntimeError):co.dispatch(self.api,self.state,{'pane_id':'p2'},'T2')

    def test_required_checks_development_result(self):
        self.bind()
        doc=dict(self.doc,kind='development',base_commit='a'*40,checks=['unit'])
        with patch.object(ct,'baseline'):
            ct.new_ticket(self.repo,self.rid,doc);t=self.dispatch()
        draft=self.result(t)
        with self.assertRaises(RuntimeError):ct.publish(self.repo,self.rid,'T1',draft)
        evidence=self.repo/'test-output';evidence.write_text('test failed')
        draft['checks']=[{'command':'unit','exit_code':1,'evidence':str(evidence)}]
        with self.assertRaises(RuntimeError):ct.publish(self.repo,self.rid,'T1',draft)
        draft['status']='failed'
        ct.publish(self.repo,self.rid,'T1',draft)

    def test_baseline_rejects_dirty_and_mismatch(self):
        def git(*a):return subprocess.check_output(['git','-C',str(self.repo),*a],stderr=subprocess.DEVNULL,text=True).strip()
        git('init');git('config','user.email','test@example.invalid');git('config','user.name','test')
        (self.repo/'src').mkdir();(self.repo/'src/a.txt').write_text('base')
        git('add','src');git('commit','-m','baseline');sha=git('rev-parse','HEAD')
        ct.baseline(self.repo,self.repo,sha)
        with self.assertRaises(RuntimeError):ct.baseline(self.repo,self.repo,'HEAD')
        (self.repo/'src/a.txt').write_text('dirty')
        with self.assertRaises(RuntimeError):ct.baseline(self.repo,self.repo,sha)


if __name__=='__main__':unittest.main()
