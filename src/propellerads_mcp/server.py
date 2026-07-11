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
            "Create a campaign (POST /adv/campaigns). Created in draft (status=1) by default; "
            "start it later with start_campaigns. For Push CPA Goal use direction='nativeads', "
            "rate_model='cpag'; for Onclick popunder use direction='onclick'. CPA/SCPA rate models "
            "require ${SUBID} in target_url. Note: campaigns cannot be deleted via API, only archived."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Campaign name"},
                "direction": {
                    "type": "string",
                    "enum": ["onclick", "nativeads"],
                    "description": "onclick = popunder; nativeads = push/native/interstitial",
                },
                "rate_model": {
                    "type": "string",
                    "enum": ["cpm", "scpm", "cpc", "scpc", "scpa", "cpag"],
                    "description": "cpag = CPA Goal for Push; scpa = CPA Goal for Onclick; cpc/cpm = fixed bid",
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
                "bid": {"type": "number", "description": "Base bid in dollars (builds the rate row)"},
                "started_at": {
                    "type": "string",
                    "description": "Start date in dd/MM/YYYY format",
                },
                "daily_amount": {"type": "number", "description": "Daily budget cap in USD (Push CPC min $10)"},
                "total_amount": {"type": "number", "description": "Total budget cap in USD (must exceed daily)"},
                "cpa_goal_bid": {"type": "number", "description": "Target CPA in dollars (sets cpa_goal_status=true)"},
                "user_activity": {
                    "type": "array",
                    "items": {"type": "integer", "enum": [1, 2, 3]},
                    "description": "1=High, 2=Medium, 3=Low activity (protocol: High+Medium only)",
                },
                "frequency": {"type": "integer", "description": "Impressions per user per capping window"},
                "capping": {"type": "integer", "description": "Frequency-cap window in hours (e.g. 24)"},
                "timezone": {"type": "integer", "description": "UTC offset, default -5"},
                "status": {"type": "integer", "enum": [1, 2], "description": "1=draft (default), 2=submit to moderation"},
            },
            "required": ["name", "direction", "rate_model", "target_url", "countries", "bid", "started_at"],
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
        description="Create a copy of an existing campaign.",
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
        description="Get list of available ad formats.",
        inputSchema={"type": "object", "properties": {}},
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
        countries = [c.lower() for c in args["countries"]]
        targeting: dict[str, Any] = {
            "country": {"list": countries, "is_excluded": False},
        }
        if args.get("user_activity"):
            targeting["user_activity"] = {"list": args["user_activity"], "is_excluded": False}

        campaign_data: dict[str, Any] = {
            "name": args["name"],
            "direction": args["direction"],
            "rate_model": args["rate_model"],
            "target_url": args["target_url"],
            "status": args.get("status", 1),
            "started_at": args["started_at"],
            "timezone": args.get("timezone", -5),
            "targeting": targeting,
            "rates": [{"amount": args["bid"], "countries": countries}],
        }
        for k in ("daily_amount", "total_amount", "frequency", "capping"):
            if args.get(k) is not None:
                campaign_data[k] = args[k]
        if args.get("cpa_goal_bid") is not None:
            campaign_data["cpa_goal_bid"] = args["cpa_goal_bid"]
            campaign_data["cpa_goal_status"] = True

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
