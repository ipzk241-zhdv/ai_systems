from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from sources import Article
from digest import limit_items
from html_digest import render_html
from news_history import save_articles
from telegram_bot import open_state
import news_personalization as personal
import news_preparation as prep


class PersonalizationTests(unittest.TestCase):
    def test_runtime_corpus_has_calibration_examples_and_excludes_reserved_ids(self):
        examples,_=personal.load()
        metadata=json.loads(personal.EXAMPLES.read_text(encoding='utf-8'))
        self.assertEqual(len(examples),100)
        self.assertEqual(metadata['held_out_ids'],[])
        self.assertEqual(len(metadata['historical_mini_test_ids']),20)
        self.assertFalse({a['id'] for a in examples}&set(metadata['held_out_ids']))
        self.assertEqual({a['label'] for a in examples},{'skip','news','important'})
        self.assertTrue(all(a['label']=='news' for a in examples if a['legacy_label']=='fun'))

    def test_retrieval_is_bounded_and_only_contains_actual_calibration_examples(self):
        examples,_=personal.load()
        result=personal.retrieve([{'title':'ДТП на трасі: водій потрапив до лікарні'}])
        self.assertLessEqual(len(result),6)
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False).encode()),3600)
        self.assertEqual({r['label'] for r in result},{'skip','news','important'})
        self.assertTrue(all(r['title'] in {a['title'][:220] for a in examples} for r in result))
        self.assertTrue(any(r['label']=='skip' for r in result))

    def test_bad_corpus_cannot_leak_a_reserved_example(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'examples.json'
            file.write_text(json.dumps({'held_out_ids':['reserved'],'examples':[{'id':'reserved','title':'Secret validation title','label':'skip'}]}),encoding='utf-8')
            with patch.object(personal,'EXAMPLES',file):
                with self.assertRaises(ValueError):personal.load()

    def test_corpus_change_changes_cache_fingerprint(self):
        first=personal.fingerprint()
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'examples.json'
            file.write_text(json.dumps({'examples':[{'id':'a','title':'Updated preference','label':'skip'}]}),encoding='utf-8')
            with patch.object(personal,'EXAMPLES',file):self.assertNotEqual(first,personal.fingerprint())

    def test_news_model_can_be_selected_without_changing_chat_model(self):
        from settings import news_model,default_model
        config={'LLM_MODEL':'chat-model','NEWS_MODEL':'news-model'}
        self.assertEqual(default_model(config),'chat-model')
        self.assertEqual(news_model('chat-model',config),'news-model')
        self.assertEqual(news_model('chat-model',{'NEWS_MODEL':''}),'chat-model')

    def test_exact_human_skip_uses_no_model_and_records_provenance(self):
        db=open_state(':memory:');now=datetime.now(timezone.utc)
        article=Article('Known advertisement','https://example.org/ad',now-timedelta(minutes=1),'test')
        try:
            article_id=save_articles(db,[article],now)[0]
            with patch.object(prep,'revision',return_value=('test','rules')),patch.object(prep,'known_decision',side_effect=lambda a:'skip' if a.get('url')==article.url else None),patch.object(prep,'chat',side_effect=AssertionError('human skip must not call LLM')):
                prep.prepare(db,'model',[article_id])
                items,_,_,stats=prep.ready_items(db,[article],[article_id],123,'model',now)
            self.assertEqual(items,[])
            self.assertEqual(stats['human_skipped'],1);self.assertEqual(stats['model_skipped'],0)
            self.assertEqual(db.execute('SELECT decision_source FROM news_annotations').fetchone()[0],'human_exact')
        finally:db.close()

    def test_human_match_requires_both_same_url_and_same_title(self):
        examples,_=personal.load();item=examples[0]
        self.assertEqual(personal.known_decision({'url':item['url'],'title':item['title']}),item['label'])
        self.assertIsNone(personal.known_decision({'url':item['url'],'title':'A changed headline'}))
        self.assertIsNone(personal.known_decision({'url':'https://other.example/story','title':item['title']}))

    def test_tags_are_independent_and_skip_cannot_keep_them(self):
        incoming=[{'id':1,'title':'Новий захист'}]
        self.assertEqual(prep.validate(incoming,[],[{'label':'important','tags':['positive'],'relation':'new','previous_id':0}])[0]['label'],'important')
        for label,tags in [('fun',[]),('skip',['funny']),('news',['positive','positive']),('news',['made-up'])]:
            with self.assertRaises(prep.AnnotationError):
                prep.validate(incoming,[],[{'label':label,'tags':tags,'relation':'new','previous_id':0}])

    def test_old_database_keeps_legacy_rows_during_tag_migration(self):
        import sqlite3
        db=sqlite3.connect(':memory:')
        try:
            db.executescript('''CREATE TABLE news_articles(id INTEGER PRIMARY KEY);
                CREATE TABLE news_annotations(article_id INTEGER,version TEXT,label TEXT,relation TEXT,previous_id INTEGER,prepared_at TEXT,PRIMARY KEY(article_id,version));
                INSERT INTO news_annotations VALUES(1,'old','fun','new',NULL,'2026-11-19');''')
            prep.initialize(db);prep.initialize(db)
            self.assertEqual(db.execute('SELECT version,label,tags_json FROM news_annotations').fetchone(),('old','fun','[]'))
        finally:db.close()

    def test_positive_tag_survives_cache_and_html_without_becoming_fun_category(self):
        db=open_state(':memory:');now=datetime.now(timezone.utc)
        article=Article('Нові засоби захисту України','https://example.org/protection',now-timedelta(minutes=1),'test')
        try:
            article_id=save_articles(db,[article],now)[0]
            with patch.object(prep,'revision',return_value=('test','rules')):
                prep.prepare(db,'model',[article_id],annotate_fn=lambda *args,**kwargs:[{'label':'important','tags':['positive'],'relation':'new','previous_id':0}])
                items,_,_,_=prep.ready_items(db,[article],[article_id],123,'model',now)
            self.assertEqual(items,[{'id':1,'section':'important','tags':['positive']}])
            html=BeautifulSoup(render_html([article],items,{}, {},now-timedelta(hours=1),now),'html.parser')
            self.assertEqual(html.select_one('.news-tag').get_text(),'Позитивне')
            self.assertIn('Важливе',html.get_text())
            self.assertNotIn('Кумедне',html.get_text())
        finally:db.close()

    def test_tagged_news_get_featured_slots_after_important_items(self):
        now=datetime.now(timezone.utc)
        articles=[Article(str(i),'https://example.org/'+str(i),now,'test') for i in range(20)]
        items=[{'id':i+1,'section':'news'} for i in range(20)]
        items[-1]['tags']=['funny']
        selected=limit_items(articles,items,limit=3)
        self.assertEqual(selected[0]['id'],20)
        self.assertEqual(len({item['id'] for item in selected}),3)
