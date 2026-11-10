"""Browse FFN's category and community directories using their native ordering and pagination."""

import re
from functools import lru_cache
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from . import search


CATEGORIES = {
    "anime": ("Anime / Manga", 201),
    "book": ("Books", 202),
    "cartoon": ("Cartoons", 203),
    "comic": ("Comics", 204),
    "game": ("Games", 209),
    "misc": ("Miscellaneous", 211),
    "movie": ("Movies", 205),
    "play": ("Plays / Musicals", 207),
    "tv": ("TV Shows", 208),
}
COMMUNITY_SORT = {"followers": 3, "stories": 2, "newest": 4, "staff": 1}
COMMUNITY_SEARCH_SORT = {"best match": None, "newest": "datecreated"}
DIRECTORY_SORT = ("name", "stories")
COMMUNITY_FANDOM_SORT = ("name", "communities")
COMMUNITY_STORY_SORT = ("archive date", *search.FFN_FANDOM_SORT)

# Community pages use rating ranges and exact ratings with different IDs from fandom pages.
COMMUNITY_RATING = {"all": 99, "K": 1, "K+": 12, "T": 13, "M": 14, "K-T": 3}


def _count(text):
    match = re.search(r"([\d,]+(?:\.\d+)?)\s*([KkMm]?)", text)
    if not match:
        return 0
    multiplier = {"k": 1000, "m": 1000000}.get(match[2].lower(), 1)
    return int(float(match[1].replace(",", "")) * multiplier)


def _category_rows(communities=False):
    rows = []
    for category, (title, _) in CATEGORIES.items():
        rows.append({
            "kind": "category", "title": title, "category": category,
            "url": f"{search.FFN_BASE}/{category}/", "summary": f"Browse {title} fandoms.",
        })
    if communities:
        rows.append({
            "kind": "general", "title": "General communities", "category": "general",
            "url": f"{search.FFN_BASE}/communities/general/", "summary": "Communities covering multiple fandoms or general themes.",
        })
    return rows


def parse_fandoms(html, category, *, communities=False):
    """Read the complete desktop directory, including small fandoms outside the mobile top 200."""
    soup = BeautifulSoup(html, "lxml")
    prefix = f"/communities/{category}/" if communities else f"/{category}/"
    pattern = re.compile(re.escape(prefix) + r"([^/]+)/?$")
    rows = []
    seen = set()
    for link in soup.select("a[href]"):
        path = urlparse(urljoin(search.FFN_BASE, link["href"])).path
        match = pattern.fullmatch(path)
        if not match or path in seen:
            continue
        seen.add(path)
        title = search._extract_title(link)
        # Desktop story and community directories put the count in the next span.
        count_tag = link.find_next_sibling("span")
        count = _count(count_tag.get_text()) if count_tag else 0
        rows.append({
            "kind": "fandom", "title": title, "category": category,
            "slug": match[1], "url": f"{search.FFN_BASE}/{category}/{match[1]}/",
            "communities" if communities else "stories": count,
            "summary": f"{title}: {count:,} {'communities' if communities else 'stories'}.",
        })
    if not rows:
        raise RuntimeError("FFN's fandom directory could not be read. Please try again later.")
    if soup.select_one("select[name='l']"):
        raise RuntimeError("FFN returned an incomplete mobile directory. Please try the desktop directory again later.")
    return rows


def parse_communities(html):
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for element in soup.select("div.z-list"):
        link = element.find("a", href=re.compile(r"^/community/[^/]+/\d+"))
        if link is None:
            continue
        metadata = element.select_one(".z-padtop2")
        metadata_text = metadata.get_text(" ", strip=True) if metadata else ""
        description = element.select_one(".z-indent")
        summary_parts = []
        if description:
            for child in description.children:
                if child is metadata:
                    break
                summary_parts.append(child.get_text(" ", strip=True) if hasattr(child, "get_text") else str(child))
        row = {
            "kind": "community", "title": search._extract_title(link),
            "url": urljoin(search.FFN_BASE, link["href"]),
            "summary": " ".join(" ".join(summary_parts).split()) + "\n" + metadata_text,
        }
        for field, label in (("stories", "Archive"), ("followers", "Followers"), ("staff", "Staff")):
            match = re.search(rf"{label}:\s*([\d,]+)", metadata_text)
            row[field] = int(match[1].replace(",", "")) if match else 0
        for field, label in (("created", "Since"), ("founder", "Founder")):
            match = re.search(rf"{label}:\s*(.*?)(?:\s+-\s+|$)", metadata_text)
            row[field] = match[1].strip() if match else ""
        rows.append(row)
    return rows


def _has_next(html, current_url):
    soup = BeautifulSoup(html, "lxml")
    current = urlparse(current_url)
    current_parts = current.path.strip("/").split("/")
    for link in soup.select("a[href]"):
        label = link.get_text(" ", strip=True).casefold()
        if label not in ("next", "next »", "next ›", "next >", "next page"):
            continue
        destination = urlparse(urljoin(current_url, link["href"]))
        if destination.netloc != current.netloc:
            continue
        if current.path == "/search/":
            current_page = parse_qs(current.query).get("ppage", ["1"])[0]
            next_page = parse_qs(destination.query).get("ppage", ["0"])[0]
            if destination.path == current.path and current_page.isdigit() and next_page.isdigit():
                if int(next_page) == int(current_page) + 1:
                    return True
            continue
        # Community story forms put the page in segment 5; directory forms put it last.
        page_index = 5 if current_parts[0] == "community" else len(current_parts) - 1
        next_parts = destination.path.strip("/").split("/")
        if len(next_parts) <= page_index or next_parts[:page_index] != current_parts[:page_index]:
            continue
        if next_parts[page_index].isdigit() and current_parts[page_index].isdigit():
            if int(next_parts[page_index]) == int(current_parts[page_index]) + 1:
                return True
    return False


def _fandom_scope(filters):
    # Browser selections carry the exact link, so unusual punctuation never needs to be guessed.
    path = filters.get("fandom_path")
    if path and filters.get("fandom") == filters.get("fandom_label"):
        match = re.fullmatch(r"/([a-z]+)/([^/]+)/", path)
        if match and match[1] in CATEGORIES:
            return match[1], match[2]
    resolved = search._resolve_fandom(filters.get("fandom"), filters.get("category"))
    if resolved and not resolved[0]:
        raise ValueError("Please choose a category for this fandom, or select it from the category browser.")
    return resolved


def _directory(query, filters, page, *, communities=False):
    if page > 1:
        return search.SearchPage(exhausted=True)
    category = filters.get("category", "any")
    if category == "any":
        rows = _category_rows(communities)
    else:
        if category not in CATEGORIES:
            raise ValueError("Please choose one of FFN's categories.")
        prefix = "communities/" if communities else ""
        html = search._fetch_search_page(f"{search.FFN_BASE}/{prefix}{category}/")
        rows = parse_fandoms(html, category, communities=communities)
    if query:
        rows = [row for row in rows if query.casefold() in row["title"].casefold()]
    order = filters.get("sort", "name")
    allowed = COMMUNITY_FANDOM_SORT if communities else DIRECTORY_SORT
    if order not in allowed:
        raise ValueError(f"Choose a directory order: {', '.join(allowed)}.")
    if order == "name":
        rows.sort(key=lambda row: row["title"].casefold())
    else:
        rows.sort(key=lambda row: (-row.get(order, 0), row["title"].casefold()))
    return search.SearchPage(rows, exhausted=True)


@lru_cache(maxsize=128)
def _fandom_id(category, slug):
    """Fandom IDs are stable; resolve once instead of refetching the story page on every Load More."""
    html = search._fetch_search_page(f"{search.FFN_BASE}/{category}/{slug}/")
    soup = BeautifulSoup(html, "lxml")
    crossover = soup.find("a", href=re.compile(r"^/crossovers/[^/]+/\d+/?$"))
    if crossover is None:
        raise RuntimeError("FFN's fandom search ID could not be read. Please try browsing its communities without a query.")
    return urlparse(crossover["href"]).path.rstrip("/").split("/")[-1]


def _community_search_url(query, filters, page, scope):
    params = {"ready": 1, "type": "community", "keywords": query, "ppage": page}
    order = filters.get("sort", "best match")
    if order not in COMMUNITY_SEARCH_SORT:
        raise ValueError("Community keyword search can be sorted by best match or newest.")
    if COMMUNITY_SEARCH_SORT[order]:
        params["sort"] = COMMUNITY_SEARCH_SORT[order]
    category = filters.get("category", "any")
    if category in CATEGORIES:
        params["categoryid"] = CATEGORIES[category][1]
    elif category == "general":
        params["categoryid"] = -1
    if scope:
        params["categoryid"] = _fandom_id(*scope)
    match = search._resolve_filter(filters.get("match"), search.FFN_MATCH, "match")
    if match:
        params["match"] = match
    return f"{search.FFN_BASE}/search/?{urlencode(params)}"


def search_communities(query, *, page=1, **filters):
    scope = _fandom_scope(filters)
    if query:
        url = _community_search_url(query, filters, page, scope)
    elif scope or filters.get("category") == "general":
        order = filters.get("sort", "followers")
        if order not in COMMUNITY_SORT:
            raise ValueError(f"Choose a community order: {', '.join(COMMUNITY_SORT)}.")
        language = search._resolve_filter(filters.get("language"), search.FFN_LANGUAGE, "language") or 0
        category, slug = scope if scope else ("general", "")
        directory = f"{category}/{slug}/" if slug else f"{category}/"
        url = f"{search.FFN_BASE}/communities/{directory}{language}/{COMMUNITY_SORT[order]}/{page}/"
    else:
        return _directory(query, filters, page, communities=True)
    html = search._fetch_search_page(url)
    return search.SearchPage(parse_communities(html), exhausted=not _has_next(html, url))


def community_story_url(url, filters, page):
    """Build the path-based community form; query-string fandom filters do not work here."""
    parsed = urlparse(url)
    if parsed.hostname not in ("fanfiction.net", "www.fanfiction.net", "m.fanfiction.net"):
        raise ValueError("Please enter a FanFiction.net community URL.")
    match = re.fullmatch(r"/community/([^/]+)/(\d+)(?:/.*)?", parsed.path)
    if not match:
        raise ValueError("Please enter a community URL such as https://www.fanfiction.net/community/Name/123/.")
    order = filters.get("sort", "archive date")
    if order not in COMMUNITY_STORY_SORT:
        raise ValueError(f"Choose a community story order: {', '.join(COMMUNITY_STORY_SORT)}.")
    rating = search._resolve_filter(filters.get("rating", "all"), COMMUNITY_RATING, "rating")
    genre = search._resolve_filter(filters.get("genre"), search.FFN_GENRE, "genre") or 0
    status = search._resolve_filter(filters.get("status"), search.FFN_STATUS, "status") or 0
    time = search._resolve_filter(filters.get("time"), search.FFN_TIME, "time") or 0
    words = filters.get("min_words", "any")
    length = search._FFN_WORDS_TO_LEN.get(words, 0)
    if words not in ("any", "", None) and words not in search._FFN_WORDS_TO_LEN:
        raise ValueError("Please choose a word length from the list.")
    sort = search.FFN_FANDOM_SORT.get(order, 0)
    return f"{search.FFN_BASE}/community/{match[1]}/{match[2]}/{rating}/{sort}/{page}/{genre}/{length}/{status}/{time}/"


def search_community_stories(query, *, community, page=1, **filters):
    url = community_story_url(community, filters, page)
    html = search._fetch_search_page(url)
    rows = search._parse_results(html)
    # FFN offers coarser length buckets than the shared search controls. Filter the remainder after fetching the nearest lower bucket.
    length = re.fullmatch(r"(\d+)k\+", filters.get("min_words", ""))
    if length:
        minimum = int(length[1]) * 1000
        rows = [row for row in rows if _count(str(row.get("words", ""))) >= minimum]
    if query:
        match_in = filters.get("match", "any")
        fields = ("title", "summary") if match_in == "any" else (match_in,)
        rows = [row for row in rows if query.casefold() in " ".join(row.get(field, "") for field in fields).casefold()]
    return search.SearchPage(rows, exhausted=not _has_next(html, url))


def search_catalog(query, *, browse="stories", community="", page=1, **filters):
    """Dispatch the FFN window without changing ordinary CLI story searches."""
    if browse == "categories":
        return _directory(query, filters, page)
    if browse == "communities":
        return search_communities(query, page=page, **filters)
    if browse != "stories":
        raise ValueError("Please choose stories, communities, or categories.")
    if community:
        return search_community_stories(query, community=community, page=page, **filters)
    scope = _fandom_scope(filters) if filters.get("fandom_path") else None
    if scope:
        return search._search_ffn_fandom(query, scope[0], scope[1], filters, page)
    return search.search_ffn(query, page=page, **filters)
