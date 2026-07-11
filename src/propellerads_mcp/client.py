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
        json_data: dict[str, Any] | None = None,
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
        """Update campaign settings."""
        result = self._request(
            "PUT", f"/adv/campaigns/{campaign_id}", json_data=updates
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
        """Available ad formats. Static enum from spec Format.yaml (no live endpoint;
        the old /adv/ad-formats path 404s)."""
        return [
            {"value": "onclick", "title": "Onclick (Popunder)"},
            {"value": "ipp", "title": "In-Page Push"},
            {"value": "classic_push", "title": "Classic Push"},
            {"value": "survey", "title": "Interactive Ads"},
            {"value": "telegram", "title": "Telegram Ads"},
        ]

    def get_collection(self, collection_type: str) -> list[dict[str, Any]]:
        """Get a targeting collection (os, device, browser, zone, language, ...).
        Spec: GET /collections/targeting/{type} (or GET /collections for the type list)."""
        result = self._request("GET", f"/collections/targeting/{collection_type}")
        return _unwrap(result)

    def close(self):
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
