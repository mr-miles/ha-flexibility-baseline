"""The Octopus adapter: shape and transport, never meaning.

Every test here is offline. `FakeTransport` records the requests the client
makes and replays canned bodies, so the interesting assertions are about what
the client *asks for* — which is where the expensive mistakes live.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.flexibility_baseline.daytype import UK_TZ
from custom_components.flexibility_baseline.events import EventType, FlexibilityEvent
from custom_components.flexibility_baseline.octopus import (
    MeterPoint,
    FlexibilityData,
    OctopusAuthError,
    OctopusClient,
    OctopusError,
    SessionResult,
)


class FakeTransport:
    """Replays bodies by URL fragment and records what was asked."""

    def __init__(self, **bodies):
        self.bodies = bodies
        self.requests: list[tuple[str, str, dict]] = []

    async def request(self, method, url, *, headers=None, json=None):
        self.requests.append((method, url, dict(headers or {})))
        for fragment, body in self.bodies.items():
            if fragment in url:
                value = body(json) if callable(body) else body
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"no canned body for {url}")

    def urls(self) -> list[str]:
        return [url for _, url, _ in self.requests]


TOKEN_BODY = {"data": {"obtainKrakenToken": {"token": "tok", "refreshExpiresIn": None}}}
VIEWER_BODY = {"data": {"viewer": {"accounts": [{"number": "A-1"}]}}}


def graphql_router(**named):
    """Route the several GraphQL calls that share one URL, by operation name."""

    def route(payload):
        query = payload["query"]
        for name, body in named.items():
            if name in query:
                return body
        raise AssertionError(f"unrouted query: {query[:80]}")

    return route


def consumption(rows, nxt=None):
    return {"results": rows, "next": nxt}


def row(instant: str, kwh: float):
    return {"interval_start": instant, "consumption": kwh}


async def test_consumption_window_starts_at_local_midnight_not_utc():
    """The BST gotcha: `T00:00:00Z` is 01:00 local and silently loses two periods."""
    transport = FakeTransport(**{"consumption/": consumption([])})
    client = OctopusClient("key", account_number="A-1", transport=transport)

    await client.async_consumption(
        MeterPoint("mpan", "serial"), start=date(2026, 6, 29), end=date(2026, 6, 29)
    )

    url = transport.urls()[0]
    assert "period_from=2026-06-28T23:00:00Z" in url
    assert "period_to=2026-06-29T23:00:00Z" in url


async def test_consumption_follows_pagination():
    pages = iter(
        [
            consumption([row("2026-06-29T00:00:00+01:00", 0.1)], nxt="https://api.octopus.energy/v1/electricity-meter-points/mpan/meters/serial/consumption/?page=2"),
            consumption([row("2026-06-29T00:30:00+01:00", 0.2)]),
        ]
    )
    transport = FakeTransport(**{"electricity-meter-points": lambda _: next(pages)})
    client = OctopusClient("key", account_number="A-1", transport=transport)

    readings = await client.async_consumption(
        MeterPoint("mpan", "serial"), start=date(2026, 6, 29), end=date(2026, 6, 29)
    )

    assert len(readings) == 2
    assert len(transport.urls()) == 2


async def test_export_meter_point_nets_negatively():
    transport = FakeTransport(
        **{
            "graphql": graphql_router(
                ObtainToken=TOKEN_BODY, MeterPoints=None, SavingSessions=None
            ),
            "consumption/": consumption([row("2026-06-29T00:00:00+01:00", 0.4)]),
        }
    )
    client = OctopusClient("key", account_number="A-1", transport=transport)

    readings = await client.async_consumption(
        MeterPoint("mpan", "serial", is_export=True),
        start=date(2026, 6, 29),
        end=date(2026, 6, 29),
    )
    # The sign is applied when meter points are combined, not by the fetch.
    assert readings[datetime(2026, 6, 29, tzinfo=UK_TZ)] == 0.4


async def test_dev_events_are_not_event_days():
    """Octopus's own test fixtures never ran on a real meter."""
    body = {
        "data": {
            "savingSessions": {
                "events": [
                    {
                        "id": 1,
                        "code": "REAL",
                        "startAt": "2026-08-12T17:00:00+00:00",
                        "endAt": "2026-08-12T18:00:00+00:00",
                        "eventType": "TURN_DOWN",
                        "status": "DONE",
                        "rewardPerKwhInOctoPoints": 16,
                        "devEvent": False,
                    },
                    {
                        "id": 2,
                        "code": "DEV",
                        "startAt": "2026-08-13T17:00:00+00:00",
                        "endAt": "2026-08-13T18:00:00+00:00",
                        "eventType": "TURN_DOWN",
                        "status": "DONE",
                        "rewardPerKwhInOctoPoints": 16,
                        "devEvent": True,
                    },
                ],
                "account": {"joinedEvents": []},
            }
        }
    }
    transport = FakeTransport(
        **{
            "api.octopus.energy/v1/graphql": graphql_router(ObtainToken=TOKEN_BODY),
            "api.backend.octopus.energy": body,
        }
    )
    client = OctopusClient("key", account_number="A-1", transport=transport)

    events, _ = await client.async_saving_sessions()
    assert [e.code for e in events] == ["REAL"]


async def test_joined_flag_comes_from_the_joined_event_ids():
    body = {
        "data": {
            "savingSessions": {
                "events": [
                    {
                        "id": 7,
                        "code": "JOINED",
                        "startAt": "2026-08-12T17:00:00+00:00",
                        "endAt": "2026-08-12T18:00:00+00:00",
                        "eventType": "TURN_DOWN",
                        "status": "DONE",
                        "rewardPerKwhInOctoPoints": 16,
                        "devEvent": False,
                    },
                    {
                        "id": 8,
                        "code": "OFFERED",
                        "startAt": "2026-08-13T17:00:00+00:00",
                        "endAt": "2026-08-13T18:00:00+00:00",
                        "eventType": "TURN_DOWN",
                        "status": "DONE",
                        "rewardPerKwhInOctoPoints": 16,
                        "devEvent": False,
                    },
                ],
                "account": {
                    "joinedEvents": [
                        {
                            "eventId": 7,
                            "startAt": "2026-08-12T17:00:00+00:00",
                            "endAt": "2026-08-12T18:00:00+00:00",
                            "eventType": "TURN_DOWN",
                            "resultsStatus": "PARTIAL_SUCCESS",
                            "baselineConsumptionDeltaKwh": 1.05,
                            "consumptionDeltaKwh": 1.08,
                            "rewardGivenInOctoPoints": 16,
                            "rewardGivenInPence": None,
                        }
                    ]
                },
            }
        }
    }
    transport = FakeTransport(
        **{
            "api.octopus.energy/v1/graphql": graphql_router(ObtainToken=TOKEN_BODY),
            "api.backend.octopus.energy": body,
        }
    )
    client = OctopusClient("key", account_number="A-1", transport=transport)

    events, results = await client.async_saving_sessions()
    assert {e.code: e.joined for e in events} == {"JOINED": True, "OFFERED": False}
    assert results[0].settled is True
    assert results[0].baseline_kwh == 1.05


async def test_calculating_session_is_unsettled_not_zero():
    body = {
        "data": {
            "savingSessions": {
                "events": [],
                "account": {
                    "joinedEvents": [
                        {
                            "eventId": 9,
                            "startAt": "2026-08-27T17:00:00+00:00",
                            "endAt": "2026-08-27T18:00:00+00:00",
                            "eventType": "TURN_DOWN",
                            "resultsStatus": "CALCULATING",
                            "baselineConsumptionDeltaKwh": None,
                            "consumptionDeltaKwh": None,
                            "rewardGivenInOctoPoints": None,
                            "rewardGivenInPence": None,
                        }
                    ]
                },
            }
        }
    }
    transport = FakeTransport(
        **{
            "api.octopus.energy/v1/graphql": graphql_router(ObtainToken=TOKEN_BODY),
            "api.backend.octopus.energy": body,
        }
    )
    client = OctopusClient("key", account_number="A-1", transport=transport)

    _, results = await client.async_saving_sessions()
    assert results[0].settled is False
    assert results[0].baseline_kwh is None


async def test_auth_failure_raises_rather_than_returning_nothing():
    """A silently empty event list would inflate every baseline."""
    body = {"errors": [{"message": "Signature of the JWT has expired."}]}
    transport = FakeTransport(
        **{
            "api.octopus.energy/v1/graphql": graphql_router(ObtainToken=TOKEN_BODY),
            "api.backend.octopus.energy": body,
        }
    )
    client = OctopusClient("key", account_number="A-1", transport=transport)

    with pytest.raises(OctopusAuthError):
        await client.async_saving_sessions()


async def test_a_non_auth_graphql_error_is_not_mistaken_for_one():
    body = {"errors": [{"message": "Cannot query field 'savingSessions'"}]}
    transport = FakeTransport(
        **{
            "api.octopus.energy/v1/graphql": graphql_router(ObtainToken=TOKEN_BODY),
            "api.backend.octopus.energy": body,
        }
    )
    client = OctopusClient("key", account_number="A-1", transport=transport)

    with pytest.raises(OctopusError) as excinfo:
        await client.async_saving_sessions()
    assert not isinstance(excinfo.value, OctopusAuthError)


async def test_the_token_is_obtained_once_and_reused():
    transport = FakeTransport(
        **{
            "api.octopus.energy/v1/graphql": graphql_router(
                ObtainToken=TOKEN_BODY, MeterPoints={"data": {"account": {}}}
            )
        }
    )
    client = OctopusClient("key", account_number="A-1", transport=transport)

    await client.async_meter_points()
    await client.async_meter_points()

    obtained = sum(1 for _, _, h in transport.requests if h.get("Authorization") is None)
    assert obtained == 1


async def test_meter_points_are_deduplicated_across_agreements():
    body = {
        "data": {
            "account": {
                "electricityAgreements": [
                    {"meterPoint": {"mpan": "M1", "meters": [{"serialNumber": "S1"}]}},
                    {"meterPoint": {"mpan": "M1", "meters": [{"serialNumber": "S1"}]}},
                ]
            }
        }
    }
    transport = FakeTransport(
        **{
            "api.octopus.energy/v1/graphql": graphql_router(
                ObtainToken=TOKEN_BODY, MeterPoints=body
            )
        }
    )
    client = OctopusClient("key", account_number="A-1", transport=transport)

    assert await client.async_meter_points() == (MeterPoint("M1", "S1"),)


def test_account_data_round_trips_through_serialisation():
    data = FlexibilityData(
        account_number="A-1",
        meter_points=(MeterPoint("M1", "S1"),),
        readings={datetime(2026, 8, 25, tzinfo=timezone.utc): 0.2},
        events=(
            FlexibilityEvent(
                code="E",
                name="E",
                start=datetime(2026, 8, 25, 17, tzinfo=timezone.utc),
                end=datetime(2026, 8, 25, 18, tzinfo=timezone.utc),
                joined=True,
                event_type=EventType.TURN_DOWN,
                event_id=1,
                reward_per_kwh_points=68,
            ),
        ),
        results=(
            SessionResult(
                event_id=1,
                start=datetime(2026, 8, 25, 17, tzinfo=timezone.utc),
                end=datetime(2026, 8, 25, 18, tzinfo=timezone.utc),
                event_type="TURN_DOWN",
                status="PARTIAL_SUCCESS",
                baseline_kwh=1.05,
                consumption_kwh=1.08,
            ),
        ),
        window_start=date(2026, 6, 26),
        window_end=date(2026, 8, 25),
    )

    assert FlexibilityData.from_dict(data.to_dict()) == data


def test_merge_keeps_events_the_api_has_stopped_returning():
    """The 60-day pool reaches back further than the API may retain."""
    old = FlexibilityData(
        account_number="A-1",
        events=(
            FlexibilityEvent(
                code="OLD",
                name="OLD",
                start=datetime(2026, 6, 16, 17, tzinfo=timezone.utc),
                end=datetime(2026, 6, 16, 18, tzinfo=timezone.utc),
                joined=False,
                event_id=1,
            ),
        ),
        readings={datetime(2026, 6, 16, tzinfo=timezone.utc): 0.1},
        window_start=date(2026, 6, 16),
        window_end=date(2026, 6, 16),
    )
    new = FlexibilityData(
        account_number="A-1",
        events=(
            FlexibilityEvent(
                code="NEW",
                name="NEW",
                start=datetime(2026, 8, 12, 17, tzinfo=timezone.utc),
                end=datetime(2026, 8, 12, 18, tzinfo=timezone.utc),
                joined=True,
                event_id=2,
            ),
        ),
        readings={datetime(2026, 8, 12, tzinfo=timezone.utc): 0.2},
        window_start=date(2026, 8, 12),
        window_end=date(2026, 8, 12),
    )

    merged = old.merge(new)
    assert [e.code for e in merged.events] == ["OLD", "NEW"]
    assert len(merged.readings) == 2
    assert merged.window_start == date(2026, 6, 16)
    assert merged.window_end == date(2026, 8, 12)


def test_merge_refuses_to_mix_accounts():
    a = FlexibilityData(account_number="A-1")
    b = FlexibilityData(account_number="A-2")
    with pytest.raises(ValueError):
        a.merge(b)


def test_event_periods_are_local_clock_positions():
    """17:00Z in BST is local 18:00 — periods 36 and 37, not 34 and 35."""
    event = FlexibilityEvent(
        code="E",
        name="E",
        start=datetime(2026, 8, 12, 17, tzinfo=timezone.utc),
        end=datetime(2026, 8, 12, 19, tzinfo=timezone.utc),
        joined=True,
    )
    assert event.periods == (36, 37, 38, 39)
