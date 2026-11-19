from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from sources import Article
from news_history import save_articles
from telegram_bot import open_state
import news_preparation as prep


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.temp.name)/'bot.sqlite3')
        self.db=open_state(self.path)
        self.now=datetime.now(timezone.utc)
        self.revision=patch.object(prep,'revision',return_value=('version','rules'));self.revision.start()

    def tearDown(self):
        self.revision.stop();self.db.close();self.temp.cleanup()

    def article(self, title='New story', suffix='a', offset=1):
        article=Article(title,'https://example.org/'+suffix,self.now-timedelta(minutes=offset),'source')
        value=save_articles(self.db,[article],self.now)[0]
        return article,value

    def classifier(self,label='news'):
        return Mock(side_effect=lambda batch,*args,**kwargs:[{'label':label,'tags':[],'relation':'new','previous_id':0} for _ in batch])

    def deliver(self,article_id,chat_id=123):
        with self.db:
            digest=self.db.execute('INSERT INTO news_digests(chat_id,since_at,until_at,html,delivered_at) VALUES (?,?,?,?,?)',
                (chat_id,(self.now-timedelta(hours=24)).isoformat(),self.now.isoformat(),'html',self.now.isoformat())).lastrowid
            self.db.execute('INSERT INTO news_digest_items(digest_id,article_id,section) VALUES (?,?,?)',(digest,article_id,'news'))

    def test_skip_is_persisted_and_reused_without_another_model_call(self):
        article,article_id=self.article()
        model=self.classifier('skip')
        prep.prepare(self.db,'model',[article_id],annotate_fn=model)
        prep.prepare(self.db,'model',[article_id],annotate_fn=Mock(side_effect=AssertionError('cached')))
        items,updates,repeated,stats=prep.ready_items(self.db,[article],[article_id],123,'model',self.now)
        self.assertEqual(items,[]);self.assertEqual(stats['model_skipped'],1)
        self.assertEqual(model.call_count,1)

    def test_only_new_or_changed_titles_are_evaluated(self):
        first,first_id=self.article();model=self.classifier()
        prep.prepare(self.db,'model',[first_id],annotate_fn=model)
        changed,changed_id=self.article('Changed title','a')
        self.assertNotEqual(first_id,changed_id)
        prep.prepare(self.db,'model',[first_id,changed_id],annotate_fn=model)
        self.assertEqual([a['id'] for a in model.call_args.args[0]],[changed_id])

    def test_model_or_rules_change_invalidates_cache_but_preserves_old_records(self):
        article,article_id=self.article();prep.prepare(self.db,'model',[article_id],annotate_fn=self.classifier())
        with patch.object(prep,'revision',return_value=('new-version','rules')):
            self.assertEqual(prep.missing(self.db,[article_id],'new-version'),[article_id])
            prep.prepare(self.db,'model',[article_id],annotate_fn=self.classifier('important'))
            self.assertEqual(prep.ready_items(self.db,[article],[article_id],123,'model',self.now)[0][0]['section'],'important')
        self.assertEqual(self.db.execute('SELECT count(*) FROM news_annotations').fetchone()[0],2)

    def test_on_demand_completion_moves_next_deadline_and_old_tick_does_not_run(self):
        _,article_id=self.article()
        with patch.object(prep,'monotonic',return_value=100):
            prep.prepare(self.db,'model',[article_id],interval=60,annotate_fn=self.classifier())
        self.assertEqual(prep.state_for(self.db).next_due,160)
        _,second_id=self.article('Second story','b')
        with patch.object(prep,'monotonic',return_value=150):
            prep.prepare(self.db,'model',[second_id],interval=60,annotate_fn=self.classifier())
        self.assertEqual(prep.state_for(self.db).next_due,210)
        collect=Mock(side_effect=AssertionError('old timer must not collect'))
        with patch.object(prep,'monotonic',return_value=161):
            self.assertEqual(prep.prepare(self.db,'model',scheduled=True,collect_fn=collect),0)
        collect.assert_not_called()

    def test_simultaneous_requests_join_one_evaluation(self):
        _,article_id=self.article()
        entered,release=Event(),Event();errors=[]
        def classifier(batch,*args,**kwargs):
            entered.set()
            if not release.wait(3):raise AssertionError('timeout')
            return [{'label':'news','tags':[],'relation':'new','previous_id':0} for _ in batch]
        model=Mock(side_effect=classifier)
        def run():
            db=sqlite3.connect(self.path,timeout=5)
            try:prep.prepare(db,'model',[article_id],annotate_fn=model)
            except Exception as error:errors.append(error)
            finally:db.close()
        first,second=Thread(target=run),Thread(target=run)
        first.start()
        try:
            self.assertTrue(entered.wait(2));second.start()
        finally:
            release.set();first.join(4)
            if second.ident:second.join(4)
        self.assertEqual(errors,[]);self.assertEqual(model.call_count,1)

    def test_partial_failure_keeps_completed_batch_and_retries_only_missing(self):
        ids=[self.article('Story '+str(i),str(i),offset=20-i)[1] for i in range(9)]
        def classify(batch,*args,**kwargs):
            if len(batch)==1:raise ValueError('provider unavailable')
            return [{'label':'news','tags':[],'relation':'new','previous_id':0} for _ in batch]
        with self.assertRaises(ValueError):prep.prepare(self.db,'model',ids,annotate_fn=classify)
        self.assertEqual(len(prep.missing(self.db,ids,'version')),1)
        model=self.classifier();prep.prepare(self.db,'model',ids,annotate_fn=model)
        self.assertEqual(len(model.call_args.args[0]),1)
        self.assertEqual(self.db.execute('SELECT prepared,status FROM news_preparation_runs ORDER BY id LIMIT 1').fetchone(),(8,'error'))

    def test_invalid_relation_does_not_commit_fabricated_or_cyclic_link(self):
        _,article_id=self.article()
        for predecessor in (article_id,999999):
            model=Mock(return_value=[{'label':'news','tags':[],'relation':'repeat','previous_id':predecessor}])
            with self.assertRaises(ValueError):prep.prepare(self.db,'model',[article_id],annotate_fn=model)
        self.assertEqual(prep.missing(self.db,[article_id],'version'),[article_id])

    def test_shared_repeats_hidden_only_for_user_who_saw_the_event(self):
        old,old_id=self.article('Water supply restored','old',offset=10)
        prep.prepare(self.db,'model',[old_id],annotate_fn=self.classifier());self.deliver(old_id)
        new,new_id=self.article('Water supply restored in city','new')
        model=Mock(return_value=[{'label':'news','tags':[],'relation':'repeat','previous_id':old_id}])
        prep.prepare(self.db,'model',[new_id],annotate_fn=model)
        self.assertEqual(prep.ready_items(self.db,[new],[new_id],123,'model',self.now)[0],[])
        self.assertEqual(len(prep.ready_items(self.db,[new],[new_id],456,'model',self.now)[0]),1)

    def test_update_links_to_previously_delivered_article_without_model_on_request(self):
        old,old_id=self.article('Bridge damaged','old',offset=10)
        prep.prepare(self.db,'model',[old_id],annotate_fn=self.classifier());self.deliver(old_id)
        new,new_id=self.article('Bridge repaired after damage','new')
        prep.prepare(self.db,'model',[new_id],annotate_fn=Mock(return_value=[{'label':'important','tags':[],'relation':'update','previous_id':old_id}]))
        with patch.object(prep,'chat',side_effect=AssertionError('ready digest must not call LLM')):
            items,updates,_,_=prep.ready_items(self.db,[new],[new_id],123,'model',self.now)
        self.assertEqual(items,[{'id':1,'section':'important'}]);self.assertEqual(updates[new.url]['id'],old_id)

    def test_preparation_worker_uses_its_connection_and_stops_cleanly(self):
        from news_cache import NewsCollector
        done=Event()
        worker=NewsCollector(self.path,interval=.01,model='model')
        def prepare(db,model,**kwargs):
            self.assertIsNot(db,self.db)
            self.assertEqual(model,'model');self.assertTrue(kwargs['scheduled'])
            worker.stopped.set();done.set()
        with patch.object(prep,'prepare',side_effect=prepare):
            worker.start()
            try:self.assertTrue(done.wait(2))
            finally:worker.close()
        self.assertFalse(worker.thread.is_alive());self.assertIsNone(worker.failure)

    def test_foreground_ready_digest_has_no_model_or_market_network_calls(self):
        from collections import OrderedDict
        import news_cache
        import telegram_bot as bot
        article,article_id=self.article()
        prep.prepare(self.db,'model',[article_id],annotate_fn=self.classifier())
        news_cache.collect_once(self.db,self.now,lambda *args:([article],{'source':(1,'')}))
        api=Mock()
        with patch.object(bot,'refresh_news',return_value=(self.now,{})), patch.object(prep,'chat',side_effect=AssertionError('no LLM')), patch('html_digest.fetch_market',side_effect=AssertionError('no market fetch')):
            bot.handle_update(api,{'update_id':1,'message':{'chat':{'id':123,'type':'private'},'text':'За 4 години'}},self.db,OrderedDict(),'model')
        api.send_document.assert_called_once()
        self.assertIn('Оцінки вже готові',str(api.send.call_args_list))

    def test_provider_schema_failure_is_repaired_by_smaller_batches(self):
        ids=[self.article('News story '+str(i),str(i),offset=20-i)[1] for i in range(8)]
        def classify(batch,*args,**kwargs):
            if len(batch)>2:raise prep.LLMSchemaError('invalid generated label')
            return [{'label':'skip','tags':[],'relation':'new','previous_id':0} for _ in batch]
        model=Mock(side_effect=classify)
        self.assertEqual(prep.prepare(self.db,'model',ids,annotate_fn=model),8)
        self.assertEqual(prep.missing(self.db,ids,'version'),[])
        self.assertEqual([len(call.args[0]) for call in model.call_args_list],[8,4,2,2,4,2,2])
        self.assertEqual(self.db.execute("SELECT count(*) FROM news_annotations WHERE label='skip'").fetchone()[0],8)

    def test_persistent_invalid_singleton_keeps_other_successful_parts(self):
        ids=[self.article('News story '+str(i),str(i),offset=20-i)[1] for i in range(8)]
        def classify(batch,*args,**kwargs):
            return [{'label':'wrong' if article['id']==ids[4] else 'news','tags':[],'relation':'new','previous_id':0} for article in batch]
        with self.assertRaises(prep.AnnotationError):prep.prepare(self.db,'model',ids,annotate_fn=classify)
        ready={row[0] for row in self.db.execute('SELECT article_id FROM news_annotations')}
        self.assertEqual(ready,set(ids[:4]))
        model=self.classifier();prep.prepare(self.db,'model',ids,annotate_fn=model)
        self.assertEqual({a['id'] for a in model.call_args.args[0]},set(ids[4:]))

    def test_quota_failure_does_not_split_or_repeat_the_batch(self):
        ids=[self.article('Story '+str(i),str(i))[1] for i in range(8)]
        from llm import LLMError
        model=Mock(side_effect=LLMError('quota exceeded'))
        with self.assertRaises(LLMError):prep.prepare(self.db,'model',ids,annotate_fn=model)
        self.assertEqual(model.call_count,1)
        self.assertEqual(len(prep.missing(self.db,ids,'version')),8)

    def test_singleton_relaxed_provider_schema_still_validates_enums_locally(self):
        import json
        _,article_id=self.article()
        good={'message':{'content':json.dumps({'annotations':[{'label':'skip','tags':[],'relation':'new','previous_id':0}]})}}
        with patch.object(prep,'chat',side_effect=[prep.LLMSchemaError('invalid enum'),good]) as request:
            prep.prepare(self.db,'model',[article_id])
        self.assertEqual(request.call_count,2)
        self.assertIn('enum',request.call_args_list[0].kwargs['schema']['properties']['annotations']['items']['properties']['label'])
        self.assertNotIn('enum',request.call_args_list[1].kwargs['schema']['properties']['annotations']['items']['properties']['label'])
        self.assertEqual(self.db.execute('SELECT label FROM news_annotations').fetchone()[0],'skip')

    def test_relaxed_response_with_invalid_enum_is_never_saved(self):
        import json
        _,article_id=self.article()
        bad={'message':{'content':json.dumps({'annotations':[{'label':'not-a-category','tags':[],'relation':'new','previous_id':0}]})}}
        with patch.object(prep,'chat',side_effect=[prep.LLMSchemaError('invalid enum'),bad]) as request:
            with self.assertRaises(prep.AnnotationError):prep.prepare(self.db,'model',[article_id])
        self.assertEqual(request.call_count,2)
        self.assertEqual(prep.missing(self.db,[article_id],'version'),[article_id])

    def test_one_representative_per_event_for_a_new_user(self):
        old,old_id=self.article('Water restored','old',offset=10)
        prep.prepare(self.db,'model',[old_id],annotate_fn=self.classifier())
        new,new_id=self.article('Water restored to consumers','new')
        prep.prepare(self.db,'model',[new_id],annotate_fn=Mock(return_value=[{'label':'news','tags':[],'relation':'repeat','previous_id':old_id}]))
        items,_,repeated,_=prep.ready_items(self.db,[new,old],[new_id,old_id],456,'model',self.now)
        self.assertEqual(items,[{'id':1,'section':'news'}]);self.assertEqual(repeated,1)


if __name__=='__main__':unittest.main()
