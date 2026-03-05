"""
OpenAI-backed helpers for agent chat and summarization.

Keeps all OpenAI client usage and message formatting in one place.
"""

import os

from django.conf import settings
from django.http import HttpRequest

from openai import OpenAI
from rest_framework.response import Response

from .models import (
    AgentConversation,
    AgentMessage,
    BusinessProfile,
    ReviewsConversation,
    ReviewsMessage,
)


def build_seo_system_prompt(user, profile: BusinessProfile | None) -> str:
    """
    Build the system prompt for the SEO agent, optionally using business profile context.
    """
    base = (
        "You are an expert SEO agent that helps a small business understand and act on their search data. "
        "You speak plainly, avoid jargon, and focus on high-intent, revenue-generating opportunities. "
        "Always be specific and actionable."
    )
    if profile:
        details: list[str] = []
        if profile.business_name:
            details.append(f"Business name: {profile.business_name}.")
        if profile.industry:
            details.append(f"Industry: {profile.industry}.")
        if profile.description:
            details.append(f"Business description: {profile.description}.")
        if details:
            base += " Here is context about the business: " + " ".join(details)
    return base


def build_reviews_system_prompt(user, profile: BusinessProfile | None) -> str:
    """
    Build the system prompt for the Reviews Agent (trust, reputation, review response).
    Different role from SEO; same structure (optionally use business profile context).
    """
    base = (
        "You are an expert Reviews and Reputation agent that helps a small business "
        "build trust, respond to reviews, and turn feedback into marketing leverage. "
        "You focus on: responding to reviews in a brand-aligned way, identifying praise themes "
        "for ad copy, flagging recurring complaints, and improving close rate through trust. "
        "You speak plainly and are specific and actionable. "
        "Never argue with reviewers; never sound robotic."
    )
    if profile:
        details: list[str] = []
        if profile.business_name:
            details.append(f"Business name: {profile.business_name}.")
        if profile.industry:
            details.append(f"Industry: {profile.industry}.")
        if profile.tone_of_voice:
            details.append(f"Tone of voice: {profile.tone_of_voice}.")
        if profile.description:
            details.append(f"Business description: {profile.description}.")
        if details:
            base += " Here is context about the business: " + " ".join(details)
    return base


def _get_client(api_key_env: str | None = None) -> OpenAI:
    """
    Return an OpenAI client using the given env var for the API key.

    - If api_key_env is set (e.g. OPEN_AI_SEO_API_KEY, OPEN_AI_REVIEWS_API_KEY),
      use that env var, then fall back to OPENAI_API_KEY.
    - Otherwise use OPENAI_API_KEY.
    """
    if api_key_env:
        api_key = os.getenv(api_key_env) or os.getenv("OPENAI_API_KEY")
    else:
        api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        return OpenAI(api_key=api_key)
    return OpenAI()


def _get_model() -> str:
    return getattr(settings, "OPENAI_MODEL", "gpt-4o-mini")


def _get_chat_reply(
    system_prompt: str,
    recent_messages: list,
    conversation_summary: str | None = None,
    api_key_env: str | None = None,
) -> str:
    """
    Call OpenAI chat completion. recent_messages must have .role and .content.
    api_key_env: env var for API key (e.g. OPEN_AI_SEO_API_KEY, OPEN_AI_REVIEWS_API_KEY).
    Returns the assistant reply text.
    """
    openai_messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
    ]
    if conversation_summary:
        openai_messages.append(
            {
                "role": "system",
                "content": f"Conversation summary so far (memory): {conversation_summary}",
            },
        )
    for msg in recent_messages:
        openai_messages.append(
            {"role": msg.role, "content": msg.content},
        )

    client = _get_client(api_key_env)
    model = _get_model()
    completion = client.chat.completions.create(
        model=model,
        messages=openai_messages,
    )
    return (completion.choices[0].message.content or "").strip()


def get_seo_chat_reply(
    system_prompt: str,
    recent_messages: list[AgentMessage],
    conversation_summary: str | None = None,
) -> str:
    """Call OpenAI for SEO agent using OPEN_AI_SEO_API_KEY."""
    return _get_chat_reply(
        system_prompt,
        recent_messages,
        conversation_summary,
        api_key_env="OPEN_AI_SEO_API_KEY",
    )


def summarize_seo_conversation(messages: list[AgentMessage]) -> str:
    """
    Ask OpenAI to summarize a list of messages into concise memory notes.
    """
    payload: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "Summarize the following SEO conversation into concise memory notes. "
                "Capture key goals, constraints, and decisions. 5-10 bullet points max."
            ),
        },
    ]
    for m in messages:
        payload.append({"role": m.role, "content": m.content})

    client = _get_client("OPEN_AI_SEO_API_KEY")
    model = _get_model()
    completion = client.chat.completions.create(
        model=model,
        messages=payload,
    )
    return (completion.choices[0].message.content or "").strip()


def summarize_reviews_conversation(messages: list[ReviewsMessage]) -> str:
    """
    Ask OpenAI to summarize a Reviews conversation into concise memory notes.
    """
    payload: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "Summarize the following Reviews/Reputation conversation into concise memory notes. "
                "Capture key goals, tone preferences, and decisions. 5-10 bullet points max."
            ),
        },
    ]
    for m in messages:
        payload.append({"role": m.role, "content": m.content})

    client = _get_client("OPEN_AI_REVIEWS_API_KEY")
    model = _get_model()
    completion = client.chat.completions.create(
        model=model,
        messages=payload,
    )
    return (completion.choices[0].message.content or "").strip()


def seo_chat(request: HttpRequest) -> Response:
    """
    Core implementation of the SEO agent chat endpoint backed by OpenAI.

    This is called by the Django view wrapper in accounts.views.
    """
    data = request.data
    message = (data.get("message") or "").strip()
    if not message:
        return Response({"detail": "Message is required."}, status=400)

    conversation_id = data.get("conversation_id")

    # Get or create conversation for this user & agent "seo"
    conversation: AgentConversation | None = None
    if conversation_id:
        try:
            conversation = AgentConversation.objects.get(
                id=conversation_id,
                user=request.user,
                agent="seo",
            )
        except AgentConversation.DoesNotExist:
            conversation = None

    if not conversation:
        conversation = AgentConversation.objects.create(
            user=request.user,
            agent="seo",
            title="SEO Agent Chat",
        )

    # Store the user message
    AgentMessage.objects.create(
        conversation=conversation,
        role="user",
        content=message,
    )

    # Build message history (last N messages) for context
    recent_messages = list(
        conversation.messages.order_by("-created_at")[:20],
    )
    recent_messages.reverse()  # oldest → newest

    profile = BusinessProfile.objects.filter(user=request.user).first()
    system_prompt = build_seo_system_prompt(request.user, profile)
    assistant_reply = get_seo_chat_reply(
        system_prompt,
        recent_messages,
        conversation_summary=conversation.summary or None,
    )

    # Store assistant reply
    AgentMessage.objects.create(
        conversation=conversation,
        role="assistant",
        content=assistant_reply,
    )

    # Periodic summarization to keep history manageable
    total_messages = conversation.messages.count()
    if total_messages > 40:
        summary_messages = list(
            conversation.messages.order_by("created_at")[:80],
        )
        conversation.summary = summarize_seo_conversation(summary_messages)
        conversation.save(update_fields=["summary", "updated_at"])

    return Response(
        {
            "conversation_id": conversation.id,
            "reply": assistant_reply,
        },
    )


def reviews_chat(request: HttpRequest) -> Response:
    """
    Core implementation of the Reviews Agent chat endpoint. Same pattern as SEO chat
    but uses ReviewsConversation and ReviewsMessage (separate tables) and a different system role.
    """
    data = request.data
    message = (data.get("message") or "").strip()
    if not message:
        return Response({"detail": "Message is required."}, status=400)

    conversation_id = data.get("conversation_id")

    conversation: ReviewsConversation | None = None
    if conversation_id:
        try:
            conversation = ReviewsConversation.objects.get(
                id=conversation_id,
                user=request.user,
            )
        except ReviewsConversation.DoesNotExist:
            conversation = None

    if not conversation:
        conversation = ReviewsConversation.objects.create(
            user=request.user,
            title="Reviews Agent Chat",
        )

    ReviewsMessage.objects.create(
        conversation=conversation,
        role="user",
        content=message,
    )

    recent_messages = list(
        conversation.messages.order_by("-created_at")[:20],
    )
    recent_messages.reverse()

    profile = BusinessProfile.objects.filter(user=request.user).first()
    system_prompt = build_reviews_system_prompt(request.user, profile)
    assistant_reply = _get_chat_reply(
        system_prompt,
        recent_messages,
        conversation_summary=conversation.summary or None,
        api_key_env="OPEN_AI_REVIEWS_API_KEY",
    )

    ReviewsMessage.objects.create(
        conversation=conversation,
        role="assistant",
        content=assistant_reply,
    )

    total_messages = conversation.messages.count()
    if total_messages > 40:
        summary_messages = list(
            conversation.messages.order_by("created_at")[:80],
        )
        conversation.summary = summarize_reviews_conversation(summary_messages)
        conversation.save(update_fields=["summary", "updated_at"])

    return Response(
        {
            "conversation_id": conversation.id,
            "reply": assistant_reply,
        },
    )
