import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import messaging


class MessagingTests(unittest.TestCase):
    def setUp(self):
        self.state={'phase':'ready','run_id':'r','shop_id':'s','tab':'t',
            'architect':{'name':'a','pane':'p1','launch_id':'a1','terminal_id':'t1'},
            'lead':{'name':'l','pane':'p2','launch_id':'l1','terminal_id':'t2'},
            'workers':[{'name':'w','pane':'p3','launch_id':'w1','terminal_id':'t3'}]}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state['cwd'] = temp.name
        (Path(temp.name) / '.shop/runs/r').mkdir(parents=True)
        self.calls=[];self.status='idle'
    def api(self,*args):
        self.calls.append(args)
        if args[:2]==('agent','get'):
            i=args[2][-1];return {'agent':{'name':{'1':'a','2':'l','3':'w'}[i], 'agent':'pi','tab_id':'t','terminal_id':'t'+i,'agent_status':self.status}}
        if args[:2]==('agent','prompt'):return {}
        raise AssertionError(args)
    def send(self,target='lead',busy=False):
        with patch.object(messaging.co,'binding'):
            return messaging.send(self.api,self.state,{'pane_id':'p3'},target,'Which interface?',busy)
    def test_light_message_journal_without_ticket_or_prompt(self):
        result=self.send();self.assertFalse(result['submitted']);self.assertEqual(result['tickets_created'],0)
        self.assertIn('transport_request', result)
        self.assertFalse(any(call[:2] == ('agent', 'prompt') for call in self.calls))
        self.assertFalse((Path(self.state['cwd']) / '.shop/runs/r/tickets').exists())
    def test_busy_requires_opt_in(self):
        self.status='working';self.assertFalse(self.send()['submitted'])
        self.assertFalse(any(x[:2]==('agent','prompt') for x in self.calls))
        self.assertIn('transport_request', self.send(busy=True))
    def test_changed_occupant_refused(self):
        self.state['lead']['terminal_id']='wrong'
        with self.assertRaises(RuntimeError):self.send()
    def test_ambiguous_role_refused(self):
        self.state['workers'].append(dict(self.state['workers'][0],name='w2',pane='p4'))
        with self.assertRaises(RuntimeError):self.send('worker')
    def test_prepare_failure_never_falls_back(self):
        with patch.object(messaging.transport, 'prepare', side_effect=RuntimeError('journal unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'journal unavailable'): self.send()
        self.assertFalse(any(call[:2] == ('agent', 'prompt') for call in self.calls))


if __name__=='__main__':unittest.main()
