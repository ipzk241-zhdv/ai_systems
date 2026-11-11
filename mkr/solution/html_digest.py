import json
from datetime import date
from html import escape
from math import isfinite
from urllib.request import Request, urlopen

from digest import group_items
from sources import KYIV


MARKET = {"SPY.US": "S&P 500", "QQQ.US": "Nasdaq-100", "DIA.US": "Dow Jones"}
MARKET_SOURCE = "https://ninequantai.com/en/api#data-timeliness"

STYLE = """
:root { color-scheme: light; --ink: #152332; --muted: #4e5f71; --line: #dce4ed;
        --blue: #1d557d; font-family: system-ui, -apple-system, sans-serif; }
* { box-sizing: border-box; }
html { background: #eef3f7; }
body { max-width: 58rem; margin: auto; padding: 1.5rem; color: var(--ink); }
a { color: var(--blue); text-underline-offset: .2em; }
a:focus-visible { outline: 2px solid currentColor; outline-offset: 4px; }
.hero { background: #142b40; color: #fff; border-radius: 1.3rem; padding: clamp(1.35rem, 5vw, 3rem); }
.eyebrow { margin: 0; color: #a4d5ef; font-size: .79rem; font-weight: 700; letter-spacing: .12em;
           text-transform: uppercase; }
h1 { font-size: clamp(2rem, 8vw, 3.75rem); letter-spacing: -.05em; line-height: 1.04;
     margin: .55rem 0 .85rem; }
.period { color: #d7e7f2; font-size: 1rem; line-height: 1.5; margin: 0; }
.stats { color: #d7e7f2; margin: .5rem 0 0; }
nav { display: flex; flex-wrap: wrap; gap: .45rem; margin: 1rem 0; }
nav a { background: #fff; border: 1px solid var(--line); border-radius: 2rem; padding: .48rem .85rem;
        font-size: .84rem; font-weight: 650; text-decoration: none; }
nav a:hover { background: #e6f2fa; }
.panel { background: #fff; border: 1px solid var(--line); border-radius: 1rem; margin-bottom: 1rem;
         padding: clamp(1rem, 3.2vw, 1.7rem); }
h2 { font-size: 1.32rem; letter-spacing: -.025em; margin: 0 0 .65rem; }
.hint { color: var(--muted); line-height: 1.5; font-size: .87rem; margin: .45rem 0 0; }
.market { display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: .65rem;
          margin: .85rem 0; }
.quote { background: #f5f8fb; border: 1px solid var(--line); border-radius: .7rem; padding: .85rem; }
.quote strong { font-size: 1.45rem; display: block; margin: .35rem 0; font-variant-numeric: tabular-nums; }
.quote small { color: var(--muted); }
.up { color: #136045; } .down { color: #9d2638; }
.news h2 { border-left: .25rem solid #3a799c; padding-left: .65rem; }
.news.important h2 { border-left-color: #ad4a3d; }
.news ol { list-style: none; padding: 0; margin: 0; }
.news li { padding: .85rem 0; border-bottom: 1px solid var(--line); }
.news li:last-child { border-bottom: 0; padding-bottom: 0; }
.headline { display: block; width: fit-content; color: var(--ink); line-height: 1.42;
            font-weight: 650; text-decoration: none; overflow-wrap: anywhere; }
.headline:hover { color: var(--blue); text-decoration: underline; }
.meta { color: var(--muted); font-size: .82rem; margin-top: .28rem; }
details summary { cursor: pointer; font-weight: 650; }
.coverage { margin: .75rem 0 0; padding-left: 1.25rem; color: var(--muted); line-height: 1.6; }
@media (max-width: 32rem) { body { padding: .75rem; } }
@media print { html { background: #fff; } .hero { background: #fff; color: var(--ink); }
               .hero p { color: var(--muted); } nav { display: none; } .quote, .news li { break-inside: avoid; } }
"""


def fetch_market():
    request = Request(
        "https://api.ninequantai.com/v1/batch",
        data=json.dumps({"symbols": list(MARKET), "type": "quote"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=7) as response:
            result = json.load(response)
        if result["code"] != 0:
            return {}
        return result["data"]["results"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def market_cards(quotes):
    cards = []
    for symbol, index in MARKET.items():
        quote = quotes.get(symbol, {})
        try:
            close = date.fromisoformat(quote["as_of"])
            change = float(quote["change_pct"])
            if not isfinite(change):
                raise ValueError("Invalid return")
            style = "up" if change > 0 else "down" if change < 0 else "flat"
            percent = f"{change:+.2f}%".replace(".", ",")
            value = f'<strong class="{style}">{percent}</strong><small>{close:%d.%m.%Y}</small>'
        except (KeyError, ValueError, TypeError):
            value = '<p class="hint">Дані недоступні</p>'
        ticker = symbol.split(".", 1)[0]
        cards.append(f'<div class="quote"><b>{escape(index)}</b> <small>{ticker}</small>{value}</div>')
    return "\n".join(cards)


def render_html(articles, items, status, quotes, since, until):
    start, end = since.astimezone(KYIV), until.astimezone(KYIV)
    links = []
    sections = []
    for n, (title, group) in enumerate(group_items(articles, items), 1):
        section_id = f"news-{n}"
        style = " important" if title == "Важливе" else ""
        links.append(f'<a href="#{section_id}">{escape(title)}</a>')
        rows = []
        for article in group:
            stamp = article.published.astimezone(KYIV)
            rows.append(f'''<li>
<a class="headline" href="{escape(article.url)}">{escape(article.title)}</a>
<div class="meta">{escape(article.source)} · <time datetime="{stamp.isoformat()}">{stamp:%d.%m, %H:%M}</time></div>
</li>''')
        sections.append(f'<section id="{section_id}" class="panel news{style}">'
                        f'<h2>{escape(title)}</h2><ol>{"".join(rows)}</ol></section>')
    if not sections:
        empty = "Серед зібраних публікацій немає відібраних новин." if articles else "За цей період публікацій не знайдено."
        sections.append(f'<section class="panel"><h2>Новини</h2><p>{empty}</p></section>')
    links.extend(['<a href="#market">Ринки</a>', '<a href="#sources">Джерела</a>'])
    coverage = []
    for source, (count, error) in status.items():
        note = " · неповне покриття" if error else ""
        coverage.append(f"<li>{escape(source)} · {count}{note}</li>")
    return f'''<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Дайджест новин · {end:%d.%m.%Y}</title>
<style>{STYLE}</style>
</head>
<body>
<header class="hero">
<p class="eyebrow">Житомир · Україна · Світ</p>
<h1>Дайджест новин</h1>
<p class="period">{start:%d.%m.%Y, %H:%M} — {end:%d.%m.%Y, %H:%M} за Києвом</p>
<p class="stats">Відібрано <b>{len(items)}</b> із <b>{len(articles)}</b> публікацій</p>
</header>
<nav aria-label="Розділи дайджесту">{" ".join(links)}</nav>
<main>
{"".join(sections)}
<section id="market" class="panel">
<h2>Ринки США</h2>
<p class="hint">Зміна фондів, що відстежують індекси, відносно попереднього закриття.</p>
<div class="market">{market_cards(quotes)}</div>
<p class="hint">Остання доступна сесія з датою під кожним фондом. <a href="{MARKET_SOURCE}">Дані NineQuantAI</a> надходять із затримкою.</p>
</section>
<details id="sources" class="panel"{" open" if any(error for _, error in status.values()) else ""}>
<summary>Покриття джерел</summary><ul class="coverage">{"".join(coverage)}</ul>
</details>
</main>
</body>
</html>
'''
