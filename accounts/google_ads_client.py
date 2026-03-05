from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from django.conf import settings

from .models import GoogleAdsConnection, GoogleAdsKeywordIdea

logger = logging.getLogger(__name__)


@dataclass
class KeywordIdea:
    keyword: str
    avg_monthly_searches: int | None
    competition: str | None
    competition_index: int | None
    low_top_of_page_bid_micros: int | None
    high_top_of_page_bid_micros: int | None


def _has_app_ads_credentials() -> bool:
    """App-level credentials (developer token, OAuth client). Refresh token comes from user auth."""
    required = [
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ]
    return all(getattr(settings, name, None) for name in required)


def classify_intent(keyword: str) -> str:
    """
    Simple rule-based intent classification.
    Does not rely on Google Ads-provided intent.
    """
    k = keyword.lower()
    high_triggers = [
        "buy",
        "price",
        "cost",
        "near me",
        "coupon",
        "deal",
        "best",
        "hire",
        "book",
        "quote",
        "service",
    ]
    low_triggers = [
        "what is",
        "definition",
        "how to",
        "tutorial",
        "examples",
        "guide",
        "meaning",
    ]

    if any(t in k for t in high_triggers):
        return "HIGH"
    if any(t in k for t in low_triggers):
        return "LOW"
    return "MEDIUM"


def fetch_keyword_ideas_for_user(
    user_id: int,
    keywords: Iterable[str],
    cache_ttl_days: int = 7,
    industry: str | None = None,
    description: str | None = None,
) -> dict[str, KeywordIdea]:
    """
    Fetch keyword ideas from Google Ads KeywordPlanIdeaService for the given user & keywords.

    Results are cached per user/keyword in GoogleAdsKeywordIdea to reduce API calls.
    If Google Ads credentials are missing, this returns only cached data.
    """
    from google.ads.googleads.client import GoogleAdsClient  # type: ignore[import]

    keywords_list = list(keywords)
    logger.info(
        "[Google Ads] fetch_keyword_ideas_for_user: user_id=%s, keywords_count=%s, industry=%s, description_len=%s",
        user_id,
        len(keywords_list),
        industry or "(none)",
        len(description) if description else 0,
    )

    now = datetime.now(timezone.utc)

    # First, load cached ideas.
    cached: dict[str, KeywordIdea] = {}
    fresh_cutoff = now - timedelta(days=cache_ttl_days)

    for idea in GoogleAdsKeywordIdea.objects.filter(
        user_id=user_id, last_fetched_at__gte=fresh_cutoff
    ):
        cached[idea.keyword] = KeywordIdea(
            keyword=idea.keyword,
            avg_monthly_searches=idea.avg_monthly_searches,
            competition=idea.competition,
            competition_index=idea.competition_index,
            low_top_of_page_bid_micros=idea.low_top_of_page_bid_micros,
            high_top_of_page_bid_micros=idea.high_top_of_page_bid_micros,
        )

    remaining = [k for k in keywords_list if k not in cached]
    logger.info(
        "[Google Ads] Cached ideas loaded: %s; remaining keywords to fetch: %s",
        len(cached),
        len(remaining),
    )

    # Use business profile context (industry / description) as additional seeds
    # to help Google Ads suggest better ideas, without changing intent logic.
    extra_seeds: list[str] = []
    if industry:
        extra_seeds.append(industry)
    if description:
        # Take a short prefix of the description as a seed (avoid flooding the request).
        snippet = description.strip()
        if len(snippet) > 80:
            snippet = snippet[:80]
        if snippet:
            extra_seeds.append(snippet)

    has_app_creds = _has_app_ads_credentials()
    logger.info(
        "[Google Ads] App credentials present: %s; remaining=%s, extra_seeds=%s",
        has_app_creds,
        len(remaining),
        extra_seeds,
    )

    if (not remaining and not extra_seeds) or not has_app_creds:
        logger.info(
            "[Google Ads] Early return (no seeds or no app creds). Returning %s cached.",
            len(cached),
        )
        return cached

    # Use the user's Google Ads connection (refresh_token + customer_id from OAuth at login).
    # Do not use env for refresh token — it comes from user auth only.
    try:
        conn = GoogleAdsConnection.objects.get(user_id=user_id)
        logger.info(
            "[Google Ads] User connection found: user_id=%s, has_refresh_token=%s, customer_id=%s",
            user_id,
            bool((conn.refresh_token or "").strip()),
            (conn.customer_id or "").strip() or "(empty)",
        )
    except GoogleAdsConnection.DoesNotExist:
        logger.warning("[Google Ads] No GoogleAdsConnection for user_id=%s. Returning cached only.", user_id)
        return cached
    refresh_token = (conn.refresh_token or "").strip()
    customer_id = (conn.customer_id or "").strip()
    if not customer_id:
        customer_id = (getattr(settings, "GOOGLE_ADS_CUSTOMER_ID", None) or "").strip()
        if customer_id:
            logger.info("[Google Ads] Using GOOGLE_ADS_CUSTOMER_ID from settings (user connection had no customer_id).")
    if not refresh_token or not customer_id:
        logger.warning(
            "[Google Ads] User connection missing refresh_token or customer_id (refresh_token=%s, customer_id=%s). Returning cached.",
            "present" if refresh_token else "missing",
            customer_id or "missing",
        )
        return cached

    # Build Google Ads client using user's tokens and app-level developer token + OAuth client.
    config = {
        "developer_token": settings.GOOGLE_ADS_DEVELOPER_TOKEN,
        "login_customer_id": customer_id.replace("-", ""),
        "client_customer_id": customer_id.replace("-", ""),
        "use_proto_plus": True,
        "refresh_token": refresh_token,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
    }
    customer_id_clean = customer_id.replace("-", "")
    request_seeds = remaining + extra_seeds
    logger.info(
        "[Google Ads] Calling KeywordPlanIdeaService: customer_id=%s, seed_count=%s, seeds=%s",
        customer_id_clean,
        len(request_seeds),
        request_seeds[:10] if len(request_seeds) > 10 else request_seeds,
    )

    client = GoogleAdsClient.load_from_dict(config)
    service = client.get_service("KeywordPlanIdeaService")

    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = customer_id_clean
    request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH_AND_PARTNERS
    # Always include the remaining keywords; optionally add extra business-context seeds.
    if remaining:
        request.keyword_seed.keywords.extend(remaining)
    if extra_seeds:
        request.keyword_seed.keywords.extend(extra_seeds)

    try:
        response = service.generate_keyword_ideas(request=request)
        response_list = list(response)
        logger.info("[Google Ads] API returned %s keyword ideas.", len(response_list))
    except Exception as e:
        logger.exception(
            "[Google Ads] KeywordPlanIdeaService.generate_keyword_ideas failed: %s. Returning cached.",
            e,
        )
        return cached

    added = 0
    # When remaining is empty we're in "recommendation only" mode (seeds from
    # industry/description): accept all ideas from the API. Otherwise only
    # keep ideas that match the requested seed keywords.
    for idea in response_list:
        text = idea.text
        if remaining and text not in remaining:
            continue

        metrics = idea.keyword_idea_metrics
        avg_monthly_searches = (
            int(metrics.avg_monthly_searches) if metrics.avg_monthly_searches else None
        )
        competition_enum = metrics.competition.name if metrics.competition else None
        competition_index = (
            int(metrics.competition_index) if metrics.competition_index else None
        )
        low_bid = (
            int(metrics.low_top_of_page_bid_micros)
            if metrics.low_top_of_page_bid_micros
            else None
        )
        high_bid = (
            int(metrics.high_top_of_page_bid_micros)
            if metrics.high_top_of_page_bid_micros
            else None
        )

        obj, _ = GoogleAdsKeywordIdea.objects.update_or_create(
            user_id=user_id,
            keyword=text,
            defaults={
                "avg_monthly_searches": avg_monthly_searches or 0,
                "competition": competition_enum or "",
                "competition_index": competition_index or 0,
                "low_top_of_page_bid_micros": low_bid or 0,
                "high_top_of_page_bid_micros": high_bid or 0,
                "last_fetched_at": now,
            },
        )

        cached[text] = KeywordIdea(
            keyword=text,
            avg_monthly_searches=avg_monthly_searches,
            competition=competition_enum,
            competition_index=competition_index,
            low_top_of_page_bid_micros=low_bid,
            high_top_of_page_bid_micros=high_bid,
        )
        added += 1

    logger.info(
        "[Google Ads] fetch_keyword_ideas_for_user done: user_id=%s, ideas_added_from_api=%s, total_cached=%s.",
        user_id,
        added,
        len(cached),
    )
    return cached

