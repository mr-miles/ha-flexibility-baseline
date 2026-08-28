"""Every Octopus network call this component makes.

Nothing else in the component talks to Octopus. The client's whole output is one
:class:`~.models.FlexibilityData`, so the calculator has no reason to know
that three different APIs, two auth schemes and a pagination loop sit behind it.

Three endpoints are involved, and the split is not obvious — it cost a long
detour to establish (PLAN.md §4.2):

============================================  ==========  ==========================
Endpoint                                      Auth        Provides
============================================  ==========  ==========================
``api.octopus.energy/v1/graphql/``            API key     Kraken token, account,
                                                          meter points
``api.octopus.energy/v1/.../consumption/``    HTTP Basic  settled half-hourly kWh
``api.backend.octopus.energy/v1/graphql/``    token       ``savingSessions``
============================================  ==========  ==========================

The third — "OEBE" — is the only one carrying Saving Sessions. It is *not*
reachable on the public Kraken API, where the field does not exist, nor on the
dashboard gateway. It does accept the token derived from an ordinary API key, so
no browser session or scraped cookie is needed.

Authentication failure must surface as :class:`OctopusAuthError` and be raised to
Home Assistant as a re-auth, never swallowed into an empty event list: a silently
empty list drops Event Days from the pool and *inflates* every baseline, which is
the one failure mode a user would never notice.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from ..daytype import UK_TZ, period_starts
from ..events import EventType, FlexibilityEvent
from ..data import FlexibilityData, MeterPoint, SessionResult, utcnow

KRAKEN_URL = "https://api.octopus.energy/v1/graphql/"
OEBE_URL = "https://api.backend.octopus.energy/v1/graphql/"
REST_ROOT = "https://api.octopus.energy/v1"

#: The API rejects ``+00:00`` offsets on these parameters and wants ``Z``.
_API_INSTANT = "%Y-%m-%dT%H:%M:%SZ"

#: Well above a 60-day pull (2832 intervals observed), so the pagination loop is
#: a correctness backstop rather than the normal path.
_PAGE_SIZE = 25_000

#: Renew a little early rather than discover expiry mid-fetch.
_TOKEN_MARGIN = timedelta(minutes=5)


class OctopusError(Exception):
    """Any failure talking to Octopus."""


class OctopusAuthError(OctopusError):
    """Credentials were rejected. Must reach the user, not the logs."""


@runtime_checkable
class Transport(Protocol):
    """The seam that keeps every test in this repo offline."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any | None = None,
    ) -> Any:
        """Perform one HTTP request and return the decoded JSON body."""


class AiohttpTransport:
    """The real transport. ``aiohttp`` is imported lazily so that importing this
    module — and therefore the models — costs nothing outside Home Assistant."""

    def __init__(self, session: Any | None = None) -> None:
        self._session = session
        self._owned: Any | None = None

    async def _get_session(self) -> Any:
        if self._session is not None:
            return self._session
        if self._owned is None:
            import aiohttp  # noqa: PLC0415 — optional outside Home Assistant

            self._owned = aiohttp.ClientSession()
        return self._owned

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any | None = None,
    ) -> Any:
        session = await self._get_session()
        async with session.request(
            method, url, headers=dict(headers or {}), json=json
        ) as response:
            if response.status in (401, 403):
                raise OctopusAuthError(f"{response.status} from {url}")
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise OctopusError(f"{response.status} from {url}: {body}")
            return body

    async def aclose(self) -> None:
        if self._owned is not None:
            await self._owned.close()
            self._owned = None


class OctopusClient:
    """Fetches one :class:`FlexibilityData`.

    The client holds no interpretation and no state beyond a cached auth token.
    Give it an API key and a window; get back data.
    """

    def __init__(
        self,
        api_key: str,
        *,
        account_number: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._api_key = api_key
        self._account_number = account_number
        self._transport: Transport = transport or AiohttpTransport()
        self._token: str | None = None
        self._token_expires: datetime | None = None

    # ---------------------------------------------------------------- auth

    async def async_token(self) -> str:
        """A Kraken token, obtained once and reused until it nears expiry."""
        now = utcnow()
        if (
            self._token is not None
            and self._token_expires is not None
            and now + _TOKEN_MARGIN < self._token_expires
        ):
            return self._token

        payload = await self._graphql(
            KRAKEN_URL,
            """
            mutation ObtainToken($key: String!) {
              obtainKrakenToken(input: {APIKey: $key}) {
                token
                refreshExpiresIn
              }
            }
            """,
            {"key": self._api_key},
            authenticated=False,
        )
        node = (payload or {}).get("obtainKrakenToken") or {}
        token = node.get("token")
        if not token:
            raise OctopusAuthError("no token returned for the supplied API key")
        self._token = token
        expires = node.get("refreshExpiresIn")
        self._token_expires = (
            datetime.fromtimestamp(expires, tz=UK_TZ)
            if isinstance(expires, (int, float))
            else now + timedelta(hours=1)
        )
        return token

    # ------------------------------------------------------------ discovery

    async def async_account_number(self) -> str:
        """The account number, asked for once and remembered."""
        if self._account_number:
            return self._account_number
        payload = await self._graphql(
            KRAKEN_URL, "query { viewer { accounts { number } } }", {}
        )
        accounts = ((payload or {}).get("viewer") or {}).get("accounts") or []
        if not accounts:
            raise OctopusError("the API key is not attached to any account")
        self._account_number = accounts[0]["number"]
        return self._account_number

    async def async_meter_points(self) -> tuple[MeterPoint, ...]:
        """Active electricity meter points, for the config flow's picker."""
        account = await self.async_account_number()
        payload = await self._graphql(
            KRAKEN_URL,
            """
            query MeterPoints($account: String!) {
              account(accountNumber: $account) {
                electricityAgreements(active: true) {
                  meterPoint { mpan meters { serialNumber } }
                }
              }
            }
            """,
            {"account": account},
        )
        agreements = ((payload or {}).get("account") or {}).get(
            "electricityAgreements"
        ) or []
        points: list[MeterPoint] = []
        seen: set[tuple[str, str]] = set()
        for agreement in agreements:
            point = agreement.get("meterPoint") or {}
            mpan = point.get("mpan")
            for meter in point.get("meters") or []:
                serial = meter.get("serialNumber")
                if mpan and serial and (mpan, serial) not in seen:
                    seen.add((mpan, serial))
                    points.append(MeterPoint(mpan=mpan, serial=serial))
        return tuple(points)

    # ------------------------------------------------------------ the fetch

    async def async_account_data(
        self,
        *,
        start: date,
        end: date,
        meter_points: Sequence[MeterPoint] | None = None,
    ) -> FlexibilityData:
        """Everything the calculator needs for local days ``start..end``.

        Consumption for several meter points is summed, export negated, so the
        readings are already net.
        """
        account = await self.async_account_number()
        points = tuple(meter_points) if meter_points else await self.async_meter_points()

        readings: dict[datetime, float] = {}
        for point in points:
            sign = -1.0 if point.is_export else 1.0
            for moment, kwh in (
                await self.async_consumption(point, start=start, end=end)
            ).items():
                readings[moment] = readings.get(moment, 0.0) + sign * kwh

        events, results = await self.async_saving_sessions(account)

        return FlexibilityData(
            account_number=account,
            meter_points=points,
            readings=readings,
            events=events,
            results=results,
            window_start=start,
            window_end=end,
            fetched_at=utcnow(),
        )

    async def async_consumption(
        self, meter_point: MeterPoint, *, start: date, end: date
    ) -> dict[datetime, float]:
        """Settled half-hourly kWh for one meter point, over local days.

        Boundaries come from :func:`period_starts`, never from ``T00:00:00Z``.
        In BST ``00:00Z`` is 01:00 local, which silently truncates the first two
        periods of the first day — the day is then stored incomplete and drops
        out of the pool, costing an eligible day invisibly. Observed in testing;
        see PLAN.md §3.2.
        """
        auth = base64.b64encode(f"{self._api_key}:".encode()).decode()
        first = period_starts(start)[0].strftime(_API_INSTANT)
        last = (period_starts(end)[-1] + timedelta(minutes=30)).strftime(_API_INSTANT)
        url = (
            f"{REST_ROOT}/electricity-meter-points/{meter_point.mpan}"
            f"/meters/{meter_point.serial}/consumption/"
            f"?period_from={first}&period_to={last}"
            f"&page_size={_PAGE_SIZE}&order_by=period"
        )

        readings: dict[datetime, float] = {}
        while url:
            body = await self._transport.request(
                "GET", url, headers={"Authorization": f"Basic {auth}"}
            )
            if not isinstance(body, Mapping) or "results" not in body:
                raise OctopusError(f"unexpected consumption response: {body!r}")
            readings.update(readings_from_intervals(body["results"]))
            url = body.get("next")
        return readings

    async def async_saving_sessions(
        self, account_number: str | None = None
    ) -> tuple[tuple[FlexibilityEvent, ...], tuple[SessionResult, ...]]:
        """Advertised events, and Octopus's settled figures for joined ones.

        ``events`` carries every advertised session, which is what the Event Day
        ruling needs — the ones the account did *not* join matter just as much
        (METHODOLOGY.md §6). ``joinedEvents`` supplies the validation figures.
        """
        account = account_number or await self.async_account_number()
        payload = await self._graphql(
            OEBE_URL,
            """
            query SavingSessions($account: String!) {
              savingSessions {
                events {
                  id code startAt endAt eventType status
                  rewardPerKwhInOctoPoints devEvent
                }
                account(accountNumber: $account) {
                  joinedEvents {
                    eventId startAt endAt eventType resultsStatus
                    baselineConsumptionDeltaKwh consumptionDeltaKwh
                    rewardGivenInOctoPoints rewardGivenInPence
                  }
                }
              }
            }
            """,
            {"account": account},
        )
        node = (payload or {}).get("savingSessions") or {}
        joined_ids = {
            row["eventId"]
            for row in ((node.get("account") or {}).get("joinedEvents") or [])
        }

        events = tuple(
            FlexibilityEvent(
                code=row.get("code") or f"event-{row['id']}",
                name=row.get("code") or f"event-{row['id']}",
                start=datetime.fromisoformat(row["startAt"]),
                end=datetime.fromisoformat(row["endAt"]),
                joined=row["id"] in joined_ids,
                event_type=_event_type(row.get("eventType")),
                event_id=row["id"],
                reward_per_kwh_points=row.get("rewardPerKwhInOctoPoints"),
            )
            # Dev events are Octopus's own test fixtures and never ran on a real
            # meter; treating one as an Event Day would cost a pool day for a
            # session that never happened.
            for row in (node.get("events") or [])
            if not row.get("devEvent")
        )

        results = tuple(
            SessionResult(
                event_id=row["eventId"],
                start=datetime.fromisoformat(row["startAt"]),
                end=datetime.fromisoformat(row["endAt"]),
                event_type=_event_type(row.get("eventType")),
                status=row.get("resultsStatus") or "UNKNOWN",
                baseline_kwh=row.get("baselineConsumptionDeltaKwh"),
                consumption_kwh=row.get("consumptionDeltaKwh"),
                reward_points=row.get("rewardGivenInOctoPoints"),
                reward_pence=row.get("rewardGivenInPence"),
            )
            for row in ((node.get("account") or {}).get("joinedEvents") or [])
        )
        return events, results

    # --------------------------------------------------------------- internals

    async def _graphql(
        self,
        url: str,
        query: str,
        variables: Mapping[str, Any],
        *,
        authenticated: bool = True,
    ) -> Mapping[str, Any] | None:
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = await self.async_token()
        body = await self._transport.request(
            "POST", url, headers=headers, json={"query": query, "variables": dict(variables)}
        )
        if not isinstance(body, Mapping):
            raise OctopusError(f"unexpected GraphQL response: {body!r}")
        if errors := body.get("errors"):
            message = "; ".join(
                str(e.get("message", e)) for e in errors if isinstance(e, Mapping)
            ) or str(errors)
            if _is_auth_error(message):
                raise OctopusAuthError(message)
            raise OctopusError(message)
        return body.get("data")


def _event_type(raw: str | None) -> EventType | str:
    try:
        return EventType(raw)
    except ValueError:
        # An unrecognised type is still an Event Day. Preserve it rather than
        # dropping the event, which would quietly inflate the baseline.
        return raw or EventType.TURN_DOWN


def _is_auth_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in ("auth", "token", "unauthor", "credential", "signature")
    )


def readings_from_intervals(
    intervals: Iterable[Mapping[str, Any]], *, sign: float = 1.0
) -> dict[datetime, float]:
    """Octopus REST ``results`` rows to the reading map.

    ``sign`` is ``-1.0`` for an export meter point, so that summing across meter
    points yields net consumption without the caller thinking about it.
    """
    return {
        datetime.fromisoformat(row["interval_start"]): sign * float(row["consumption"])
        for row in intervals
    }
