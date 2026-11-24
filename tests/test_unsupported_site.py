"""An address from a site Ficary does not download from.

Such an address lands on the FanFiction.net adapter, the fallback since
it takes bare story numbers, and the user was told "Expected a URL like
https://www.fanfiction.net/s/12345 or a numeric ID" whatever the site.
It now says Ficary does not download from that site, and which sites it
does. The list is made from the table that picks the adapter; the one
in the command line's help was written out by hand and had fallen nine
sites behind.
"""

from __future__ import annotations

import pytest

from ficary import cli, sites
from ficary.scraper import FFNScraper


@pytest.mark.parametrize(
    "address, host",
    [
        ("https://forums.spacebattles.com/threads/a-story.123/", "forums.spacebattles.com"),
        ("spacebattles.com/threads/a-story.123/", "spacebattles.com"),
        ("http://www.questionablequesting.com/threads/x.1/", "www.questionablequesting.com"),
    ],
)
def test_another_sites_address_is_called_unsupported(address, host):
    with pytest.raises(ValueError) as error:
        FFNScraper.parse_story_id(address)
    message = str(error.value)
    assert message.startswith(f"Ficary does not download from {host}. It works with: fanfiction.net, ")
    assert "archiveofourown.org" in message
    assert "Expected a URL like https://www.fanfiction.net" not in message


def test_a_fanfiction_net_address_that_is_not_a_story_keeps_its_own_message():
    with pytest.raises(ValueError) as error:
        FFNScraper.parse_story_id("https://www.fanfiction.net/u/123/Someone")
    assert "Expected a URL like https://www.fanfiction.net/s/12345" in str(error.value)


@pytest.mark.parametrize("text", ["12345", "https://www.fanfiction.net/s/12345/1/A-Story", "m.fanfiction.net/s/12345"])
def test_story_numbers_and_addresses_still_work(text):
    assert FFNScraper.parse_story_id(text) == 12345


def test_the_list_is_every_site_in_the_table():
    listed = sites.supported_sites()
    assert listed[0] == "fanfiction.net"
    assert len(listed) == len(set(listed))
    for hostname, _scraper_cls in sites._HOSTNAME_TO_SCRAPER:
        if hostname == "ao3.org":
            assert "ao3.org" not in listed  # an alias of archiveofourown.org
        elif hostname == "tapatalk.com":
            assert "tapatalk.com/groups/themousepad" in listed
        else:
            assert hostname in listed


def test_the_command_lines_help_lists_them_all(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    for site in ("scribblehub.com", "subscribestar.adult", "webnovel.com", "readonlymind.com", "giantessworld.net"):
        assert site in help_text


def test_the_command_line_says_so_for_an_unsupported_address(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["https://forums.spacebattles.com/threads/a-story.123/"])
    assert exit_info.value.code == 1
    output = capsys.readouterr()
    assert "Ficary does not download from forums.spacebattles.com" in output.out + output.err
