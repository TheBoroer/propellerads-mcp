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
        """Start (activate) campaigns."""
        result = self._request(
            "POST", "/adv/campaigns/start", json_data={"ids": campaign_ids}
        )
        return result

    def stop_campaigns(self, campaign_ids: list[int]) -> dict[str, Any]:
        """Stop (pause) campaigns."""
        result = self._request(
            "POST", "/adv/campaigns/stop", json_data={"ids": campaign_ids}
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

        # API requires at least one group_by
        if not group_by:
            group_by = ["campaign_id"]
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
        """Get statistics grouped by creative."""
        return self.get_statistics(
            date_from=date_from,
            date_to=date_to,
            group_by=["creative_id"],
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

    def add_zones_to_whitelist(
        self, campaign_id: int, zone_ids: list[int]
    ) -> dict[str, Any]:
        """Add zones to campaign whitelist."""
        return self._request(
            "POST",
            f"/adv/campaigns/{campaign_id}/targeting/zones/whitelist",
            json_data={"zone_ids": zone_ids},
        )

    def add_zones_to_blacklist(
        self, campaign_id: int, zone_ids: list[int]
    ) -> dict[str, Any]:
        """Add zones to campaign blacklist."""
        return self._request(
            "POST",
            f"/adv/campaigns/{campaign_id}/targeting/zones/blacklist",
            json_data={"zone_ids": zone_ids},
        )

    def remove_zones_from_whitelist(
        self, campaign_id: int, zone_ids: list[int]
    ) -> dict[str, Any]:
        """Remove zones from campaign whitelist."""
        return self._request(
            "DELETE",
            f"/adv/campaigns/{campaign_id}/targeting/zones/whitelist",
            json_data={"zone_ids": zone_ids},
        )

    def remove_zones_from_blacklist(
        self, campaign_id: int, zone_ids: list[int]
    ) -> dict[str, Any]:
        """Remove zones from campaign blacklist."""
        return self._request(
            "DELETE",
            f"/adv/campaigns/{campaign_id}/targeting/zones/blacklist",
            json_data={"zone_ids": zone_ids},
        )

    # ========== Account Methods ==========

    def get_balance(self) -> dict[str, Any]:
        """Get account balance."""
        result = self._request("GET", "/adv/balance")
        return _unwrap(result)

    def get_countries(self) -> list[dict[str, Any]]:
        """Get available countries for targeting."""
        result = self._request("GET", "/adv/countries")
        return _unwrap(result)

    def get_ad_formats(self) -> list[dict[str, Any]]:
        """Get available ad formats."""
        result = self._request("GET", "/adv/ad-formats")
        return _unwrap(result)

    def close(self):
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
