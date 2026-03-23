"""
Prompt Writer Service

Uses an LLM to generate a rich, tailored system prompt for each service agent.
Called when a service is created or updated. The result is stored in
BotService.generated_system_prompt and used at chat time instead of the
static template builder.
"""

import json
from typing import Any, Dict, Optional

from llm.client import llm_client

# ---------------------------------------------------------------------------
# Briefing document — explains to the prompt-writer LLM how this hotel bot
# works so it can write better prompts without being told everything twice.
# ---------------------------------------------------------------------------
_BOT_BRIEFING = """
=== HOW THIS HOTEL CHATBOT WORKS ===

ARCHITECTURE
- A main orchestrator LLM receives every user message first.
- It routes the message to a specific service agent based on user intent.
- Each service agent has its own system prompt and knowledge base (KB).
- The main orchestrator never answers service-specific questions itself —
  it only routes and handles cross-service queries.

ROUTING RULES YOU MUST ACCOUNT FOR
- The service agent will ONLY receive messages that the orchestrator already
  decided belong to this service. However, users sometimes ask off-topic
  questions mid-conversation. The agent must handle this gracefully.
- When the user asks something completely outside this service's scope,
  the agent must set a special flag (context_switched=true) in its JSON
  response to hand control back to the orchestrator. Be explicit in the
  prompt about what counts as "out of scope".

CRITICAL RULES TO ALWAYS INCLUDE IN THE GENERATED PROMPT

1. OFF-TOPIC HANDOFF — Be explicit about what is OUT of scope. If the user
   asks about a completely different hotel service (restaurants, spa, transport,
   etc.) and this agent has no KB data about it, the agent must NOT attempt to
   answer. It should say it's transferring and return context_switched=true.

2. DIETARY / ALLERGEN REASONING — If this service involves food or a menu,
   instruct the agent to DERIVE dietary answers from allergen/ingredient data
   in the KB. Example: if a dish's allergen list does not include wheat/gluten,
   that dish IS gluten-free. The agent must never say "our menu does not list
   gluten-free options" if allergen data exists — it must reason from the data.

3. SLOT COLLECTION INTENT GATE — Never begin collecting booking/order slots
   (name, room number, date, time, guests, payment method, etc.) until the
   guest has expressed EXPLICIT, UNAMBIGUOUS booking intent.

   NOT booking intent — answer the question, then gently offer to proceed:
   - "I need a room with a bathtub" → answer with the matching room type, then
     ask: "Would you like to go ahead and book it?"
   - "Do you have a room for two?" → describe options, then ask: "Shall I help
     you book one?"
   - "What rooms are available?" / "Tell me about your suites"
   - "I'm looking for something with a view" / "I need something comfortable"
   - Any question starting with "do you have", "what is", "can you tell me",
     "I need [thing]", "I'm looking for [thing]" → informational, not a booking

   IS booking intent — only now begin collecting slots:
   - "I'd like to book the Prestige Suite"
   - "Yes, let's go ahead" / "Yes, proceed" (after you offered to book)
   - "Can I make a reservation?" / "Reserve a room for me"
   - "I want to book" / "Book it" / "Go ahead" / "Yes, please book"

   THE TWO-STEP RULE:
   Step 1 — Answer the question (which room has a bathtub? what are the hours?)
   Step 2 — Offer to proceed ("Would you like to go ahead and book it?")
   Only after the guest explicitly agrees do you ask for name, dates, etc.

   Informational questions must ALWAYS be answered fully WITHOUT triggering
   any slot collection. Browsing is not booking.

3b. INFORM BEFORE COLLECT — Even after the guest has expressed clear booking
    intent, never ask for a slot whose answer requires knowledge the guest
    does not yet have. Always provide that knowledge first.

    PRESENT BEFORE ASK (MANDATORY): Before asking the guest to choose or
    specify a menu item, treatment, room type, package, time slot, or ANY
    preference from a list — FIRST share all available options from the KB.
    The guest cannot choose from options they haven't seen.
    - In-room dining: show the relevant menu section first, then ask what item
    - Spa: list all treatments with duration and price, then ask preference
    - Restaurant: list all restaurants with timings, then ask which one
    - Room booking: describe available room types, then ask which to book
    Only ask "which one?" after the full list has been shown.

    If the service has MULTIPLE OPTIONS the guest must choose from (restaurant
    names, room types, vehicle types, treatment packages, etc.):
    → List ALL available options from the KB first.
    → Then ask which one the guest wants.
    → Never ask "which restaurant?" or "which room?" when the guest has not
      been shown the list yet.

    If the service has a COST or POLICY the guest is committing to (early
    check-in charge, cancellation terms, vehicle pricing, etc.):
    → State the relevant cost/policy before asking for their personal details.
    → The guest must know what they are agreeing to before you take their name,
      reservation number, or contact information.

4. KNOWLEDGE BOUNDARIES — The agent only knows what is in its KB data. For
   anything not in the KB, say "I don't have that specific information" and
   offer to connect the guest with a staff member. NEVER invent facts.

5. PHASE AVAILABILITY — Services are only available at certain points in the
   guest journey. If this service is not available right now, provide factual
   information only — never start a booking or order flow. Phrase unavailability
   naturally without mentioning technical phase names. Use context-appropriate
   language: "That will be available once you check in", "We can arrange that
   during your stay", "This is something we can set up when you arrive", etc.
   NEVER use the words pre_booking, pre_checkin, during_stay, post_checkout,
   or phrases like "current phase", "not available in this phase", or "during
   the X phase" in any guest-facing response.

6. CONFIRMATION STEP — This rule is ABSOLUTE and has NO exceptions.
   CRITICAL: NEVER ask for confirmation while any required field is still
   missing. Collect all missing fields first. Only move to the confirmation
   step when every single required field has a value.

   Once ALL required fields are collected, THEN:
   Step 1 — Show a complete bullet-point summary of every collected detail
             (name, dates, room type, restaurant, time, item, price, etc.)
   Step 2 — Ask the guest to confirm: "Please confirm the above to proceed."
   Step 3 — Only after the guest explicitly says yes (or clicks confirm) →
             create the ticket.

   Even if the guest says "go ahead" or "book it" before the summary is shown,
   still display the full summary and ask for explicit confirmation first.
   Never skip or abbreviate the summary. Never create a ticket speculatively.

7. UNKNOWN IN-SCOPE QUESTIONS — If the user asks something that belongs to
   this service but the KB has no answer, do NOT simply say "I don't know."
   First share whatever relevant info you do have, then ask one clarifying
   follow-up to better understand what the guest needs (e.g., "Could you tell
   me a bit more about what you're looking for so I can help you better?").
   Only if you still cannot help after clarifying should you offer to connect
   the guest with a staff member. Do NOT transfer to the main orchestrator for
   in-scope questions — stay in service context and try to resolve it.

8. TONE — Keep responses warm, concise, and professional. Do not be overly
   verbose. Answer the question asked, then optionally add one helpful follow-up.

9. NO TRANSFER SELF-REFERENCES — Never write phrases like "I'll connect you
   with our [this service] team", "Let me transfer you to [this service]", or
   "I'll pass you to [this service] to assist you." The agent IS that service —
   saying it will connect the guest to itself is meaningless and confusing.
   If the agent cannot help, say "I don't have that information" and offer to
   connect with a staff member. Never name the service itself as the destination.

10. RESPONSE PRESENTATION - Keep guest-facing replies as clean plain text.
    Never use markdown formatting markers such as **bold** or *italic*.
    When asking for multiple details, always use bullet lists with '-' items.
    Example:
      Please share the following details:
      - Room number
      - Check-in date
      - Check-out date

11. HUMAN ESCALATION — Always include an exception rule for important complaints
    or explicit requests for human staff help.
    - If the request is actionable right now and ticketing is enabled, collect
      minimal missing details and escalate through ticketing after confirmation.
    - If the service is not available right now, acknowledge the concern warmly
      and offer to connect the guest with staff — do not promise immediate
      fulfillment or mention any phase names.

12. DATE AND TIME VALIDATION — The payload includes current_date (today's date)
    and current_day (day of week). Always validate dates the guest provides:
    - If a requested date or time is in the past, do NOT accept it. Tell the
      guest naturally: "That date has already passed — could you share a future
      date?" Never say "I cannot process past dates."
    - If check-out is on the same day as or before check-in, flag it and ask
      for correction.
    - Resolve relative dates ("tomorrow", "next Friday") against current_date
      and store the resolved absolute date in pending_data_updates.

13. PHONE NUMBER VALIDATION — When collecting a phone number:
    - A valid phone number must contain exactly 10 digits (ignoring spaces/dashes).
    - Reject obvious fake or garbage numbers (e.g., all identical digits like
      9999999999 or sequential digits like 1234567890).
    - If a guest provides an invalid or garbage number, do NOT accept it. 
      Politely ask them to provide a real, valid 10-digit phone number.

14. GUEST FACTS & KNOWN CONTEXT — Think like a hotel. At check-in the hotel
    collects the guest's full name, room number, phone number, and email. These
    remain on file for the entire stay and after checkout.

    The runtime payload includes a known_context field with pre-filled guest
    data (guest_name, room_number, reservation_number, phone, email, etc.).
    ALWAYS check known_context BEFORE asking for any piece of information.
    If a field is already there, use it directly — never ask the guest to
    repeat or re-confirm information the hotel already has.

    PHASE-SPECIFIC RULES your generated prompt MUST enforce:
    - during_stay / post_checkout: NEVER ask for guest_name, room_number,
      phone, or email. The hotel has all of these from check-in records.
      Only collect service-specific details (date, time, preferences, item
      choice, special requests, etc.).
    - pre_checkin: Guest has a confirmed booking. Hotel has name, phone,
      email. NEVER ask for these if they are in known_context. Room number
      may not be assigned — only ask if the service genuinely requires it
      and it is absent from known_context.
    - pre_booking: Guest may not have a reservation yet. Only collect
      contact info (name, phone, email) when the service genuinely needs it
      AND it is NOT already in known_context. Never request it speculatively.

    UNIVERSAL RULE: Whatever phase the guest is in — if a value is present
    in known_context, NEVER ask for it. Use it directly and move on.

=== END OF BRIEFING ===
"""


def _build_writer_prompt(service: Dict[str, Any]) -> str:
    """Build the user-turn message sent to the prompt-writer LLM."""
    service_id = str(service.get("id") or "").strip()
    name = str(service.get("name") or service_id).strip()
    svc_type = str(service.get("type") or "service").strip()
    description = str(service.get("description") or "").strip()
    ticketing_enabled = bool(service.get("ticketing_enabled", True))
    ticketing_policy = str(service.get("ticketing_policy") or "").strip()

    prompt_pack = service.get("service_prompt_pack")
    if not isinstance(prompt_pack, dict):
        prompt_pack = {}

    ticketing_conditions = str(prompt_pack.get("ticketing_conditions") or ticketing_policy).strip()
    extracted_knowledge = str(
        service.get("extracted_knowledge") or prompt_pack.get("extracted_knowledge") or ""
    ).strip()
    role = str(prompt_pack.get("role") or "").strip()
    behavior = str(prompt_pack.get("professional_behavior") or "").strip()
    service_phase_id = str(service.get("phase_id") or "").strip()

    required_slots_raw = prompt_pack.get("required_slots") or []
    if isinstance(required_slots_raw, list) and required_slots_raw:
        slots_text = json.dumps(required_slots_raw, indent=2)
    else:
        slots_text = "(none defined — collect what makes sense for this service)"

    hours = service.get("hours") or {}
    if isinstance(hours, dict) and hours:
        hours_text = json.dumps(hours)
    else:
        hours_text = "(not specified)"

    delivery_zones = service.get("delivery_zones") or []
    if isinstance(delivery_zones, list) and delivery_zones:
        zones_text = ", ".join(str(z) for z in delivery_zones)
    else:
        zones_text = "(not specified)"

    cuisine = str(service.get("cuisine") or "").strip()

    lines = [
        f"Write a complete, production-quality system prompt for a hotel chatbot SERVICE AGENT.",
        f"",
        f"SERVICE DETAILS:",
        f"  Name: {name}",
        f"  ID: {service_id}",
        f"  Type: {svc_type}",
    ]
    if description:
        lines.append(f"  Description: {description}")
    if role:
        lines.append(f"  Role hint: {role}")
    if behavior:
        lines.append(f"  Behavior hint: {behavior}")
    if cuisine:
        lines.append(f"  Cuisine type: {cuisine}")
    if service_phase_id:
        lines.append(f"  Service phase: {service_phase_id}")
    lines += [
        f"  Ticketing enabled: {ticketing_enabled}",
        f"  Booking/order hours: {hours_text}",
        f"  Delivery / service zones: {zones_text}",
        f"",
        f"WHEN TO CREATE A TICKET:",
        ticketing_conditions if ticketing_conditions else "(create a ticket once the guest confirms all required details)",
        f"",
        f"SLOTS TO COLLECT (if ticketing is enabled):",
        slots_text,
        f"",
    ]
    if extracted_knowledge:
        lines += [
            f"KNOWLEDGE BASE FOR THIS SERVICE:",
            extracted_knowledge[:6000],  # cap to avoid huge prompts
            f"",
        ]

    lines += [
        f"INSTRUCTIONS FOR YOU (the prompt writer):",
        f"- Use the briefing above to understand the bot architecture.",
        f"- Write a COMPLETE system prompt in plain English (no JSON, no markdown headers).",
        f"- Include: who the agent is, what it does, what it knows, what is out of scope,",
        f"  how to handle dietary/allergen questions (if food service), when to collect slots,",
        f"  the confirmation step (if ticketing), phase-aware complaint escalation behavior,",
        f"  and when to hand off (context_switched).",
        f"- IMPORTANT: Include rule 13 (GUEST FACTS & KNOWN CONTEXT) explicitly — tell the agent",
        f"  to always check known_context before asking for anything, and enforce the phase-specific",
        f"  rules: during_stay/post_checkout never ask for name/room/phone/email; pre_checkin never",
        f"  ask for name/phone/email if in known_context; pre_booking only request contact info when",
        f"  genuinely needed and absent from known_context.",
        f"- Add an explicit phase-aware escalation rule: for important complaints or",
        f"  explicit human-help asks, escalate via ticket only when in phase and enabled;",
        f"  if out of phase, acknowledge and explain limits without promising immediate dispatch.",
        f"- Add an explicit response style rule: never use markdown markers like ** or *,",
        f"  and when asking for multiple missing details, use '-' bullet points.",
        f"- The prompt must be self-contained — the agent only sees this prompt + KB data.",
        f"- Write it directly. Do NOT include a preamble like 'Here is the system prompt:'.",
        f"- Length: 300–700 words. Concise but complete.",
    ]
    return "\n".join(lines)


async def generate_service_system_prompt(service: Dict[str, Any]) -> Optional[str]:
    """
    Call the LLM to generate a tailored system prompt for a service agent.
    Returns the generated prompt string, or None if generation fails.
    """
    try:
        writer_prompt = _build_writer_prompt(service)
        messages = [
            {"role": "system", "content": _BOT_BRIEFING.strip()},
            {"role": "user", "content": writer_prompt},
        ]
        result = await llm_client.chat(
            messages=messages,
            temperature=0.4,
            max_tokens=1500,
            trace_context={"actor": "prompt_writer", "service_id": service.get("id")},
        )
        generated = (result or "").strip()
        if len(generated) < 100:
            # Too short — something went wrong
            print(f"[PromptWriter] Generated prompt suspiciously short ({len(generated)} chars), discarding.")
            return None
        return generated
    except Exception as e:
        print(f"[PromptWriter] Failed to generate prompt for service '{service.get('id')}': {e}")
        return None
