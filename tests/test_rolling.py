"""Tests for rolling feeds (episodes come out on a schedule). Run with:  pytest"""

import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import rolling
from app.db import Episode, Feed, FeedRelease
from test_feeds import ITUNES, client, episode_ids, read, token  # noqa: F401 (the fixture)


def daily(time="06:00", zone="America/New_York") -> Feed:
    return Feed(rolling=True, roll_every="day", roll_time=time, roll_zone=zone)


# ------------------------------------------------------------ the clock

def test_the_same_local_time_when_the_clocks_change():
    feed = daily()
    # Spring forward (Sunday, March 8th, 2026): 6 AM is 11:00 UTC before, 10:00 after.
    found = rolling.release_times(feed, datetime(2026, 3, 6, 12), datetime(2026, 3, 9, 12))
    assert found == [datetime(2026, 3, 7, 11), datetime(2026, 3, 8, 10), datetime(2026, 3, 9, 10)]
    # Fall back (Sunday, November 1st, 2026).
    found = rolling.release_times(feed, datetime(2026, 10, 31, 0), datetime(2026, 11, 2, 0))
    assert found == [datetime(2026, 10, 31, 10), datetime(2026, 11, 1, 11)]


def test_a_time_that_doesnt_exist_on_the_spring_forward_day():
    # 2:30 AM never happens on March 8th, 2026 in New York: still one release that day.
    found = rolling.release_times(daily("02:30"), datetime(2026, 3, 7, 12), datetime(2026, 3, 9, 0))
    assert len(found) == 1


def test_other_time_zones():
    london = daily("19:15", "Europe/London")
    assert rolling.release_times(london, datetime(2026, 7, 1, 0), datetime(2026, 7, 1, 23)) == \
        [datetime(2026, 7, 1, 18, 15)]           # summer time: an hour ahead of UTC
    assert rolling.release_times(london, datetime(2026, 12, 1, 0), datetime(2026, 12, 1, 23)) == \
        [datetime(2026, 12, 1, 19, 15)]
    # Releases exactly at the last look aren't counted twice.
    assert rolling.release_times(london, datetime(2026, 12, 1, 19, 15), datetime(2026, 12, 2, 19, 14)) == []


def test_weekdays_skip_the_weekend():
    feed = daily()
    feed.roll_every = "weekdays"
    # Friday, November 20th to Tuesday, November 24th, 2026.
    found = rolling.release_times(feed, datetime(2026, 11, 20, 0), datetime(2026, 11, 24, 23))
    assert [d.day for d in found] == [20, 23, 24]
    now = datetime(2026, 11, 20, 12)             # Friday, after 6 AM
    assert rolling.when_words(feed, rolling.next_release(feed, now), now) == \
        "Monday, November 23rd at 6:00 AM"


def test_once_a_week():
    feed = daily()
    feed.roll_every, feed.roll_weekday = "week", 5     # Saturdays
    found = rolling.release_times(feed, datetime(2026, 11, 1, 0), datetime(2026, 11, 30, 0))
    assert [d.day for d in found] == [7, 14, 21, 28]
    assert rolling.schedule_words(feed) == "One episode every Saturday at 6:00 AM (US Eastern)"


def test_every_few_days_counts_from_the_first_publish():
    feed = daily("20:00")
    feed.roll_every, feed.roll_days = "days", 3
    feed.roll_since = datetime(2026, 11, 2, 1)   # Sunday, November 1st, 8 PM in New York
    found = rolling.release_times(feed, datetime(2026, 11, 1, 0), datetime(2026, 11, 12, 0))
    assert [rolling._local_day(feed, d).day for d in found] == [1, 4, 7, 10]
    assert rolling.schedule_words(feed) == "One episode every 3 days at 8:00 PM (US Eastern)"
    now = datetime(2026, 11, 5, 12)
    assert rolling.when_words(feed, rolling.next_release(feed, now), now) == \
        "Saturday, November 7th at 8:00 PM"


def test_how_the_next_release_is_described():
    feed = daily("18:00")
    now = datetime(2026, 11, 22, 15)             # Sunday, 10 AM in New York
    assert rolling.when_words(feed, datetime(2026, 11, 22, 23), now) == "today at 6:00 PM"
    assert rolling.when_words(feed, datetime(2026, 11, 23, 23), now) == "tomorrow at 6:00 PM"
    assert rolling.when_words(feed, datetime(2026, 11, 24, 23), now) == "Tuesday, November 24th at 6:00 PM"
    assert rolling.schedule_words(feed) == "One episode every day at 6:00 PM (US Eastern)"


# ------------------------------------------------------------ in the app

def make_rolling(client, series="Suspense", start="2", time="06:00", zone="America/New_York") -> int:
    sent = client.post("/feeds", data={"series": series, "title": series, "release": "rolling",
                                       "roll_time": time, "roll_zone": zone, "roll_start": start},
                       follow_redirects=False)
    assert sent.status_code == 303
    return int(re.search(r"/feeds/(\d+)", sent.headers["location"])[1])


def titles(client, feed_id) -> list[str]:
    return [item.findtext("title") for item in read(client, feed_id).findall("item")]


def go_back_in_time(client, feed_id, days):
    """As if the feed was last looked at (and everything came out) `days` ago."""
    with Session(client.engine) as session:
        feed = session.get(Feed, feed_id)
        feed.roll_checked_at -= timedelta(days=days)
        for row in session.scalars(select(FeedRelease).where(FeedRelease.feed_id == feed_id)):
            row.released_at -= timedelta(days=days)
        session.commit()


def test_a_rolling_feed(client):
    page = client.get("/feeds/new?series=Suspense").text
    assert 'value="rolling"' in page and "every weekday (Monday to Friday)" in page
    feed_id = make_rolling(client)
    page = client.get(f"/feeds/{feed_id}").text
    assert "When it's published, the first 2" in page and "Release more now" not in page
    assert client.get(f"/feeds/{token(client, feed_id)}/feed.xml").status_code == 404   # a draft

    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    assert titles(client, feed_id) == ["The Hitchhiker", "Sorry, Wrong Number"]
    page = client.get(f"/feeds/{feed_id}").text
    assert "2 of 4 out." in page and "Next: <em>Untitled Episode</em>" in page
    assert "One episode every day at 6:00 AM (US Eastern)" in page and "Release more now" in page
    assert "rolling: 2 of 4 out" in client.get("/feeds").text

    # A recording not out yet can't be fetched.
    c = client.ids["c.mp3"]
    assert client.get(f"/feeds/{token(client, feed_id)}/audio/{c}.mp3").status_code == 404
    assert client.get(f"/feeds/{token(client, feed_id)}/audio/{client.ids['a.mp3']}.mp3").status_code == 200

    # Nobody looked for three days: the two left come out, each at its own 6 AM.
    go_back_in_time(client, feed_id, 3)
    channel = read(client, feed_id)
    assert [i.findtext("title") for i in channel.findall("item")][2:] == ["Untitled Episode", "Donovan's Brain"]
    for item in channel.findall("item")[2:]:
        when = parsedate_to_datetime(item.findtext("pubDate"))
        assert when.hour in (10, 11) and when.minute == 0     # 6 AM in New York, in UTC
    assert "All 4 episodes are out." in client.get(f"/feeds/{feed_id}").text


def test_release_more_now(client):
    feed_id = make_rolling(client, start="0")
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    assert titles(client, feed_id) == []
    page = client.post(f"/feeds/{feed_id}/release", data={"count": "3"}).text
    assert "3 more episodes came out" in page
    assert titles(client, feed_id) == ["The Hitchhiker", "Sorry, Wrong Number", "Untitled Episode"]
    page = client.post(f"/feeds/{feed_id}/release", data={"count": "5"}).text
    assert "1 more episode came out" in page and "All 4 episodes are out." in page


def test_an_episode_in_parts_comes_out_whole(client):
    feed_id = make_rolling(client, series="Coast to Coast AM", start="1")
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    assert titles(client, feed_id) == ["Art Bell (part 1 of 2)", "Art Bell (part 2 of 2)"]


def test_a_draft_again_doesnt_catch_up_on_return(client):
    feed_id = make_rolling(client, start="1")
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    client.post(f"/feeds/{feed_id}/publish", data={"published": "0"})
    go_back_in_time(client, feed_id, 3)
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    assert titles(client, feed_id) == ["The Hitchhiker"]   # the missed days didn't all come out


def test_a_new_episode_takes_its_air_date_place(client):
    feed_id = make_rolling(client, start="2")
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    with Session(client.engine) as session:     # Donovan's Brain turns out to be from 1941
        session.get(Episode, episode_ids(client, "d.flac")[0]).air_year = 1941
        session.commit()
    client.post(f"/feeds/{feed_id}/release", data={"count": "1"})
    assert titles(client, feed_id) == ["The Hitchhiker", "Sorry, Wrong Number", "Donovan's Brain"]


def test_the_choice_is_fixed_once_published(client):
    feed_id = make_rolling(client, start="1")
    page = client.get(f"/feeds/{feed_id}/edit").text
    assert 'value="all"' in page and "can't be changed once the feed is published" in page
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    page = client.get(f"/feeds/{feed_id}/edit").text
    assert 'value="all"' not in page and 'name="roll_start"' not in page
    client.post(f"/feeds/{feed_id}/edit", data={"title": "Suspense", "release": "all",
                                               "roll_time": "07:30", "roll_zone": "America/Chicago"})
    with Session(client.engine) as session:
        feed = session.get(Feed, feed_id)
        assert feed.rolling and feed.roll_time == "07:30" and feed.roll_zone == "America/Chicago"
    # An all-at-once feed, once published, can't become rolling.
    other = make_rolling(client, start="1")
    client.post(f"/feeds/{other}/edit", data={"title": "Suspense", "release": "all"})
    client.post(f"/feeds/{other}/publish", data={"published": "1"})
    client.post(f"/feeds/{other}/edit", data={"title": "Suspense", "release": "rolling"})
    with Session(client.engine) as session:
        assert not session.get(Feed, other).rolling
    assert len(titles(client, other)) == 4


def test_choosing_how_often(client):
    sent = client.post("/feeds", data={"series": "Suspense", "title": "S", "release": "rolling",
                                       "roll_every": "week", "roll_weekday": "6", "roll_days": "",
                                       "roll_time": "09:00", "roll_zone": "America/Chicago",
                                       "roll_start": "1"}, follow_redirects=False)
    feed_id = int(re.search(r"/feeds/(\d+)", sent.headers["location"])[1])
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    page = client.get(f"/feeds/{feed_id}").text
    assert "One episode every Sunday at 9:00 AM (US Central)" in page
    page = client.get(f"/feeds/{feed_id}/edit").text
    assert '<option value="week" selected>' in page and '<option value="6" selected>Sunday' in page
    # Changed after publishing: every 4 days, counted from when it was published.
    client.post(f"/feeds/{feed_id}/edit", data={"title": "S", "release": "rolling", "roll_every": "days",
                                               "roll_days": "4", "roll_time": "09:00",
                                               "roll_zone": "America/Chicago"})
    with Session(client.engine) as session:
        feed = session.get(Feed, feed_id)
        assert (feed.roll_every, feed.roll_days, feed.roll_weekday) == ("days", 4, None)
        assert feed.roll_since is not None
    assert "One episode every 4 days at 9:00 AM" in client.get(f"/feeds/{feed_id}").text


def test_the_form_checks_the_schedule(client):
    sent = client.post("/feeds", data={"series": "Suspense", "title": "S", "release": "rolling",
                                       "roll_time": "25:00", "roll_zone": "Mars", "roll_start": "x",
                                       "roll_every": "days", "roll_days": "1"})
    assert sent.status_code == 400
    assert "Give a time of day" in sent.text and "Pick a time zone" in sent.text
    assert "A number from 0 to 999" in sent.text and "A number of days from 2 to 365" in sent.text
    # The time zone used last is suggested next time.
    make_rolling(client, zone="Europe/London")
    assert '<option value="Europe/London" selected>' in client.get("/feeds/new?series=Suspense").text


def test_taking_episodes_out_of_a_hand_picked_rolling_feed(client):
    ids = episode_ids(client, "a.mp3", "b.mp3", "c.mp3")
    sent = client.post("/feeds", data={"ids": ids, "title": "Picks", "release": "rolling",
                                       "roll_time": "06:00", "roll_zone": "UTC", "roll_start": "2"},
                       follow_redirects=False)
    feed_id = int(re.search(r"/feeds/(\d+)", sent.headers["location"])[1])
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    page = client.get(f"/feeds/{feed_id}").text
    assert page.count('class="coming"') == 1 and ">next<" in page
    client.post(f"/feeds/{feed_id}/remove", data={"ids": [ids[0]]})
    with Session(client.engine) as session:
        assert session.scalar(select(FeedRelease).where(FeedRelease.episode_id == ids[0])) is None
    assert titles(client, feed_id) == ["Sorry, Wrong Number"]
