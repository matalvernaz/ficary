"""V9: mirrors._bucket_by_title_prefix strips leading articles before
bucketing so "The Dragon" and "A Dragon" land in the same bucket.
Without this, mirror pairs with article-drift between sites are
silently never compared."""

from __future__ import annotations

from pathlib import Path

from ffn_dl.library.mirrors import _bucket_by_title_prefix, normalise_title


def _record(title: str, author: str = "Author", url: str = "https://a/1"):
    # Construct a minimal _StoryRecord-like duck. The bucket function
    # only reads ``title_norm``; the rest is unused for this test.
    from ffn_dl.library.mirrors import StoryKey, _StoryRecord

    return _StoryRecord(
        key=StoryKey(root="/root", url=url),
        title_norm=normalise_title(title),
        title_tokens=set(normalise_title(title).split()),
        author_norm=normalise_title(author),
        relpath="x.epub",
        abs_path=Path("/x.epub"),
        site="x.com",
        raw_title=title,
        raw_author=author,
    )


def test_leading_article_drift_lands_in_same_bucket():
    """Two records with leading article drift now compare against each
    other instead of landing in different buckets."""
    a = _record("The Dragon Heart")
    b = _record("A Dragon Heart")
    buckets = _bucket_by_title_prefix([a, b])
    # Both should share one bucket keyed on the post-stopword pair.
    assert len(buckets) == 1


def test_mid_title_article_kept():
    """A mid-title article is signal — must not be stripped, otherwise
    "Harry and the Half-Blood Prince" collapses with "Harry Half-Blood
    Prince" and produces real collisions."""
    a = _record("Harry and the Half Blood Prince")
    b = _record("Harry Half Blood Prince")
    buckets = _bucket_by_title_prefix([a, b])
    # Different bucket keys because "and" / "the" are mid-title, not
    # leading, so they stay in their tokens.
    assert len(buckets) == 2
