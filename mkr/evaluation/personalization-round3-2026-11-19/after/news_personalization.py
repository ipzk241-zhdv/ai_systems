"""Small local retrieval over user-rated headlines; no embeddings or training required."""
from collections import Counter
import hashlib
import json
from math import log, sqrt
from pathlib import Path
import re

EXAMPLES = Path(__file__).with_name('news_examples.json')
LABELS = ['important','news','skip']
TAGS = ['funny','interesting','positive']
RETRIEVAL_VERSION = 'rated-headline-retrieval-v2-human-tags'
STOP = {'після','перед','серед','через','цього','цьому','також','стало','стали','будуть','україн','російс',
        'после','перед','среди','через','этого','этому','также','стали','будет','украин','россий'}
_cached = None
_cache_signature = None


def words(text):
    tokens = re.findall(r"[a-zа-яіїєґ0-9]{3,}", text.casefold())
    return Counter(token[:6] for token in tokens if token[:6] not in STOP)


def load():
    global _cached, _cache_signature
    data = EXAMPLES.read_bytes()
    signature = hashlib.sha256(data).hexdigest()
    if signature != _cache_signature:
        value = json.loads(data.decode('utf-8-sig'))
        examples = value.get('examples')
        if not isinstance(examples,list) or len(examples)>200:
            raise ValueError('Invalid news examples')
        held_out=set(value.get('held_out_ids',[]))
        ids=set()
        for item in examples:
            if (not isinstance(item,dict) or item.get('label') not in LABELS
                    or not isinstance(item.get('title'),str) or not isinstance(item.get('id'),str)
                    or not isinstance(item.get('comment',''),str)):
                raise ValueError('Invalid news example')
            if item['id'] in held_out or item['id'] in ids:
                raise ValueError('Duplicate or held-out news example')
            tags=item.get('tags',[])
            if (not isinstance(tags,list) or len(tags)>3
                    or any(not isinstance(tag,str) or tag not in TAGS for tag in tags)
                    or len(set(tags))!=len(tags) or (item['label']=='skip' and tags)):
                raise ValueError('Invalid human news tags')
            ids.add(item['id'])
        _cached, _cache_signature = examples, signature
    return _cached, signature


def fingerprint():
    _, digest = load()
    return RETRIEVAL_VERSION + ':' + digest


def retrieve(incoming, max_examples=6, byte_budget=3600, exclude_ids=()):
    examples,_ = load()
    excluded=set(exclude_ids)
    examples=[item for item in examples if item['id'] not in excluded]
    if not examples:return []
    vectors=[words(item['title']) for item in examples]
    frequency=Counter(term for vector in vectors for term in vector)
    idf={term:log((len(vectors)+1)/(count+1))+1 for term,count in frequency.items()}
    def score(left,right):
        shared=set(left)&set(right)
        numerator=sum(left[t]*right[t]*idf.get(t,1)**2 for t in shared)
        denominator=sqrt(sum((v*idf.get(t,1))**2 for t,v in left.items()) * sum((v*idf.get(t,1))**2 for t,v in right.items()))
        return numerator/denominator if denominator else 0
    queries=[words(item['title']) for item in incoming]
    ranked=sorted(range(len(examples)),key=lambda i:(-max((score(q,vectors[i]) for q in queries),default=0),examples[i]['id']))
    chosen=[]
    # A relevant counterexample matters: keep label diversity instead of only similar 'news' examples.
    for label in ['skip','important','news']:
        index=next((i for i in ranked if examples[i]['label']==label),None)
        if index is not None:chosen.append(index)
    # Keep one real human humor example visible; otherwise abundant skip examples
    # can suppress this less common preference even in mixed batches.
    funny=next((i for i in ranked if 'funny' in examples[i].get('tags',[])),None)
    if funny is not None and funny not in chosen:chosen.append(funny)
    chosen += [i for i in ranked if i not in chosen]
    result=[]
    for index in chosen:
        item=examples[index]
        view={'title':item['title'][:220],'label':item['label'],'explanation':(item.get('comment','')+' '+item.get('clarification','')).strip()[:180]}
        if 'tags' in item:view['tags']=list(item['tags'])
        candidate=result+[view]
        if len(json.dumps(candidate,ensure_ascii=False).encode('utf-8'))>byte_budget:continue
        result=candidate
        if len(result)>=max_examples:break
    return result


def known_decision(article):
    if not article.get('url'):return None
    examples,_=load()
    title=' '.join(article['title'].casefold().split())
    matches=[item['label'] for item in examples if item.get('url')==article['url'] and ' '.join(item['title'].casefold().split())==title]
    return matches[0] if matches and len(set(matches))==1 else None
