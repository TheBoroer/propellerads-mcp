"""PropellerAds MCP Server - Main server implementation."""

import json
import os
from datetime import datetime, timedelta
from typing import Any

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .client import PropellerAdsClient, PropellerAdsError

# Load environment variables
load_dotenv()

# Initialize server
server = Server("propellerads-mcp")

# Lazy client initialization
_client: PropellerAdsClient | None = None


def get_client() -> PropellerAdsClient:
    """Get or create PropellerAds client."""
    global _client
    if _client is None:
        _client = PropellerAdsClient()
    return _client


def format_currency(value: float | None) -> str:
    """Format currency value."""
    if value is None:
        return "$0.00"
    return f"${value:,.2f}"


def format_percentage(value: float | None) -> str:
    """Format percentage value."""
    if value is None:
        return "0.00%"
    return f"{value:.2f}%"


# PropellerAds campaign status enum (components/schemas/CampaignStatus.yaml)
CAMPAIGN_STATUS = {1: "draft", 2: "moderation", 3: "rejected", 6: "working", 7: "paused", 8: "stopped"}
CAMPAIGN_STATUS_ICON = {1: "📝", 2: "🟡", 3: "⛔", 6: "🟢", 7: "⏸️", 8: "🔴"}


def _num(value: Any) -> float:
    """Coerce API numeric fields (which may arrive as strings) to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _status_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def status_label(value: Any) -> str:
    """Render a campaign status int as icon + label."""
    s = _status_int(value)
    if s is None:
        return str(value)
    return f"{CAMPAIGN_STATUS_ICON.get(s, '❓')} {CAMPAIGN_STATUS.get(s, s)}"


def calculate_metrics(stats: dict[str, Any]) -> dict[str, Any]:
    """Calculate additional metrics from raw statistics."""
    impressions = int(_num(stats.get("impressions", 0)))
    clicks = int(_num(stats.get("clicks", 0)))
    conversions = int(_num(stats.get("conversions", 0)))
    # Goal 2 = trial-to-paid (PropellerAds StatResponseRow.conversions2)
    conversions2 = int(_num(stats.get("conversions2", 0)))
    # Spend field is 'spent' in v5 (NOT 'spend'/'cost'); may arrive as a string
    spend = _num(stats.get("spent", stats.get("money", stats.get("spend", stats.get("cost", 0)))))
    revenue = _num(stats.get("payout", stats.get("revenue", 0)))

    ctr = (clicks / impressions * 100) if impressions > 0 else 0
    cvr = (conversions / clicks * 100) if clicks > 0 else 0
    cpc = spend / clicks if clicks > 0 else 0
    cpa = spend / conversions if conversions > 0 else 0
    roi = ((revenue - spend) / spend * 100) if spend > 0 else 0

    return {
        **stats,
        "impressions": impressions,
        "clicks": clicks,
        "conversions": conversions,
        "conversions2": conversions2,
        "spend": round(spend, 4),
        "revenue": round(revenue, 2),
        "ctr": round(ctr, 2),
        "cvr": round(cvr, 2),
        "cpc": round(cpc, 4),
        "cpa": round(cpa, 2),
        "roi": round(roi, 2),
    }


# Friendly ad-format -> (direction, format-specific targeting). Verified against the
# v5 Swagger create examples. Classic Push and In-Page Push are BOTH direction=nativeads
# and differ ONLY by the zone_type targeting block:
#   Classic Push -> zone_type {list:[42], is_excluded: true}   (exclude in-page zones)
#   In-Page Push -> zone_type {list:[42], is_excluded: false}  (include only in-page zones)
#   Interactive  -> direction=onclick + traffic_categories:[all_survey]
#   Telegram     -> direction=telegram_ads
FORMAT_RECIPES: dict[str, dict[str, Any]] = {
    "onclick": {"direction": "onclick"},
    "classic_push": {"direction": "nativeads", "zone_type": {"list": [42], "is_excluded": True}},
    "ipp": {"direction": "nativeads", "zone_type": {"list": [42], "is_excluded": False}},
    "interactive": {"direction": "onclick", "traffic_categories": ["all_survey"]},
    "telegram": {"direction": "telegram_ads"},
}

# Targeting dimensions that take a {list, is_excluded} block.
_LIST_TARGETING = (
    "os_type", "os", "os_version", "device_type", "device", "browser",
    "language", "mobile_isp", "user_activity", "zone_type",
)


def _list_block(value: Any, is_excluded: bool = False) -> dict[str, Any]:
    """Wrap a value (or list) in the API's {list, is_excluded} targeting shape."""
    items = value if isinstance(value, list) else [value]
    return {"list": items, "is_excluded": is_excluded}


def _build_campaign_payload(args: dict[str, Any]) -> dict[str, Any]:
    """Build the POST /adv/campaigns body from friendly args.

    Resolves `format` into direction + format-specific targeting, merges friendly
    targeting scalars, then applies a raw `targeting` override on top. Returns the
    exact dict that will be POSTed (also surfaced by dry_run)."""
    fmt = args.get("format")
    recipe = FORMAT_RECIPES.get(fmt, {}) if fmt else {}
    direction = args.get("direction") or recipe.get("direction")
    if not direction:
        raise PropellerAdsError(
            "create_campaign needs either `format` (onclick/classic_push/ipp/"
            "interactive/telegram) or an explicit `direction`."
        )

    countries = [c.lower() for c in args.get("countries", [])]
    targeting: dict[str, Any] = {}
    if countries:
        targeting["country"] = {"list": countries, "is_excluded": False}

    # Format-derived targeting (zone_type / traffic_categories).
    if "zone_type" in recipe:
        targeting["zone_type"] = dict(recipe["zone_type"])
    if "traffic_categories" in recipe:
        targeting["traffic_categories"] = list(recipe["traffic_categories"])

    # Friendly list dimensions.
    for dim in _LIST_TARGETING:
        if args.get(dim) is not None:
            targeting[dim] = _list_block(args[dim])

    # Scalar / special-shaped dimensions.
    if args.get("connection"):
        targeting["connection"] = args["connection"]
    if args.get("time_table"):
        targeting["time_table"] = _list_block(args["time_table"])
    if args.get("traffic_categories"):
        tc = args["traffic_categories"]
        targeting["traffic_categories"] = tc if isinstance(tc, list) else [tc]
    if args.get("uvc"):
        targeting["uvc"] = _list_block(args["uvc"])

    # Raw targeting override wins (top-level merge).
    if isinstance(args.get("targeting"), dict):
        targeting.update(args["targeting"])

    campaign_data: dict[str, Any] = {
        "name": args.get("name", "API campaign"),
        "direction": direction,
        "rate_model": args["rate_model"],
        "target_url": args["target_url"],
        "status": args.get("status", 1),
        "started_at": args["started_at"],
        "timezone": args.get("timezone", -5),
        "targeting": targeting,
    }

    # rates: explicit rates[] wins, else build one row from bid + countries.
    if isinstance(args.get("rates"), list) and args["rates"]:
        campaign_data["rates"] = args["rates"]
    elif args.get("bid") is not None:
        campaign_data["rates"] = [{"amount": args["bid"], "countries": countries}]

    for k in ("daily_amount", "total_amount", "frequency", "capping",
              "expired_at", "allow_zone_update", "cpa_goal_slice_budget"):
        if args.get(k) is not None:
            campaign_data[k] = args[k]

    if args.get("cpa_goal_bid") is not None:
        campaign_data["cpa_goal_bid"] = args["cpa_goal_bid"]
        campaign_data["cpa_goal_status"] = True

    if isinstance(args.get("audience"), dict):
        campaign_data["audience"] = args["audience"]

    if isinstance(args.get("creatives"), list) and args["creatives"]:
        campaign_data["creatives"] = args["creatives"]

    return campaign_data


# ========== Tool Definitions ==========

TOOLS = [
    # Campaign Management
    Tool(
        name="list_campaigns",
        description="List all campaigns with optional filters. Returns campaign ID, name, status, rate model, and daily cap. NOTE: status 'paused' (7) is ambiguous - the API cannot say whether a campaign is auto-pacing (late-click / daily-impressions throttle, which auto-resumes), budget-capped, or manually paused; do not read paused as stopped. Cross-check with query_statistics daily spend.",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status: active, paused, pending, rejected",
                    "enum": ["active", "paused", "pending", "rejected"],
                },
                "ad_format": {
                    "type": "string",
                    "description": "Filter by ad format: push, onclick, interstitial, in-page-push",
                },
                "name": {
                    "type": "string",
                    "description": "Filter by campaign name (partial match)",
                },
            },
        },
    ),
    Tool(
        name="get_campaign_details",
        description="Get complete details for a specific campaign including targeting, creatives, and settings.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {
                    "type": "integer",
                    "description": "Campaign ID",
                },
            },
            "required": ["campaign_id"],
        },
    ),
    Tool(
        name="create_campaign",
        description=(
            "Create a campaign (POST /adv/campaigns), draft by default (status=1); start later "
            "with start_campaigns. Pick the ad format with `format` (preferred) — it sets the "
            "right direction + zone_type + traffic_categories automatically:\n"
            "  onclick = Popunder; classic_push = Classic Push; ipp = In-Page Push; "
            "interactive = Interactive/Survey ads; telegram = Telegram Ads.\n"
            "In-Page vs Classic Push are both direction=nativeads and differ ONLY by zone_type "
            "(handled for you). Rate models: cpm/scpm/cpc/scpc fixed bid; cpag = CPA Goal for Push "
            "(by clicks); scpa = CPA Goal for Onclick (by impressions). CPA-goal/CPA models require "
            "${SUBID} in target_url. Rich targeting (os, device, browser, connection, language, etc.) "
            "is optional; discover valid tokens with get_targeting_options. Pass dry_run=true to get "
            "the exact payload back WITHOUT creating anything. Campaigns cannot be deleted, only archived."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Campaign name"},
                "format": {
                    "type": "string",
                    "enum": ["onclick", "classic_push", "ipp", "interactive", "telegram"],
                    "description": "Ad format. Sets direction + zone_type/traffic_categories. Preferred over raw direction.",
                },
                "direction": {
                    "type": "string",
                    "enum": ["onclick", "nativeads", "telegram_ads"],
                    "description": "Raw direction (advanced). Usually leave unset and use `format`.",
                },
                "rate_model": {
                    "type": "string",
                    "enum": ["cpm", "scpm", "cpc", "scpc", "scpa", "cpag"],
                    "description": "cpag = CPA Goal for Push; scpa = CPA Goal for Onclick; cpc/scpc/cpm/scpm = fixed/smart bid",
                },
                "target_url": {
                    "type": "string",
                    "description": "Landing URL. Must include ${SUBID} for cpag/scpa/cpa models",
                },
                "countries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ISO alpha-2 lowercase country codes (e.g. ['us','gb','de'])",
                },
                "bid": {"type": "number", "description": "Base bid in dollars (builds one rate row for all countries). Ignored if `rates` given."},
                "rates": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Advanced: explicit per-country rate rows [{amount, countries:[...]}]. Overrides `bid`.",
                },
                "started_at": {"type": "string", "description": "Start date in dd/MM/YYYY format"},
                "expired_at": {"type": "string", "description": "End date in dd/MM/YYYY format (optional)"},
                "daily_amount": {"type": "number", "description": "Daily budget cap in USD (Push CPC min $10)"},
                "total_amount": {"type": "number", "description": "Total budget cap in USD (must exceed daily)"},
                "cpa_goal_bid": {"type": "number", "description": "Target CPA in dollars (sets cpa_goal_status=true)"},
                "cpa_goal_slice_budget": {"type": "number", "description": "CPA Goal slice budget (advanced)"},
                "user_activity": {
                    "type": "array",
                    "items": {"type": "integer", "enum": [1, 2, 3]},
                    "description": "1=High, 2=Medium, 3=Low (protocol: High+Medium only)",
                },
                "os_type": {"type": "array", "items": {"type": "string"}, "description": "OS type tokens (e.g. mobile, desktop). See get_targeting_options('os_type')."},
                "os": {"type": "array", "items": {"type": "string"}, "description": "OS tokens (e.g. ios, android, windows). See get_targeting_options('os')."},
                "os_version": {"type": "array", "items": {"type": "string"}, "description": "OS version tokens (e.g. ios13). See get_targeting_options('os_version')."},
                "device_type": {"type": "array", "items": {"type": "string"}, "description": "Device type tokens. See get_targeting_options('device_type')."},
                "device": {"type": "array", "items": {"type": "string"}, "description": "Device tokens. See get_targeting_options('device')."},
                "browser": {"type": "array", "items": {"type": "string"}, "description": "Browser tokens. See get_targeting_options('browser')."},
                "language": {"type": "array", "items": {"type": "string"}, "description": "Language tokens. See get_targeting_options('language')."},
                "mobile_isp": {"type": "array", "items": {"type": "string"}, "description": "Mobile ISP tokens. See get_targeting_options('mobile_isp')."},
                "connection": {"type": "string", "enum": ["mobile", "other"], "description": "Connection type (bare string, not a list)."},
                "traffic_categories": {"type": "array", "items": {"type": "string"}, "description": "propeller/broker/premium/social_traffic/all_survey. Auto-set for interactive format."},
                "uvc": {"type": "array", "items": {"type": "string"}, "description": "Telegram only: high_intent / wide_reach."},
                "time_table": {"type": "array", "items": {"type": "string"}, "description": "Schedule slots like 'Mon00','Tue03' (day+hour). Omit for always-on."},
                "audience": {"type": "object", "description": "Audience block {topics:[1,2,3], audience_id}."},
                "targeting": {"type": "object", "description": "Advanced: raw targeting dict merged LAST over everything above (full escape hatch)."},
                "creatives": {"type": "array", "items": {"type": "object"}, "description": "Optional creatives to attach at create time (raw CampaignCreative dicts: title, description, icon, image, skin, buttons, is_auto...)."},
                "allow_zone_update": {"type": "boolean", "description": "Auto-add renamed zone IDs to whitelist to avoid perf drop."},
                "frequency": {"type": "integer", "description": "Impressions per user per capping window (0 = unlimited)"},
                "capping": {"type": "integer", "description": "Frequency-cap window in SECONDS (e.g. 86400 = 24h)"},
                "timezone": {"type": "integer", "description": "UTC offset, default -5"},
                "status": {"type": "integer", "enum": [1, 2], "description": "1=draft (default), 2=submit to moderation"},
                "dry_run": {"type": "boolean", "description": "If true, return the built POST payload without creating the campaign."},
            },
            "required": ["rate_model", "target_url", "started_at"],
        },
    ),
    Tool(
        name="update_campaign",
        description=(
            "Update a campaign (PATCH /adv/campaigns/{id}). Only name, frequency, capping, and "
            "budget caps are editable here. For bids use set_campaign_rate / set_zone_rate; for "
            "status use start_campaigns / stop_campaigns; for the landing URL use update_campaign_url."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "name": {"type": "string", "description": "New campaign name"},
                "frequency": {"type": "integer", "description": "Impressions per user per capping window"},
                "capping": {"type": "integer", "description": "Frequency-cap window in hours (e.g. 24)"},
                "limit_daily_amount": {"type": "number", "description": "New daily budget cap in USD"},
                "limit_total_amount": {"type": "number", "description": "New total budget cap in USD"},
            },
            "required": ["campaign_id"],
        },
    ),
    Tool(
        name="start_campaigns",
        description="Activate/start one or more campaigns.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of campaign IDs to start",
                },
            },
            "required": ["campaign_ids"],
        },
    ),
    Tool(
        name="stop_campaigns",
        description="Pause/stop one or more campaigns.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of campaign IDs to stop",
                },
            },
            "required": ["campaign_ids"],
        },
    ),
    Tool(
        name="clone_campaign",
        description=(
            "Create a copy of an existing campaign via POST /adv/campaigns/{id}/clone. "
            "NOTE: this /clone path is NOT in the v5 Swagger spec and may 404 on some accounts; "
            "if it fails, get_campaign_details then rebuild with create_campaign instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {
                    "type": "integer",
                    "description": "ID of campaign to clone",
                },
                "new_name": {
                    "type": "string",
                    "description": "Name for the cloned campaign",
                },
            },
            "required": ["campaign_id"],
        },
    ),
    # Creative (banner) control
    Tool(
        name="start_creatives",
        description="Start/resume one or more creatives (banners) by ID, without touching the campaign.",
        inputSchema={
            "type": "object",
            "properties": {
                "creative_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Creative (banner) IDs to start",
                },
            },
            "required": ["creative_ids"],
        },
    ),
    Tool(
        name="stop_creatives",
        description="Stop/pause one or more creatives (banners) by ID while leaving the campaign running. Use to kill a losing creative (30+ clicks, 0 trials) without stopping the whole campaign.",
        inputSchema={
            "type": "object",
            "properties": {
                "creative_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Creative (banner) IDs to stop",
                },
            },
            "required": ["creative_ids"],
        },
    ),
    Tool(
        name="get_campaign_rates",
        description="Get a campaign's current rate (bid) rows: amount in dollars per country, with active window. Read-only.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "only_active": {
                    "type": "integer",
                    "enum": [0, 1],
                    "description": "1 = active rates only (default), 0 = include finished",
                },
            },
            "required": ["campaign_id"],
        },
    ),
    Tool(
        name="set_campaign_rate",
        description="Set a campaign's bid. WARNING: this REPLACES all existing rate rows with a single rate (PUT semantics) - existing per-country bids are closed. For CPA Goal campaigns the base bid is the rate; the CPA-goal target is a separate campaign field (cpa_goal_bid via update_campaign). Confirm intent before using on a live campaign.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "amount": {"type": "number", "description": "Bid amount in dollars (CPC/CPM)"},
                "countries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional ISO alpha-2 lowercase country codes the rate applies to; omit for all",
                },
            },
            "required": ["campaign_id", "amount"],
        },
    ),
    Tool(
        name="update_campaign_url",
        description="Set a new landing/target URL for all of a campaign's materials. WARNING: sends the campaign back through moderation. Keep the ${SUBID} macro for tracking.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "url": {"type": "string", "description": "New target URL (include ${SUBID} for tracking)"},
            },
            "required": ["campaign_id", "url"],
        },
    ),
    Tool(
        name="get_zone_rates",
        description="List per-zone bid overrides for a campaign (autonomous per-placement bids). Read-only.",
        inputSchema={
            "type": "object",
            "properties": {"campaign_id": {"type": "integer", "description": "Campaign ID"}},
            "required": ["campaign_id"],
        },
    ),
    Tool(
        name="set_zone_rate",
        description="Set a custom bid for ONE zone in a campaign (dollars). Bid a converting zone up or a marginal zone down instead of hard black/whitelisting it. ONLY supported on fixed-bid (CPC/CPM) campaigns; CPA Goal (cpag/scpa) campaigns bid automatically and reject this with 'Unsupported campaign' - use blacklist/whitelist there instead.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "zone_id": {"type": "integer", "description": "Zone ID"},
                "amount": {"type": "number", "description": "Bid for this zone in dollars"},
            },
            "required": ["campaign_id", "zone_id", "amount"],
        },
    ),
    Tool(
        name="delete_zone_rate",
        description="Remove a zone's custom bid override; the zone reverts to the campaign base bid.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "zone_id": {"type": "integer", "description": "Zone ID"},
            },
            "required": ["campaign_id", "zone_id"],
        },
    ),
    Tool(
        name="get_zone_groups",
        description="List available zone groups (reusable zone bundles for targeting).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="query_statistics",
        description="Advanced statistics via POST: server-side metric-threshold filtering and sorting on top of grouping. Use to pull, e.g., zones with spend>=$10 and 0 conversions, sorted by spend, in one call.",
        inputSchema={
            "type": "object",
            "properties": {
                "day_from": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "day_to": {"type": "string", "description": "End date YYYY-MM-DD"},
                "group_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Grouping: campaign, zone, creative, country, date, os, device, browser",
                },
                "campaign_id": {"type": "integer", "description": "Filter to a campaign"},
                "order_by": {"type": "string", "description": "Field to sort by (e.g. spent, clicks, conversions, ctr)"},
                "order_dest": {"type": "string", "enum": ["asc", "desc"], "description": "Sort direction (default desc)"},
                "min_impressions": {"type": "integer"},
                "min_clicks": {"type": "integer"},
                "min_conversions": {"type": "integer"},
                "max_conversions": {"type": "integer"},
                "min_spend": {"type": "number", "description": "Minimum spend (dollars)"},
                "max_spend": {"type": "number", "description": "Maximum spend (dollars)"},
            },
            "required": ["day_from", "day_to"],
        },
    ),
    # Statistics & Reports
    Tool(
        name="get_performance_report",
        description="Get detailed performance statistics with metrics like impressions, clicks, conversions, spend, CTR, CVR, CPC, CPA, and ROI.",
        inputSchema={
            "type": "object",
            "properties": {
                "date_from": {
                    "type": "string",
                    "description": "Start date (YYYY-MM-DD). Defaults to 7 days ago.",
                },
                "date_to": {
                    "type": "string",
                    "description": "End date (YYYY-MM-DD). Defaults to today.",
                },
                "group_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Group results by: date, campaign, zone, country, creative, device_type, browser, os",
                },
                "campaign_id": {
                    "type": "integer",
                    "description": "Filter by specific campaign ID",
                },
            },
        },
    ),
    Tool(
        name="get_campaign_performance",
        description="Get performance summary for a specific campaign with calculated metrics and insights.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "date_from": {"type": "string", "description": "Start date (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "End date (YYYY-MM-DD)"},
            },
            "required": ["campaign_id"],
        },
    ),
    Tool(
        name="compare_periods",
        description="Compare performance between two time periods.",
        inputSchema={
            "type": "object",
            "properties": {
                "period1_from": {
                    "type": "string",
                    "description": "Period 1 start date (YYYY-MM-DD)",
                },
                "period1_to": {
                    "type": "string",
                    "description": "Period 1 end date (YYYY-MM-DD)",
                },
                "period2_from": {
                    "type": "string",
                    "description": "Period 2 start date (YYYY-MM-DD)",
                },
                "period2_to": {
                    "type": "string",
                    "description": "Period 2 end date (YYYY-MM-DD)",
                },
                "campaign_id": {
                    "type": "integer",
                    "description": "Optional: Filter by campaign ID",
                },
            },
            "required": ["period1_from", "period1_to", "period2_from", "period2_to"],
        },
    ),
    Tool(
        name="get_zone_performance",
        description="Get performance statistics grouped by zone/placement. Useful for whitelist/blacklist optimization.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {
                    "type": "integer",
                    "description": "Filter by campaign ID",
                },
                "date_from": {"type": "string", "description": "Start date (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "End date (YYYY-MM-DD)"},
                "limit": {
                    "type": "integer",
                    "description": "Max number of zones to return (default: 100)",
                },
                "sort_by": {
                    "type": "string",
                    "description": "Sort by: spend, conversions, roi, ctr",
                    "enum": ["spend", "conversions", "roi", "ctr"],
                },
            },
        },
    ),
    Tool(
        name="get_creative_performance",
        description="Get performance statistics for creatives.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {
                    "type": "integer",
                    "description": "Filter by campaign ID",
                },
                "date_from": {"type": "string", "description": "Start date (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "End date (YYYY-MM-DD)"},
            },
        },
    ),
    # Optimization Tools
    Tool(
        name="find_underperforming_zones",
        description="Find zones that are spending money but not converting. Useful for blacklist candidates.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "min_spend": {
                    "type": "number",
                    "description": "Minimum spend threshold (default: $10)",
                },
                "max_conversions": {
                    "type": "integer",
                    "description": "Maximum conversions (default: 0)",
                },
                "date_from": {"type": "string", "description": "Start date"},
                "date_to": {"type": "string", "description": "End date"},
            },
            "required": ["campaign_id"],
        },
    ),
    Tool(
        name="find_top_zones",
        description="Find best performing zones. Useful for whitelist candidates.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "min_conversions": {
                    "type": "integer",
                    "description": "Minimum conversions (default: 1)",
                },
                "min_roi": {
                    "type": "number",
                    "description": "Minimum ROI percentage (default: 0)",
                },
                "limit": {"type": "integer", "description": "Max results (default: 20)"},
                "date_from": {"type": "string", "description": "Start date"},
                "date_to": {"type": "string", "description": "End date"},
            },
            "required": ["campaign_id"],
        },
    ),
    Tool(
        name="find_scaling_opportunities",
        description="Find campaigns ready for scaling based on ROI and conversion volume.",
        inputSchema={
            "type": "object",
            "properties": {
                "min_roi": {
                    "type": "number",
                    "description": "Minimum ROI percentage (default: 50)",
                },
                "min_conversions": {
                    "type": "integer",
                    "description": "Minimum conversions (default: 10)",
                },
                "date_from": {"type": "string", "description": "Start date"},
                "date_to": {"type": "string", "description": "End date"},
            },
        },
    ),
    # Zone Targeting
    Tool(
        name="add_to_blacklist",
        description="Add zones to campaign blacklist.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "zone_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Zone IDs to blacklist",
                },
            },
            "required": ["campaign_id", "zone_ids"],
        },
    ),
    Tool(
        name="add_to_whitelist",
        description="Add zones to campaign whitelist.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "zone_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Zone IDs to whitelist",
                },
            },
            "required": ["campaign_id", "zone_ids"],
        },
    ),
    Tool(
        name="auto_blacklist_zones",
        description="Automatically find and blacklist underperforming zones for a campaign.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "min_spend": {
                    "type": "number",
                    "description": "Minimum spend to consider (default: $10)",
                },
                "max_conversions": {
                    "type": "integer",
                    "description": "Maximum conversions (default: 0)",
                },
                "date_from": {"type": "string", "description": "Start date"},
                "date_to": {"type": "string", "description": "End date"},
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, show zones but don't blacklist (default: true)",
                },
            },
            "required": ["campaign_id"],
        },
    ),
    # Account
    Tool(
        name="get_balance",
        description="Get current account balance.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_available_countries",
        description="Get list of available countries for targeting.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_ad_formats",
        description="List the ad formats create_campaign supports, with the direction + targeting recipe each maps to.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ===== Targeting collections (discover valid tokens) =====
    Tool(
        name="get_targeting_options",
        description=(
            "List valid values for a targeting dimension so you can build campaign targeting. "
            "Spec: GET /collections/targeting/{type}. Use before setting os/device/browser/etc."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "region", "city", "time_table", "os_version", "os_type", "os",
                        "device_type", "device", "browser", "zone", "connection",
                        "mobile_isp", "proxy", "language", "audience",
                        "traffic_categories", "uvc",
                    ],
                    "description": "Targeting dimension to enumerate.",
                },
            },
            "required": ["type"],
        },
    ),
    Tool(
        name="list_collection_types",
        description="List available top-level collection types. Spec: GET /collections.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ===== Creatives (banners) =====
    Tool(
        name="list_creatives",
        description="List a campaign's creatives (banners). Spec: GET /adv/campaigns/{id}/creatives.",
        inputSchema={
            "type": "object",
            "properties": {"campaign_id": {"type": "integer", "description": "Campaign ID"}},
            "required": ["campaign_id"],
        },
    ),
    Tool(
        name="create_creative",
        description=(
            "Add a creative to a campaign. Spec: POST /adv/campaigns/{id}/creatives. Push/IPP need "
            "icon (base64 data URI); interstitial needs image. IPP uses `skin`; classic push uses "
            "`buttons`. Set is_auto=true with language_mode for an autocreative (push only)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "Campaign ID"},
                "title": {"type": "string", "description": "Creative title (push max 30 chars)"},
                "description": {"type": "string", "description": "Creative description (push max 40 chars)"},
                "icon": {"type": "string", "description": "Icon as base64 data URI (required for push/IPP)"},
                "image": {"type": "string", "description": "Image as base64 data URI (required for interstitial)"},
                "skin": {"type": "string", "enum": ["auto", "default", "social", "light_theme"], "description": "In-Page Push skin"},
                "buttons": {"type": "array", "items": {"type": "object"}, "description": "Classic push buttons [{name}]"},
                "default_button_disabled": {"type": "boolean"},
                "status": {"type": "integer", "enum": [1, 2], "description": "1=active, 2=disabled"},
                "is_auto": {"type": "boolean", "description": "Autocreative (push only). Only send status/language_mode/language with this."},
                "language_mode": {"type": "string", "enum": ["by_geo", "by_browser", "custom"]},
                "language": {"type": "string", "description": "ISO 639-1, only with language_mode=custom"},
                "creative": {"type": "object", "description": "Advanced: raw CampaignCreative dict, merged over the fields above."},
            },
            "required": ["campaign_id"],
        },
    ),
    Tool(
        name="update_creative",
        description="Update a creative. Spec: PUT /adv/creatives/{id}. Pass the fields to change in `updates`.",
        inputSchema={
            "type": "object",
            "properties": {
                "creative_id": {"type": "integer", "description": "Creative (banner) ID"},
                "updates": {"type": "object", "description": "Fields to update"},
            },
            "required": ["creative_id", "updates"],
        },
    ),
    # ===== Campaign targeting management (existing campaign) =====
    Tool(
        name="get_campaign_targeting",
        description=(
            "Read a campaign's whole allowed (include) or forbidden (exclude) targeting map. "
            "Spec: GET /adv/campaigns/{id}/targeting/{include|exclude}. Returns {dimension: [values]}, "
            "e.g. exclude -> {zone:[...blacklist...], zone_type:[42,78,119], proxy:[true]}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "mode": {"type": "string", "enum": ["include", "exclude"], "description": "include = allowed, exclude = forbidden"},
            },
            "required": ["campaign_id", "mode"],
        },
    ),
    Tool(
        name="set_campaign_targeting",
        description=(
            "Replace a campaign's WHOLE include or exclude targeting model (PUT). `targeting` is a "
            "{dimension: [values]} map, e.g. {os_type:['mobile'], os:['android'], "
            "user_activity:[1,2]}. WARNING: this overwrites every dimension in that mode — read "
            "with get_campaign_targeting first and merge yourself. For just adding/removing zones "
            "use add_to_blacklist / add_to_whitelist instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "mode": {"type": "string", "enum": ["include", "exclude"]},
                "targeting": {"type": "object", "description": "{dimension: [values]} map to write for this mode"},
            },
            "required": ["campaign_id", "mode", "targeting"],
        },
    ),
    Tool(
        name="set_sub_zone_targeting",
        description="Append sub-zones to a campaign's include or exclude list. Spec: PATCH /adv/campaigns/{id}/targeting/{kind}/sub_zone.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "kind": {"type": "string", "enum": ["include", "exclude"]},
                "sub_zone_ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["campaign_id", "kind", "sub_zone_ids"],
        },
    ),
    Tool(
        name="set_sub_zone_other",
        description="Exclude 'Other' subzones. mode=ALL excludes all globally; mode=TARGET_ZONES excludes per given zone_ids.",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "mode": {"type": "string", "enum": ["ALL", "TARGET_ZONES"]},
                "zone_ids": {"type": "array", "items": {"type": "integer"}, "description": "Required for TARGET_ZONES"},
            },
            "required": ["campaign_id", "mode"],
        },
    ),
    # ===== Misc reads =====
    Tool(
        name="get_zone_group",
        description="Get one zone group's detail. Spec: GET /adv/zone-groups/{id}.",
        inputSchema={
            "type": "object",
            "properties": {"group_id": {"type": "integer"}},
            "required": ["group_id"],
        },
    ),
    Tool(
        name="get_campaigns_rates",
        description="Rate rows for a LIST of campaigns. Spec: GET /adv/campaigns/rates (campaign_ids required).",
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_ids": {"type": "array", "items": {"type": "integer"}, "description": "Campaign IDs to fetch rates for"},
                "only_active": {"type": "integer", "enum": [0, 1], "description": "1=active only (default)"},
            },
            "required": ["campaign_ids"],
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls."""
    try:
        client = get_client()
        result = await handle_tool(client, name, arguments)
        return [TextContent(type="text", text=result)]
    except PropellerAdsError as e:
        return [TextContent(type="text", text=f"PropellerAds API Error: {str(e)}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def handle_tool(
    client: PropellerAdsClient, name: str, args: dict[str, Any]
) -> str:
    """Route tool calls to appropriate handlers."""

    # Campaign Management
    if name == "list_campaigns":
        campaigns = client.list_campaigns(
            status=args.get("status"),
            ad_format=args.get("ad_format"),
            name=args.get("name"),
        )
        if not campaigns:
            return "No campaigns found matching the criteria."

        lines = ["# Campaigns\n"]
        any_paused = False
        for c in campaigns:
            # status is an integer enum (6=working, 7=paused, 8=stopped), not a string
            if _status_int(c.get("status")) == 7:
                any_paused = True
            budget = c.get("limit_daily_amount", c.get("daily_amount"))
            archived = " [archived]" if c.get("is_archived") else ""
            lines.append(
                f"{status_label(c.get('status'))} **{c.get('name', 'Unnamed')}** (ID: {c.get('id')}){archived}\n"
                f"   Rate model: {c.get('rate_model', 'N/A')} | "
                f"Daily cap: {format_currency(budget)}\n"
            )
        if any_paused:
            lines.append(
                "\n> Note on **paused** (status 7): the API does NOT expose *why* a campaign "
                "is paused. PropellerAds collapses three different states into status 7 — "
                "(a) auto-pacing / late-click protection or the daily-impressions limit, which "
                "**auto-resumes** (the campaign is effectively live, just throttled to avoid "
                "overspending the daily cap on late clicks); (b) the daily budget being reached; "
                "and (c) a real manual pause. Do NOT treat paused as stopped. To tell them apart, "
                "check recent daily spend (query_statistics grouped by date) — a campaign still "
                "spending day over day is pacing, not stopped — or read the dashboard status badge.\n"
            )
        return "\n".join(lines)

    elif name == "get_campaign_details":
        campaign = client.get_campaign(args["campaign_id"])
        return f"# Campaign Details\n\n```json\n{json.dumps(campaign, indent=2)}\n```"

    elif name == "create_campaign":
        campaign_data = _build_campaign_payload(args)
        if args.get("dry_run"):
            return (
                "DRY RUN — payload NOT sent. This is the exact body that would POST "
                "to /adv/campaigns:\n\n```json\n"
                f"{json.dumps(campaign_data, indent=2)}\n```"
            )
        result = client.create_campaign(campaign_data)
        return f"Campaign created (draft unless status=2).\n\n```json\n{json.dumps(result, indent=2)}\n```"

    elif name == "update_campaign":
        campaign_id = args.pop("campaign_id")
        updates = {k: v for k, v in args.items() if v is not None}
        result = client.update_campaign(campaign_id, updates)
        return f"Campaign {campaign_id} updated successfully!\n\n```json\n{json.dumps(result, indent=2)}\n```"

    elif name == "start_campaigns":
        result = client.start_campaigns(args["campaign_ids"])
        return f"Started campaigns: {args['campaign_ids']}\n\n{json.dumps(result, indent=2)}"

    elif name == "stop_campaigns":
        result = client.stop_campaigns(args["campaign_ids"])
        return f"Stopped campaigns: {args['campaign_ids']}\n\n{json.dumps(result, indent=2)}"

    elif name == "clone_campaign":
        result = client.clone_campaign(
            args["campaign_id"], args.get("new_name")
        )
        return f"Campaign cloned successfully!\n\n```json\n{json.dumps(result, indent=2)}\n```"

    # Creative (banner) control
    elif name == "start_creatives":
        result = client.start_creatives(args["creative_ids"])
        return f"Started creatives: {args['creative_ids']}\n\n{json.dumps(result, indent=2)}"

    elif name == "stop_creatives":
        result = client.stop_creatives(args["creative_ids"])
        return f"Stopped creatives: {args['creative_ids']}\n\n{json.dumps(result, indent=2)}"

    elif name == "get_campaign_rates":
        rates = client.get_campaign_rates(
            args["campaign_id"], only_active=args.get("only_active", 1)
        )
        if not rates:
            return f"No rates found for campaign {args['campaign_id']}."
        lines = [f"# Rates: campaign {args['campaign_id']}\n"]
        for r in rates:
            geo = ", ".join(r.get("countries", []) or []) or "all"
            lines.append(
                f"- Rate {r.get('id')}: {format_currency(_num(r.get('amount')))} "
                f"| countries: {geo} | finished_at: {r.get('finished_at')}\n"
            )
        return "".join(lines)

    elif name == "set_campaign_rate":
        rate: dict[str, Any] = {"amount": args["amount"]}
        if args.get("countries"):
            rate["countries"] = args["countries"]
        result = client.set_campaign_rates(args["campaign_id"], [rate])
        return (
            f"Replaced rates for campaign {args['campaign_id']} with "
            f"{format_currency(_num(args['amount']))} "
            f"({', '.join(args.get('countries') or ['all countries'])}).\n\n"
            f"{json.dumps(result, indent=2)}"
        )

    elif name == "update_campaign_url":
        result = client.update_campaign_url(args["campaign_id"], args["url"])
        return (
            f"Updated target URL for campaign {args['campaign_id']}. "
            f"NOTE: the campaign now re-enters moderation.\n\n{json.dumps(result, indent=2)}"
        )

    elif name == "get_zone_rates":
        rates = client.get_zone_rates(args["campaign_id"])
        if not rates:
            return f"No per-zone rate overrides on campaign {args['campaign_id']}."
        lines = [f"# Per-zone rates: campaign {args['campaign_id']}\n"]
        for r in rates:
            lines.append(f"- Zone {r.get('zone_id')}: {format_currency(_num(r.get('amount')))}\n")
        return "".join(lines)

    elif name == "set_zone_rate":
        result = client.set_zone_rate(args["campaign_id"], args["zone_id"], args["amount"])
        return (
            f"Set zone {args['zone_id']} bid to {format_currency(_num(args['amount']))} "
            f"on campaign {args['campaign_id']}.\n\n{json.dumps(result, indent=2)}"
        )

    elif name == "delete_zone_rate":
        client.delete_zone_rate(args["campaign_id"], args["zone_id"])
        return f"Removed zone {args['zone_id']} bid override on campaign {args['campaign_id']} (reverts to base bid)."

    elif name == "get_zone_groups":
        groups = client.get_zone_groups()
        return f"# Zone Groups\n\n```json\n{json.dumps(groups, indent=2)}\n```"

    elif name == "query_statistics":
        def _rng(mn: Any, mx: Any) -> dict[str, int] | None:
            d: dict[str, int] = {}
            if mn is not None:
                d["from"] = int(mn)
            if mx is not None:
                d["to"] = int(mx)
            return d or None

        filters: dict[str, dict[str, int]] = {}
        if (r := _rng(args.get("min_impressions"), None)):
            filters["impressions"] = r
        if (r := _rng(args.get("min_clicks"), None)):
            filters["clicks"] = r
        if (r := _rng(args.get("min_conversions"), args.get("max_conversions"))):
            filters["conversions"] = r
        if (r := _rng(args.get("min_spend"), args.get("max_spend"))):
            filters["spent"] = r

        rows = client.query_statistics(
            day_from=args["day_from"],
            day_to=args["day_to"],
            group_by=args.get("group_by"),
            campaign_id=args.get("campaign_id"),
            order_by=args.get("order_by"),
            order_dest=args.get("order_dest", "desc"),
            filters=filters or None,
        )
        if not rows:
            return "No rows matched the filters."

        dims = ("campaign_id", "zone_id", "banner_id", "country_id", "date_time")
        lines = [f"# Statistics ({len(rows)} rows)\n"]
        for row in rows:
            m = calculate_metrics(row)
            key = " ".join(f"{d}={row[d]}" for d in dims if row.get(d) is not None) or "(all)"
            lines.append(
                f"- {key} | impr {m['impressions']:,} | clicks {m['clicks']:,} | "
                f"conv {m['conversions']} (g2 {m['conversions2']}) | "
                f"spent {format_currency(m['spend'])} | CPA {format_currency(m['cpa'])}\n"
            )
        return "".join(lines)

    # Statistics
    elif name == "get_performance_report":
        stats = client.get_statistics(
            date_from=args.get("date_from"),
            date_to=args.get("date_to"),
            group_by=args.get("group_by"),
            campaign_id=args.get("campaign_id"),
        )

        if not stats:
            return "No statistics found for the specified period."

        # Calculate metrics for each entry
        enriched = [calculate_metrics(s) for s in stats] if isinstance(stats, list) else [calculate_metrics(stats)]

        lines = ["# Performance Report\n"]
        for s in enriched:
            lines.append(
                f"- Impressions: {s.get('impressions', 0):,}\n"
                f"- Clicks: {s.get('clicks', 0):,}\n"
                f"- CTR: {format_percentage(s.get('ctr'))}\n"
                f"- Conversions (goal 1): {s.get('conversions', 0):,}\n"
                f"- Conversions (goal 2 / trial-to-paid): {s.get('conversions2', 0):,}\n"
                f"- CVR: {format_percentage(s.get('cvr'))}\n"
                f"- Spend: {format_currency(s.get('spend', 0))}\n"
                f"- CPC: {format_currency(s.get('cpc'))}\n"
                f"- CPA: {format_currency(s.get('cpa'))}\n"
                f"- Revenue: {format_currency(s.get('revenue', 0))}\n"
                f"- ROI: {format_percentage(s.get('roi'))}\n"
            )
        return "\n".join(lines)

    elif name == "get_campaign_performance":
        stats = client.get_campaign_statistics(
            args["campaign_id"],
            date_from=args.get("date_from"),
            date_to=args.get("date_to"),
        )
        if not stats:
            return f"No statistics found for campaign {args['campaign_id']}."

        metrics = calculate_metrics(stats)
        campaign = client.get_campaign(args["campaign_id"])

        return (
            f"# Campaign Performance: {campaign.get('name', 'N/A')}\n\n"
            f"**Status:** {status_label(campaign.get('status'))}\n"
            f"**Rate model:** {campaign.get('rate_model', 'N/A')}\n\n"
            f"## Metrics\n"
            f"- Impressions: {metrics.get('impressions', 0):,}\n"
            f"- Clicks: {metrics.get('clicks', 0):,}\n"
            f"- CTR: {format_percentage(metrics.get('ctr'))}\n"
            f"- Conversions (goal 1): {metrics.get('conversions', 0):,}\n"
            f"- Conversions (goal 2 / trial-to-paid): {metrics.get('conversions2', 0):,}\n"
            f"- CVR: {format_percentage(metrics.get('cvr'))}\n"
            f"- Spend: {format_currency(metrics.get('spend', 0))}\n"
            f"- CPA: {format_currency(metrics.get('cpa'))}\n"
            f"- Revenue: {format_currency(metrics.get('revenue', 0))}\n"
            f"- ROI: {format_percentage(metrics.get('roi'))}\n"
        )

    elif name == "compare_periods":
        stats1 = client.get_statistics(
            date_from=args["period1_from"],
            date_to=args["period1_to"],
            campaign_id=args.get("campaign_id"),
        )
        stats2 = client.get_statistics(
            date_from=args["period2_from"],
            date_to=args["period2_to"],
            campaign_id=args.get("campaign_id"),
        )

        m1 = calculate_metrics(stats1[0] if stats1 else {})
        m2 = calculate_metrics(stats2[0] if stats2 else {})

        def change(v1: float, v2: float) -> str:
            if v1 == 0:
                return "N/A"
            pct = ((v2 - v1) / v1) * 100
            arrow = "📈" if pct > 0 else "📉" if pct < 0 else "➡️"
            return f"{arrow} {pct:+.1f}%"

        return (
            f"# Period Comparison\n\n"
            f"**Period 1:** {args['period1_from']} to {args['period1_to']}\n"
            f"**Period 2:** {args['period2_from']} to {args['period2_to']}\n\n"
            f"| Metric | Period 1 | Period 2 | Change |\n"
            f"|--------|----------|----------|--------|\n"
            f"| Impressions | {m1.get('impressions', 0):,} | {m2.get('impressions', 0):,} | {change(m1.get('impressions', 0), m2.get('impressions', 0))} |\n"
            f"| Clicks | {m1.get('clicks', 0):,} | {m2.get('clicks', 0):,} | {change(m1.get('clicks', 0), m2.get('clicks', 0))} |\n"
            f"| CTR | {format_percentage(m1.get('ctr'))} | {format_percentage(m2.get('ctr'))} | {change(m1.get('ctr', 0), m2.get('ctr', 0))} |\n"
            f"| Conversions | {m1.get('conversions', 0):,} | {m2.get('conversions', 0):,} | {change(m1.get('conversions', 0), m2.get('conversions', 0))} |\n"
            f"| Spend | {format_currency(m1.get('spend', 0))} | {format_currency(m2.get('spend', 0))} | {change(m1.get('spend', 0), m2.get('spend', 0))} |\n"
            f"| ROI | {format_percentage(m1.get('roi'))} | {format_percentage(m2.get('roi'))} | {change(m1.get('roi', 0), m2.get('roi', 0))} |\n"
        )

    elif name == "get_zone_performance":
        zones = client.get_zone_statistics(
            campaign_id=args.get("campaign_id"),
            date_from=args.get("date_from"),
            date_to=args.get("date_to"),
            limit=args.get("limit", 100),
        )

        if not zones:
            return "No zone statistics found."

        enriched = [calculate_metrics(z) for z in zones]

        # Sort if requested
        sort_by = args.get("sort_by", "spend")
        enriched.sort(key=lambda x: x.get(sort_by, 0), reverse=True)

        lines = ["# Zone Performance\n\n"]
        lines.append("| Zone ID | Impressions | Clicks | CTR | Conv | Spend | ROI |\n")
        lines.append("|---------|-------------|--------|-----|------|-------|-----|\n")

        for z in enriched[:args.get("limit", 100)]:
            lines.append(
                f"| {z.get('zone_id', 'N/A')} | "
                f"{z.get('impressions', 0):,} | "
                f"{z.get('clicks', 0):,} | "
                f"{format_percentage(z.get('ctr'))} | "
                f"{z.get('conversions', 0)} | "
                f"{format_currency(z.get('spend', z.get('cost', 0)))} | "
                f"{format_percentage(z.get('roi'))} |\n"
            )

        return "".join(lines)

    elif name == "get_creative_performance":
        creatives = client.get_creative_statistics(
            campaign_id=args.get("campaign_id"),
            date_from=args.get("date_from"),
            date_to=args.get("date_to"),
        )

        if not creatives:
            return "No creative statistics found."

        enriched = [calculate_metrics(c) for c in creatives]

        lines = ["# Creative Performance\n\n"]
        for c in enriched:
            lines.append(
                f"**Creative {c.get('creative_id', 'N/A')}**\n"
                f"- Impressions: {c.get('impressions', 0):,}\n"
                f"- Clicks: {c.get('clicks', 0):,}\n"
                f"- CTR: {format_percentage(c.get('ctr'))}\n"
                f"- Conversions: {c.get('conversions', 0)}\n"
                f"- Spend: {format_currency(c.get('spend', c.get('cost', 0)))}\n\n"
            )

        return "".join(lines)

    # Optimization
    elif name == "find_underperforming_zones":
        zones = client.get_zone_statistics(
            campaign_id=args["campaign_id"],
            date_from=args.get("date_from"),
            date_to=args.get("date_to"),
        )

        min_spend = args.get("min_spend", 10)
        max_conv = args.get("max_conversions", 0)

        underperforming = []
        for z in zones:
            spend = z.get("spend", z.get("cost", 0)) or 0
            conv = z.get("conversions", 0) or 0
            if spend >= min_spend and conv <= max_conv:
                underperforming.append(z)

        if not underperforming:
            return f"No underperforming zones found (min spend: ${min_spend}, max conversions: {max_conv})."

        underperforming.sort(key=lambda x: x.get("spend", x.get("cost", 0)) or 0, reverse=True)

        lines = [f"# Underperforming Zones (Campaign {args['campaign_id']})\n\n"]
        lines.append(f"Criteria: Spend >= ${min_spend}, Conversions <= {max_conv}\n\n")
        lines.append("| Zone ID | Spend | Conversions | Clicks |\n")
        lines.append("|---------|-------|-------------|--------|\n")

        total_waste = 0
        for z in underperforming:
            spend = z.get("spend", z.get("cost", 0)) or 0
            total_waste += spend
            lines.append(
                f"| {z.get('zone_id')} | "
                f"{format_currency(spend)} | "
                f"{z.get('conversions', 0)} | "
                f"{z.get('clicks', 0)} |\n"
            )

        lines.append(f"\n**Total wasted spend:** {format_currency(total_waste)}\n")
        lines.append(f"**Zones to blacklist:** {len(underperforming)}\n")

        zone_ids = [z.get("zone_id") for z in underperforming if z.get("zone_id")]
        lines.append(f"\nZone IDs: `{zone_ids}`")

        return "".join(lines)

    elif name == "find_top_zones":
        zones = client.get_zone_statistics(
            campaign_id=args["campaign_id"],
            date_from=args.get("date_from"),
            date_to=args.get("date_to"),
        )

        min_conv = args.get("min_conversions", 1)
        min_roi = args.get("min_roi", 0)
        limit = args.get("limit", 20)

        enriched = [calculate_metrics(z) for z in zones]
        top_zones = [
            z for z in enriched
            if (z.get("conversions", 0) or 0) >= min_conv and (z.get("roi", 0) or 0) >= min_roi
        ]
        top_zones.sort(key=lambda x: x.get("roi", 0), reverse=True)

        if not top_zones:
            return f"No zones found matching criteria (min conversions: {min_conv}, min ROI: {min_roi}%)."

        lines = [f"# Top Performing Zones (Campaign {args['campaign_id']})\n\n"]
        lines.append("| Zone ID | Conversions | Spend | Revenue | ROI |\n")
        lines.append("|---------|-------------|-------|---------|-----|\n")

        for z in top_zones[:limit]:
            lines.append(
                f"| {z.get('zone_id')} | "
                f"{z.get('conversions', 0)} | "
                f"{format_currency(z.get('spend', z.get('cost', 0)))} | "
                f"{format_currency(z.get('revenue', 0))} | "
                f"{format_percentage(z.get('roi'))} |\n"
            )

        zone_ids = [z.get("zone_id") for z in top_zones[:limit] if z.get("zone_id")]
        lines.append(f"\nZone IDs for whitelist: `{zone_ids}`")

        return "".join(lines)

    elif name == "find_scaling_opportunities":
        campaigns = client.list_campaigns(status="active")
        if not campaigns:
            return "No active campaigns found."

        min_roi = args.get("min_roi", 50)
        min_conv = args.get("min_conversions", 10)

        opportunities = []
        for c in campaigns:
            stats = client.get_campaign_statistics(
                c["id"],
                date_from=args.get("date_from"),
                date_to=args.get("date_to"),
            )
            if stats:
                metrics = calculate_metrics(stats)
                conv = metrics.get("conversions", 0) or 0
                roi = metrics.get("roi", 0) or 0
                if conv >= min_conv and roi >= min_roi:
                    opportunities.append({**c, **metrics})

        if not opportunities:
            return f"No scaling opportunities found (min ROI: {min_roi}%, min conversions: {min_conv})."

        opportunities.sort(key=lambda x: x.get("roi", 0), reverse=True)

        lines = ["# Scaling Opportunities\n\n"]
        lines.append(f"Criteria: ROI >= {min_roi}%, Conversions >= {min_conv}\n\n")

        for c in opportunities:
            lines.append(
                f"### {c.get('name')} (ID: {c.get('id')})\n"
                f"- ROI: {format_percentage(c.get('roi'))}\n"
                f"- Conversions: {c.get('conversions', 0)}\n"
                f"- Spend: {format_currency(c.get('spend', c.get('cost', 0)))}\n"
                f"- Revenue: {format_currency(c.get('revenue', 0))}\n\n"
            )

        return "".join(lines)

    # Zone Targeting
    elif name == "add_to_blacklist":
        result = client.add_zones_to_blacklist(args["campaign_id"], args["zone_ids"])
        return f"Added {len(args['zone_ids'])} zones to blacklist for campaign {args['campaign_id']}.\n\n{json.dumps(result, indent=2)}"

    elif name == "add_to_whitelist":
        result = client.add_zones_to_whitelist(args["campaign_id"], args["zone_ids"])
        return f"Added {len(args['zone_ids'])} zones to whitelist for campaign {args['campaign_id']}.\n\n{json.dumps(result, indent=2)}"

    elif name == "auto_blacklist_zones":
        zones = client.get_zone_statistics(
            campaign_id=args["campaign_id"],
            date_from=args.get("date_from"),
            date_to=args.get("date_to"),
        )

        min_spend = args.get("min_spend", 10)
        max_conv = args.get("max_conversions", 0)
        dry_run = args.get("dry_run", True)

        underperforming = []
        for z in zones:
            spend = z.get("spend", z.get("cost", 0)) or 0
            conv = z.get("conversions", 0) or 0
            if spend >= min_spend and conv <= max_conv:
                underperforming.append(z)

        if not underperforming:
            return "No underperforming zones found."

        zone_ids = [z.get("zone_id") for z in underperforming if z.get("zone_id")]
        total_spend = sum(z.get("spend", z.get("cost", 0)) or 0 for z in underperforming)

        if dry_run:
            return (
                f"# Dry Run - Zones to Blacklist\n\n"
                f"Found {len(zone_ids)} underperforming zones.\n"
                f"Total wasted spend: {format_currency(total_spend)}\n\n"
                f"Zone IDs: `{zone_ids}`\n\n"
                f"Run with `dry_run: false` to actually blacklist these zones."
            )
        else:
            result = client.add_zones_to_blacklist(args["campaign_id"], zone_ids)
            return (
                f"# Zones Blacklisted\n\n"
                f"Blacklisted {len(zone_ids)} zones.\n"
                f"Potential savings: {format_currency(total_spend)}\n\n"
                f"Zone IDs: `{zone_ids}`"
            )

    # Account
    elif name == "get_balance":
        balance = client.get_balance()
        return f"# Account Balance\n\n```json\n{json.dumps(balance, indent=2)}\n```"

    elif name == "get_available_countries":
        countries = client.get_countries()
        return f"# Available Countries\n\n```json\n{json.dumps(countries, indent=2)}\n```"

    elif name == "get_ad_formats":
        formats = client.get_ad_formats()
        return f"# Available Ad Formats\n\n```json\n{json.dumps(formats, indent=2)}\n```"

    # ===== Targeting collections =====
    elif name == "get_targeting_options":
        options = client.get_collection(args["type"])
        return f"# Targeting options: {args['type']}\n\n```json\n{json.dumps(options, indent=2)}\n```"

    elif name == "list_collection_types":
        types = client.list_collection_types()
        return f"# Collection Types\n\n```json\n{json.dumps(types, indent=2)}\n```"

    # ===== Creatives =====
    elif name == "list_creatives":
        creatives = client.list_campaign_creatives(args["campaign_id"])
        return f"# Creatives for campaign {args['campaign_id']}\n\n```json\n{json.dumps(creatives, indent=2)}\n```"

    elif name == "create_creative":
        creative = {
            k: args[k]
            for k in (
                "title", "description", "icon", "image", "skin", "buttons",
                "default_button_disabled", "status", "is_auto", "language_mode", "language",
            )
            if args.get(k) is not None
        }
        if isinstance(args.get("creative"), dict):
            creative.update(args["creative"])
        result = client.create_campaign_creative(args["campaign_id"], creative)
        return f"Creative added to campaign {args['campaign_id']}.\n\n```json\n{json.dumps(result, indent=2)}\n```"

    elif name == "update_creative":
        result = client.update_creative(args["creative_id"], args["updates"])
        return f"Creative {args['creative_id']} updated.\n\n```json\n{json.dumps(result, indent=2)}\n```"

    # ===== Campaign targeting management =====
    elif name == "get_campaign_targeting":
        data = client.get_campaign_targeting(args["campaign_id"], args["mode"])
        return f"# Targeting ({args['mode']}) on campaign {args['campaign_id']}\n\n```json\n{json.dumps(data, indent=2)}\n```"

    elif name == "set_campaign_targeting":
        result = client.set_campaign_targeting(args["campaign_id"], args["mode"], args["targeting"])
        return f"Targeting ({args['mode']}) replaced on campaign {args['campaign_id']}.\n\n```json\n{json.dumps(result, indent=2)}\n```"

    elif name == "set_sub_zone_targeting":
        result = client.add_sub_zones(args["campaign_id"], args["kind"], args["sub_zone_ids"])
        return f"Added {len(args['sub_zone_ids'])} sub-zones to {args['kind']} on campaign {args['campaign_id']}.\n\n```json\n{json.dumps(result, indent=2)}\n```"

    elif name == "set_sub_zone_other":
        result = client.set_sub_zone_other(args["campaign_id"], args["mode"], args.get("zone_ids"))
        return f"sub_zone_other ({args['mode']}) set on campaign {args['campaign_id']}.\n\n```json\n{json.dumps(result, indent=2)}\n```"

    # ===== Misc reads =====
    elif name == "get_zone_group":
        group = client.get_zone_group(args["group_id"])
        return f"# Zone Group {args['group_id']}\n\n```json\n{json.dumps(group, indent=2)}\n```"

    elif name == "get_campaigns_rates":
        rates = client.get_campaigns_rates(args["campaign_ids"], only_active=args.get("only_active", 1))
        return f"# Campaign Rates\n\n```json\n{json.dumps(rates, indent=2)}\n```"

    else:
        return f"Unknown tool: {name}"


def main():
    """Run the MCP server."""
    import asyncio

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
