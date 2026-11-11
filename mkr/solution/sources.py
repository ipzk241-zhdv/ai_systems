from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from subprocess import run
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup


KYIV = ZoneInfo("Europe/Kyiv")


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    published: datetime
    source: str


def fetch(url):
    result = run(
        ["curl", "--fail", "--silent", "--show-error", "--location",
         "--proto", "=https", "--proto-redir", "=https", "--max-time", "12", url],
        capture_output=True,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def page(url):
    return BeautifulSoup(fetch(url), "html.parser", from_encoding="utf-8")


def iso_datetime(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def suspilne(url, source, since):
    for _ in range(8):
        soup = page(url)
        dates = []
        for card in soup.select("article.c-article-card"):
            link = card.select_one("a.c-article-card__headline[href]")
            title = card.select_one(".c-article-card__headline-inner")
            stamp = card.select_one("time[datetime]")
            if link and title and stamp:
                published = iso_datetime(stamp["datetime"])
                dates.append(published)
                yield Article(title.get_text(" ", strip=True), urljoin(url, link["href"]),
                              published, source)
        if not dates:
            raise ValueError("Сторінка не містить новин із датою.")
        next_page = soup.select_one(".pagination .next a[href]")
        if not next_page or min(dates) <= since:
            return
        url = urljoin(url, next_page["href"])
    raise ValueError("Досягнуто ліміту сторінок до початку періоду.")


def zhitomir(since):
    url = "https://www.zhitomir.info/news.html"
    for _ in range(8):
        soup = page(url)
        dates = []
        for card in soup.select(".news-item-list"):
            link = card.select_one("a.news-item-list-link[href]")
            stamp = card.select_one(".news-date")
            if link and stamp:
                published = datetime.strptime(stamp.get_text(strip=True), "%d.%m.%Y, %H:%M")
                published = published.replace(tzinfo=KYIV)
                dates.append(published)
                yield Article(link.get_text(" ", strip=True), urljoin(url, link["href"]),
                              published, "Житомир.info")
        if not dates:
            raise ValueError("Сторінка не містить новин із датою.")
        next_page = soup.select_one(".pagination .next a[href]")
        if not next_page or min(dates) <= since:
            return
        url = urljoin(url, next_page["href"])
    raise ValueError("Досягнуто ліміту сторінок до початку періоду.")


def insider(since):
    root = ElementTree.fromstring(fetch("https://insiderua.net/feed"))
    items = root.findall("./channel/item")
    if not items:
        raise ValueError("У RSS немає публікацій.")
    dates = []
    for item in items:
        published = parsedate_to_datetime(item.findtext("pubDate"))
        dates.append(published)
        yield Article(unescape(item.findtext("title")), item.findtext("link"),
                      published, "INSIDER UA")
    if min(dates) > since:
        raise ValueError("Поточна RSS-стрічка не охоплює весь період.")


def ateo(since, until):
    base = "https://ateobreaking.com"
    root = ElementTree.fromstring(fetch(base + "/sitemap.xml"))
    entries = []
    for item in root.findall("{*}url"):
        slug = urlsplit(item.findtext("{*}loc")).path.strip("/")
        stamp = item.findtext("{*}lastmod")
        if slug and "/" not in slug and stamp:
            entries.append((iso_datetime(stamp), slug))
    if not entries:
        raise ValueError("У карті сайту немає новин.")
    candidates = sorted({entry for entry in entries if since <= entry[0] < until}, reverse=True)
    failures = 0
    for stamp, slug in candidates[:40]:
        url = f"{base}/new/{slug}"
        try:
            tag = page(url).select_one('meta[property="og:title"][content]')
            if not tag:
                raise ValueError("Немає заголовка.")
            yield Article(tag["content"], url, stamp, "Ateo Breaking")
        except ValueError:
            failures += 1
    if failures or len(candidates) > 40 or min(stamp for stamp, _ in entries) > since:
        raise ValueError(f"Неповне покриття карти сайту. Помилок сторінок: {failures}.")


def collect(since, until):
    feeds = {
        "Суспільне Житомир": suspilne("https://suspilne.media/zhytomyr/", "Суспільне Житомир", since),
        "Суспільне Новини": suspilne("https://suspilne.media/latest/", "Суспільне Новини", since),
        "Житомир.info": zhitomir(since),
        "INSIDER UA": insider(since),
        "Ateo Breaking": ateo(since, until),
    }
    articles = {}
    status = {}
    for source, feed in feeds.items():
        urls = set()
        error = ""
        try:
            for article in feed:
                if since <= article.published < until and urlsplit(article.url).scheme == "https":
                    urls.add(article.url)
                    articles.setdefault(article.url, article)
        except (OSError, ValueError, ElementTree.ParseError) as exc:
            error = str(exc)
        status[source] = (len(urls), error)
    return sorted(articles.values(), key=lambda article: article.published, reverse=True), status
