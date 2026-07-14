"""PropellerAds API Client."""

import os
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel



def _unwrap(result: Any) -> Any:
    """Unwrap PropellerAds API envelopes: {result: [...]}, {items: [...]}, or {data: ...}."""
    if isinstance(result, dict):
        for key in ("result", "items", "data"):
            if key in result:
                return result[key]
    return result


# Map friendly grouping names to PropellerAds v5 statistics enum tokens.
# API rejects anything outside VALID_GROUP_BY with "The selected choice is invalid."
GROUP_BY_MAP = {
    "campaign": "campaign_id",
    "zone": "zone_id",
    "creative": "banner_id",
    "banner": "banner_id",
    "product": "product_id",
    "country": "country_id",
    "geo": "country_id",
    "date": "date_time",
    "datetime": "date_time",
    "hour": "hour",
    "device": "device_id",
    "device_type": "device_id",
    "browser": "browser_id",
    "os": "os_id",
    "os_type": "os_type_id",
    "os_version": "os_version_id",
    "language": "language_id",
    "connection": "connection_id",
    "mobile_isp": "mobile_isp_id",
    "activity": "user_activity",
    "user_activity": "user_activity",
}
VALID_GROUP_BY = {
    "product_id", "campaign_id", "banner_id", "zone_id", "country_id",
    "date_time", "hour", "device_id", "browser_id", "mobile_isp_id",
    "os_version_id", "os_id", "os_type_id", "language_id", "connection_id",
    "user_activity", "zone_type", "is_broker", "request_var_id",
}


def _map_group_by(group_by: list[str]) -> list[str]:
    """Translate friendly grouping names to API enum tokens; pass valid tokens through."""
    return [GROUP_BY_MAP.get(g, g) for g in group_by]


class PropellerAdsError(Exception):
    """PropellerAds API error."""
    pass


class CampaignFilter(BaseModel):
    """Filter for listing campaigns."""
    status: str | None = None
    ad_format: str | None = None
    name: str | None = None


class StatisticsParams(BaseModel):
    """Parameters for statistics queries."""
    date_from: str | None = None
    date_to: str | None = None
    group_by: list[str] | None = None
    campaign_id: int | None = None
    zone_id: int | None = None


class PropellerAdsClient:
    """Client for PropellerAds SSP API v5."""

    BASE_URL = "https://ssp-api.propellerads.com/v5"

    def __init__(self, api_token: str | None = None):
        self.api_token = api_token or os.getenv("PROPELLERADS_API_TOKEN")
        if not self.api_token:
            raise PropellerAdsError(
                "API token required. Set PROPELLERADS_API_TOKEN environment variable "
                "or pass api_token parameter."
            )
        self.client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30.0,
            # Some endpoints (e.g. /rates) 301-redirect to a trailing-slash URL.
            follow_redirects=True,
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | list[Any] | None = None,
    ) -> dict[str, Any]:
        """Make API request."""
        try:
            response = self.client.request(
                method=method,
                url=endpoint,
                params=params,
                json=json_data,
            )
            response.raise_for_status()
            # DELETE / Success responses may be 204 or empty-bodied
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        except httpx.HTTPStatusError as e:
            error_detail = ""
            try:
                error_detail = e.response.json()
            except Exception:
                error_detail = e.response.text
            raise PropellerAdsError(
                f"API error {e.response.status_code}: {error_detail}"
            ) from e
        except httpx.RequestError as e:
            raise PropellerAdsError(f"Request failed: {str(e)}") from e

    # ========== Campaign Methods ==========

    def list_campaigns(
        self,
        status: str | None = None,
        ad_format: str | None = None,
        name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all campaigns with optional filters."""
        params = {}
        if status:
            params["status"] = status
        if ad_format:
            params["ad_format"] = ad_format
        if name:
            params["name"] = name

        result = self._request("GET", "/adv/campaigns", params=params or None)
        return _unwrap(result)

    def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        """Get campaign details by ID."""
        result = self._request("GET", f"/adv/campaigns/{campaign_id}")
        return _unwrap(result)

    def create_campaign(self, campaign_data: dict[str, Any]) -> dict[str, Any]:
        """Create a new campaign."""
        result = self._request("POST", "/adv/campaigns", json_data=campaign_data)
        return _unwrap(result)

    def update_campaign(
        self, campaign_id: int, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Update campaign settings. Spec: PATCH /adv/campaigns/{id} (not PUT). The
        endpoint only accepts name / frequency / capping / limit_daily_amount /
        limit_total_amount. Bids go through zone/campaign rates, status via play/stop,
        target URL via /url, targeting via the targeting endpoints."""
        result = self._request(
            "PATCH", f"/adv/campaigns/{campaign_id}", json_data=updates
        )
        return _unwrap(result)

    def start_campaigns(self, campaign_ids: list[int]) -> dict[str, Any]:
        """Start (activate) campaigns. Spec: PUT /adv/campaigns/play, body {campaign_ids}."""
        result = self._request(
            "PUT", "/adv/campaigns/play", json_data={"campaign_ids": campaign_ids}
        )
        return result

    def stop_campaigns(self, campaign_ids: list[int]) -> dict[str, Any]:
        """Stop (pause) campaigns. Spec: PUT /adv/campaigns/stop, body {campaign_ids}."""
        result = self._request(
            "PUT", "/adv/campaigns/stop", json_data={"campaign_ids": campaign_ids}
        )
        return result

    def clone_campaign(
        self, campaign_id: int, new_name: str | None = None
    ) -> dict[str, Any]:
        """Clone an existing campaign."""
        data = {}
        if new_name:
            data["name"] = new_name
        result = self._request(
            "POST", f"/adv/campaigns/{campaign_id}/clone", json_data=data or None
        )
        return _unwrap(result)

    # ========== Statistics Methods ==========

    def get_statistics(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        group_by: list[str] | None = None,
        campaign_id: int | None = None,
        zone_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get performance statistics."""
        # Default to last 7 days
        if not date_from:
            date_from = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        if not date_to:
            date_to = datetime.now().strftime("%Y-%m-%d")

        # API expects day_from/day_to as full datetimes; expand bare dates
        if len(date_from) == 10:
            date_from = f"{date_from} 00:00:00"
        if len(date_to) == 10:
            date_to = f"{date_to} 23:59:59"

        params: dict[str, Any] = {
            "day_from": date_from,
            "day_to": date_to,
        }

        # API requires at least one group_by; values must be enum tokens (see GROUP_BY_MAP)
        if not group_by:
            group_by = ["campaign_id"]
        group_by = _map_group_by(group_by)
        for i, gb in enumerate(group_by):
            params[f"group_by[{i}]"] = gb

        if campaign_id:
            params["campaign_id[]"] = campaign_id
        if zone_id:
            params["zone_id[]"] = zone_id

        result = self._request("GET", "/adv/statistics", params=params)
        return _unwrap(result)

    def get_campaign_statistics(
        self,
        campaign_id: int,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Get statistics for a specific campaign."""
        stats = self.get_statistics(
            date_from=date_from,
            date_to=date_to,
            campaign_id=campaign_id,
        )
        return stats[0] if stats else {}

    def get_zone_statistics(
        self,
        campaign_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get statistics grouped by zone."""
        stats = self.get_statistics(
            date_from=date_from,
            date_to=date_to,
            group_by=["zone_id"],
            campaign_id=campaign_id,
        )
        return stats[:limit] if isinstance(stats, list) else []

    def get_creative_statistics(
        self,
        campaign_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get statistics grouped by creative (PropellerAds calls creatives 'banners')."""
        return self.get_statistics(
            date_from=date_from,
            date_to=date_to,
            group_by=["banner_id"],
            campaign_id=campaign_id,
        )

    # ========== Creative Methods ==========

    def list_creatives(
        self, campaign_id: int | None = None
    ) -> list[dict[str, Any]]:
        """List creatives, optionally filtered by campaign."""
        params = {}
        if campaign_id:
            params["campaign_id"] = campaign_id

        result = self._request("GET", "/adv/creatives", params=params or None)
        return _unwrap(result)

    def get_creative(self, creative_id: int) -> dict[str, Any]:
        """Get creative details."""
        result = self._request("GET", f"/adv/creatives/{creative_id}")
        return _unwrap(result)

    def create_creative(self, creative_data: dict[str, Any]) -> dict[str, Any]:
        """Create a new creative."""
        result = self._request("POST", "/adv/creatives", json_data=creative_data)
        return _unwrap(result)

    def update_creative(
        self, creative_id: int, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Update creative."""
        result = self._request(
            "PUT", f"/adv/creatives/{creative_id}", json_data=updates
        )
        return _unwrap(result)

    # ========== Targeting Methods ==========

    def get_zones(self, campaign_id: int | None = None) -> list[dict[str, Any]]:
        """Get zones, optionally for a specific campaign."""
        params = {}
        if campaign_id:
            params["campaign_id"] = campaign_id

        result = self._request("GET", "/adv/zones", params=params or None)
        return _unwrap(result)

    # Zone targeting. Spec endpoints:
    #   whitelist (allowed)  = /adv/campaigns/{id}/targeting/include/zone
    #   blacklist (forbidden) = /adv/campaigns/{id}/targeting/exclude/zone
    #   GET reads current list, PATCH appends, PUT replaces the whole list.
    # Body schema is {"zone": ["123", "456"]} — zone IDs are STRINGS.
    def get_zone_targeting(self, campaign_id: int, kind: str) -> list[str]:
        """Get current zone list. kind = 'include' (whitelist) or 'exclude' (blacklist)."""
        data = _unwrap(
            self._request("GET", f"/adv/campaigns/{campaign_id}/targeting/{kind}/zone")
        )
        if isinstance(data, dict):
            return [str(z) for z in (data.get("zone") or [])]
        return [str(z) for z in (data or [])]

    def add_zones_to_whitelist(
        self, campaign_id: int, zone_ids: list[int]
    ) -> dict[str, Any]:
        """Append zones to campaign whitelist (PATCH = additive)."""
        return self._request(
            "PATCH",
            f"/adv/campaigns/{campaign_id}/targeting/include/zone",
            json_data={"zone": [str(z) for z in zone_ids]},
        )

    def add_zones_to_blacklist(
        self, campaign_id: int, zone_ids: list[int]
    ) -> dict[str, Any]:
        """Append zones to campaign blacklist (PATCH = additive)."""
        return self._request(
            "PATCH",
            f"/adv/campaigns/{campaign_id}/targeting/exclude/zone",
            json_data={"zone": [str(z) for z in zone_ids]},
        )

    def remove_zones_from_whitelist(
        self, campaign_id: int, zone_ids: list[int]
    ) -> dict[str, Any]:
        """Remove zones from whitelist (no DELETE endpoint: read, filter, PUT the remainder)."""
        remove = {str(z) for z in zone_ids}
        keep = [z for z in self.get_zone_targeting(campaign_id, "include") if z not in remove]
        return self._request(
            "PUT",
            f"/adv/campaigns/{campaign_id}/targeting/include/zone",
            json_data={"zone": keep},
        )

    def remove_zones_from_blacklist(
        self, campaign_id: int, zone_ids: list[int]
    ) -> dict[str, Any]:
        """Remove zones from blacklist (no DELETE endpoint: read, filter, PUT the remainder)."""
        remove = {str(z) for z in zone_ids}
        keep = [z for z in self.get_zone_targeting(campaign_id, "exclude") if z not in remove]
        return self._request(
            "PUT",
            f"/adv/campaigns/{campaign_id}/targeting/exclude/zone",
            json_data={"zone": keep},
        )

    # ========== Creative (banner) start/stop ==========

    def start_creatives(self, creative_ids: list[int]) -> dict[str, Any]:
        """Start (resume) one or more creatives. Spec: PUT /adv/creatives/start {creative_ids}."""
        return self._request(
            "PUT", "/adv/creatives/start", json_data={"creative_ids": creative_ids}
        )

    def stop_creatives(self, creative_ids: list[int]) -> dict[str, Any]:
        """Stop (pause) one or more creatives without stopping the campaign.
        Spec: PUT /adv/creatives/stop {creative_ids}."""
        return self._request(
            "PUT", "/adv/creatives/stop", json_data={"creative_ids": creative_ids}
        )

    # ========== Campaign rates (bids) ==========

    def get_campaign_rates(
        self, campaign_id: int, only_active: int = 1
    ) -> list[dict[str, Any]]:
        """Get campaign rate (bid) rows. only_active: 1 = active only, 0 = all."""
        result = self._request(
            "GET",
            f"/adv/campaigns/{campaign_id}/rates",
            params={"only_active": only_active},
        )
        return _unwrap(result)

    def set_campaign_rates(
        self, campaign_id: int, rates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Replace the campaign's rate list. WARNING: PUT closes ALL current rates and
        installs `rates` in their place. Each rate: {amount: <dollars>, countries: [..]}.
        Spec: PUT /adv/campaigns/{id}/rates {rates: [...]}."""
        return self._request(
            "PUT",
            f"/adv/campaigns/{campaign_id}/rates",
            json_data={"rates": rates},
        )

    # ========== Per-zone rates (autonomous per-placement bidding) ==========

    def get_zone_rates(self, campaign_id: int) -> list[dict[str, Any]]:
        """List per-zone rate overrides. Spec: GET /adv/campaigns/{id}/zone_rates."""
        return _unwrap(
            self._request("GET", f"/adv/campaigns/{campaign_id}/zone_rates")
        )

    def set_zone_rate(
        self, campaign_id: int, zone_id: int, amount: float
    ) -> dict[str, Any]:
        """Set/override the bid for a single zone (dollars). Spec:
        PUT /adv/campaigns/{id}/zone_rates/{zoneId} {amount}."""
        return self._request(
            "PUT",
            f"/adv/campaigns/{campaign_id}/zone_rates/{zone_id}",
            json_data={"amount": amount},
        )

    def delete_zone_rate(self, campaign_id: int, zone_id: int) -> dict[str, Any]:
        """Remove a single zone's rate override (reverts to campaign base bid).
        Spec: DELETE /adv/campaigns/{id}/zone_rates/{zoneId}."""
        return self._request(
            "DELETE", f"/adv/campaigns/{campaign_id}/zone_rates/{zone_id}"
        )

    def set_zone_rates_bulk(
        self, campaign_id: int, zone_rates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Replace ALL per-zone rates at once. Body is a bare array
        [{amount, zone_id}, ...]. Spec: PUT /adv/campaigns/{id}/zone_rates."""
        return self._request(
            "PUT", f"/adv/campaigns/{campaign_id}/zone_rates", json_data=zone_rates
        )

    # ========== Target URL ==========

    def update_campaign_url(self, campaign_id: int, url: str) -> dict[str, Any]:
        """Set a new target URL for all of a campaign's materials. NOTE: this sends the
        campaign back through moderation. Spec: PUT /adv/campaigns/{id}/url {url}."""
        return self._request(
            "PUT", f"/adv/campaigns/{campaign_id}/url", json_data={"url": url}
        )

    # ========== Zone groups ==========

    def get_zone_groups(self) -> list[dict[str, Any]]:
        """List zone groups. Spec: GET /adv/zone-groups/ (hyphenated, trailing slash)."""
        return _unwrap(self._request("GET", "/adv/zone-groups/"))

    # ========== Statistics (POST: server-side filters + sorting) ==========

    def query_statistics(
        self,
        day_from: str,
        day_to: str,
        group_by: list[str] | None = None,
        campaign_id: int | list[int] | None = None,
        zone_id: int | list[int] | None = None,
        banner_id: int | list[int] | None = None,
        geo: list[str] | None = None,
        order_by: str | None = None,
        order_dest: str = "desc",
        filters: dict[str, dict[str, int]] | None = None,
    ) -> list[dict[str, Any]]:
        """POST /adv/statistics with server-side filtering + sorting.

        filters: {metric: {"from": n, "to": m}} for impressions/clicks/conversions/
        conversions2/spent/ctr/cr. order_by: any response field; order_dest asc|desc.
        Richer than the GET wrapper (get_statistics), which only groups.
        """
        if len(day_from) == 10:
            day_from = f"{day_from} 00:00:00"
        if len(day_to) == 10:
            day_to = f"{day_to} 23:59:59"
        body: dict[str, Any] = {
            "group_by": _map_group_by(group_by or ["campaign_id"]),
            "day_from": day_from,
            "day_to": day_to,
        }

        def _as_list(v: Any) -> list[Any]:
            return v if isinstance(v, list) else [v]

        if campaign_id is not None:
            body["campaign_id"] = _as_list(campaign_id)
        if zone_id is not None:
            body["zone_id"] = _as_list(zone_id)
        if banner_id is not None:
            body["banner_id"] = _as_list(banner_id)
        if geo:
            body["geo"] = geo
        if order_by:
            body["order_by"] = order_by
            body["order_dest"] = order_dest
        if filters:
            for metric, rng in filters.items():
                body[metric] = rng

        return _unwrap(self._request("POST", "/adv/statistics", json_data=body))

    # ========== Account Methods ==========

    def get_balance(self) -> dict[str, Any]:
        """Get account balance."""
        result = self._request("GET", "/adv/balance")
        return _unwrap(result)

    def get_countries(self) -> list[dict[str, Any]]:
        """Get available countries for targeting. Spec: GET /collections/countries
        -> {result: [{id, value (ISO alpha-2 lowercase), title}]}."""
        result = self._request("GET", "/collections/countries")
        return _unwrap(result)

    def get_ad_formats(self) -> list[dict[str, Any]]:
        """Ad formats create_campaign supports, with the direction + targeting recipe each
        maps to (verified against the v5 Swagger create examples). No live endpoint exists;
        this documents how `format` is resolved. Classic Push and In-Page Push are both
        direction=nativeads and differ ONLY by the zone_type block."""
        return [
            {"format": "onclick", "title": "Onclick (Popunder)", "direction": "onclick",
             "rate_models": ["cpm", "scpm", "cpc", "scpc", "scpa"]},
            {"format": "classic_push", "title": "Classic Push", "direction": "nativeads",
             "zone_type": {"list": [42], "is_excluded": True},
             "rate_models": ["cpag", "cpc", "scpc"]},
            {"format": "ipp", "title": "In-Page Push", "direction": "nativeads",
             "zone_type": {"list": [42], "is_excluded": False},
             "rate_models": ["cpag", "cpc", "scpc"]},
            {"format": "interactive", "title": "Interactive Ads (Survey)", "direction": "onclick",
             "traffic_categories": ["all_survey"], "rate_models": ["scpa", "cpc", "scpc"]},
            {"format": "telegram", "title": "Telegram Ads", "direction": "telegram_ads",
             "uvc": ["high_intent", "wide_reach"], "rate_models": ["cpag", "scpc", "cpc"]},
        ]

    def get_collection(self, collection_type: str) -> list[dict[str, Any]]:
        """Get a targeting collection (os, device, browser, zone, language, ...).
        Spec: GET /collections/targeting/{type}. Valid types: region, city, time_table,
        os_version, os_type, os, device_type, device, browser, zone, connection,
        mobile_isp, proxy, language, audience, traffic_categories, uvc."""
        result = self._request("GET", f"/collections/targeting/{collection_type}")
        return _unwrap(result)

    def list_collection_types(self) -> list[dict[str, Any]]:
        """List available top-level collection types. Spec: GET /collections."""
        return _unwrap(self._request("GET", "/collections"))

    def get_collection_by_type(self, collection_type: str) -> list[dict[str, Any]]:
        """Get a non-targeting collection by type. Spec: GET /collections/{type}."""
        return _unwrap(self._request("GET", f"/collections/{collection_type}"))

    # ========== Zone groups (single) ==========

    def get_zone_group(self, group_id: int) -> dict[str, Any]:
        """Get one zone group's detail. Spec: GET /adv/zone-groups/{id}."""
        return _unwrap(self._request("GET", f"/adv/zone-groups/{group_id}"))

    # ========== Bulk rates for a list of campaigns ==========

    def get_campaigns_rates(
        self, campaign_ids: list[int], only_active: int = 1
    ) -> list[dict[str, Any]]:
        """Rate rows for a LIST of campaigns. Spec: GET /adv/campaigns/rates/ with the
        required campaign_ids[] query array (the endpoint 400s if it is blank)."""
        params: dict[str, Any] = {"only_active": only_active}
        for i, cid in enumerate(campaign_ids):
            params[f"campaign_ids[{i}]"] = cid
        return _unwrap(self._request("GET", "/adv/campaigns/rates/", params=params))

    # ========== Creatives under a campaign ==========

    def list_campaign_creatives(self, campaign_id: int) -> list[dict[str, Any]]:
        """A campaign's creatives. There is no GET creatives endpoint (the path is
        POST-only), so read them from the campaign object's `creatives` array."""
        campaign = self.get_campaign(campaign_id)
        if isinstance(campaign, dict):
            return campaign.get("creatives") or []
        return []

    def create_campaign_creative(
        self, campaign_id: int, creative: dict[str, Any]
    ) -> dict[str, Any]:
        """Add a creative to a campaign. Spec: POST /adv/campaigns/{id}/creatives with the
        body wrapped as {"creatives": [ ... ]}. creative fields: title, description, icon
        (base64), image (base64, push/interstitial), skin (IPP: auto/default/social/
        light_theme), buttons, default_button_disabled, status, is_auto + language_mode."""
        return _unwrap(
            self._request(
                "POST",
                f"/adv/campaigns/{campaign_id}/creatives",
                json_data={"creatives": [creative]},
            )
        )

    # ========== Whole-model campaign targeting (existing campaign) ==========

    # /targeting/{mode} where mode = "include" (allowed) or "exclude" (forbidden).
    # GET returns a {dimension: [values]} map; PUT REPLACES the whole mode with a
    # TargetingPutModel: {"targeting": [{"targeting": <dim>, "values": [...]}, ...]}.
    def get_campaign_targeting(self, campaign_id: int, mode: str) -> Any:
        """Read the whole include/exclude targeting map. Spec:
        GET /adv/campaigns/{id}/targeting/{include|exclude}/."""
        return _unwrap(
            self._request("GET", f"/adv/campaigns/{campaign_id}/targeting/{mode}/")
        )

    def set_campaign_targeting(
        self, campaign_id: int, mode: str, targeting_map: dict[str, list[Any]]
    ) -> dict[str, Any]:
        """Replace the whole include/exclude targeting model. WARNING: this overwrites
        every dimension in that mode. targeting_map is {dimension: [values]}; it is
        converted to the API's TargetingPutModel. Spec: PUT
        /adv/campaigns/{id}/targeting/{include|exclude}/."""
        body = {
            "targeting": [
                {"targeting": dim, "values": vals}
                for dim, vals in targeting_map.items()
            ]
        }
        return self._request(
            "PUT", f"/adv/campaigns/{campaign_id}/targeting/{mode}/", json_data=body
        )

    # ========== Sub-zone targeting ==========

    def get_sub_zone_targeting(self, campaign_id: int, kind: str) -> list[str]:
        """Read sub-zone list. kind = 'include' | 'exclude'.
        Spec: GET /adv/campaigns/{id}/targeting/{kind}/sub_zone."""
        data = _unwrap(
            self._request(
                "GET", f"/adv/campaigns/{campaign_id}/targeting/{kind}/sub_zone"
            )
        )
        if isinstance(data, dict):
            return [str(z) for z in (data.get("sub_zone") or [])]
        return [str(z) for z in (data or [])]

    def add_sub_zones(
        self, campaign_id: int, kind: str, sub_zone_ids: list[int]
    ) -> dict[str, Any]:
        """Append sub-zones to include/exclude (PATCH = additive).
        Spec: PATCH /adv/campaigns/{id}/targeting/{kind}/sub_zone."""
        return self._request(
            "PATCH",
            f"/adv/campaigns/{campaign_id}/targeting/{kind}/sub_zone",
            json_data={"sub_zone": [str(z) for z in sub_zone_ids]},
        )

    def set_sub_zone_other(
        self, campaign_id: int, mode: str, zone_ids: list[int] | None = None
    ) -> dict[str, Any]:
        """Exclude 'Other' subzones. mode = 'ALL' (exclude all globally) or
        'TARGET_ZONES' (per-zone). Spec: PUT /adv/campaigns/{id}/targeting/sub_zone_other."""
        payload: dict[str, Any] = {"type": mode, "is_excluded": True}
        if mode == "TARGET_ZONES":
            payload["list"] = [int(z) for z in (zone_ids or [])]
        return self._request(
            "PUT",
            f"/adv/campaigns/{campaign_id}/targeting/sub_zone_other",
            json_data=payload,
        )

    def close(self):
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
