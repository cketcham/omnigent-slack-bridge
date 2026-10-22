import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec); sys.modules[name]=mod; spec.loader.exec_module(mod)
    return mod
bridge=load('bridge',ROOT/'omnigent_slack_bridge.py')
keeper=load('keeper',ROOT/'scripts/omnigent-token-keeper.py')

class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.b=bridge.Bridge(bridge.Config(state_dir=self.tmp.name, auth_token='fake'))
        self.b.slack=Mock(); self.b.omni=Mock()
        self.b.slack.rename.return_value='ok'
        self.b.slack.post_message.return_value='1.0'
        self.b.omni.list_items.return_value=[]

    def seed(self,**fields):
        record=dict(channel_id='C1',channel_name=bridge.channel_name(self.b.cfg.prefix,'','title'),title='title',last_mirror_id='old')
        record.update(fields)
        self.b.store.update(lambda sessions:sessions.update(sid=record))

    def update(self,status='idle'):
        self.b._handle_session(dict(id='sid',title='title',status=status))

    def test_failed_unarchive_keeps_channel(self):
        self.seed(closed=True); self.b.slack.unarchive.return_value=False
        self.update()
        self.assertEqual(self.b.store.records()[0].channel_id,'C1')
        self.b.slack.create_channel.assert_not_called()
        self.assertEqual(self.b.slack.unarchive.call_count,1)

    def test_reopen_clears_archive_marker(self):
        self.seed(closed=True,archived_ok=True); self.b.slack.unarchive.return_value=True
        self.update()
        rec=self.b.store.records()[0]
        self.assertFalse(rec.closed); self.assertFalse(rec.archived_ok)
        self.b.slack.create_channel.assert_not_called()

    def test_slow_slack_does_not_block_inbound_state(self):
        self.seed(closed=True)
        entered=threading.Event(); release=threading.Event(); wrote=threading.Event()
        def unarchive(*args):
            entered.set()
            if not release.wait(5): raise AssertionError('release timed out')
            return True
        self.b.slack.unarchive.side_effect=unarchive
        worker=threading.Thread(target=self.update); worker.start()
        self.assertTrue(entered.wait(5))
        def inbound():
            self.b._set_field('sid','dm_channel','D2'); wrote.set()
        writer=threading.Thread(target=inbound); writer.start()
        try: self.assertTrue(wrote.wait(1))
        finally:
            release.set(); worker.join(5); writer.join(5)
        self.assertEqual(self.b.store._load()['sessions']['sid']['dm_channel'],'D2')

    def test_missing_membership_does_not_create(self):
        self.seed(channel_name='old-name'); self.b.slack.rename.return_value='gone'
        self.update()
        self.assertEqual(self.b.store.records()[0].channel_id,'C1')
        self.b.slack.create_channel.assert_not_called()

    def test_failed_post_does_not_advance_cursor(self):
        self.seed()
        with patch.object(self.b,'_new_assistant_text',return_value=('hello','new')):
            self.b.slack.post_message.side_effect=bridge.ApiError('slack','ratelimited')
            self.update()
            self.assertEqual(self.b.store.records()[0].last_mirror_id,'old')
            self.b.slack.post_message.side_effect=None
            self.update()
            self.assertEqual(self.b.store.records()[0].last_mirror_id,'new')

    def test_dm_failure_retries_without_reposting_channel(self):
        self.b.cfg.ops_session='sid'; self.seed(dm_channel='D1')
        cursors=[]
        def texts(sid,cursor):
            cursors.append(cursor)
            return ('hello','new') if cursor=='old' else ('','new')
        def post(cid,text):
            if cid=='D1': raise bridge.ApiError('slack','ratelimited')
            return '1.0'
        with patch.object(self.b,'_new_assistant_text',side_effect=texts):
            self.b.slack.post_message.side_effect=post
            self.update()
            self.b.slack.post_message.reset_mock()
            self.b.slack.post_message.side_effect=None
            self.update()
            self.b.slack.post_message.assert_called_once_with('D1','hello')
        self.assertEqual(cursors,['old','old','new','old'])

    def test_channel_id_is_on_disk_before_invite(self):
        self.b.slack.create_channel.return_value='C2'
        def invite(*args):
            self.assertEqual(self.b.store.records()[0].channel_id,'C2')
            raise RuntimeError('simulated process failure after creation')
        self.b.slack.invite_user.side_effect=invite
        with self.assertRaises(RuntimeError): self.update()
        self.assertEqual(self.b.store.records()[0].channel_id,'C2')

    def test_silently_rejected_post_keeps_cursor(self):
        self.seed(); self.b.slack.post_message.return_value=''
        with patch.object(self.b,'_new_assistant_text',return_value=('hello','new')):
            self.update()
        self.assertEqual(self.b.store.records()[0].last_mirror_id,'old')

    def test_name_collision_never_adopts_another_channel(self):
        self.b.slack.create_channel.side_effect=[bridge.ApiError('slack','name_taken'),'C2']
        cid,_=self.b._create_channel('taken','','title','sid')
        self.assertEqual(cid,'C2'); self.b.slack.lookup_by_name.assert_not_called()

    def test_running_session_without_channel_is_valid(self):
        self.update('running')
        self.b.slack.create_channel.assert_not_called()

    def test_save_progress_on_late_failure(self):
        def update(sessions):
            sessions['sid']={'channel_id':'C1'}
            raise bridge.ApiError('slack','failure')
        with self.assertRaises(bridge.ApiError): self.b.store.update(update)
        self.assertEqual(self.b.store.records()[0].channel_id,'C1')

    def test_transient_removal_does_not_archive(self):
        self.seed(); self.b.omni.get_session.side_effect=bridge.ApiError('omnigent','http 503')
        self.b._handle_session_removed('sid')
        self.b.slack.archive.assert_not_called()

    def test_both_token_writers_private_even_with_stale_tmp(self):
        with patch.object(Path,'home',return_value=Path(self.tmp.name)):
            path=Path(self.tmp.name)/'.omnigent/auth_tokens.json'; path.parent.mkdir()
            stale=Path(str(path)+'.tmp'); stale.write_text('stale'); stale.chmod(0o644)
            bridge._store_token('server','access','refresh',None)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            keeper._store_token('other','access','refresh',3600)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(set(json.loads(path.read_text())),{'server','other'})
            self.assertEqual(stale.read_text(),'stale')

    def test_keeper_starts_with_sso_only(self):
        with patch.object(keeper,'_read_server_url',return_value='server'), patch.object(keeper,'_read_credentials',return_value=None), patch.object(keeper,'_load_token_entry',return_value={'refresh_token':'refresh'}), patch.object(keeper,'refresh_if_needed') as refresh, patch.object(keeper.time,'sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): keeper.run_loop()
            refresh.assert_called_once_with('server')

    def test_bridge_reuses_token_rotated_while_waiting(self):
        with patch.object(Path,'home',return_value=Path(self.tmp.name)), patch.object(bridge,'_load_token',return_value='new'), patch.object(bridge.urllib.request,'urlopen') as request:
            self.assertTrue(bridge.OmnigentClient(bridge.Config(auth_token='old'))._refresh_via_server())
            request.assert_not_called()

if __name__=='__main__': unittest.main()
