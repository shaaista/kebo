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

6. CONFIRMATION STEP — This rule depends on the ticketing mode.

   TEXT-BASED TICKETING (ticketing_mode=text):
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

   FORM-BASED TICKETING (ticketing_mode=form):
   This service uses an inline form UI. The flow is DIFFERENT from text mode:
   Step 1 — Present options from the KB, let the guest browse and choose.
   Step 2 — Once the guest picks an option, give a warm 1-2 sentence
            confirmation highlighting key features, then say something like
            "Please fill in the booking details below."
   Step 3 — The system shows the form automatically. The form collects the
            remaining details (name, dates, phone, etc.). The form submission
            itself IS the confirmation — the ticket is created from form data.

   CRITICAL FOR FORM MODE:
   - NEVER ask the guest to say "yes confirm" or any confirmation phrase.
   - NEVER ask for form fields (name, phone, dates, time, etc.) in
     conversation — the form handles them.
   - NEVER create the ticket yourself — the form submission does it.
   - After confirming the guest's choice, immediately signal readiness for
     the form. Do NOT add an extra "Shall I proceed?" step.

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
   CRITICAL: Never use technical/internal jargon in guest-facing responses.
   Banned words: "escalate", "ticket", "reference number", "ticket ID",
   "create a ticket", "automated ticketing", "system issue", "logged your complaint".
   Use instead: "I have noted this", "our team will look into this",
   "I will make sure someone helps you", "let me connect you with our team".
   For complaints: always empathize first, then offer to help.
   For requests to speak with staff: provide contact info or say someone will
   reach out — never use internal process language.

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

13. PHONE NUMBER VALIDATION — Phone numbers are collected via a form with a
    country code dropdown. The submitted value will include the country code
    prefix (e.g., +91, +1, +44). Validate using your knowledge of each
    country's telecom rules:
    - India (+91): exactly 10 digits after code, first digit must be 6-9.
    - US/Canada (+1): exactly 10 digits, area code cannot start with 0 or 1.
    - UK (+44): 10-11 digits after code.
    - UAE (+971): exactly 9 digits after code.
    - For other countries, use your knowledge of their phone number format.
    - Reject obviously fake/placeholder numbers: all-same digits (9999999999),
      sequential (1234567890, 9876543210), all-zeros, or any pattern that no
      real person would have. Think like a hotel receptionist.
    - If invalid, politely ask the guest to provide a real phone number.

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
    # --- ticketing_mode & form_config ---
    ticketing_mode = str(service.get("ticketing_mode") or "").strip().lower()
    form_config = service.get("form_config")
    if not ticketing_mode:
        ticketing_mode = "text" if ticketing_enabled else "none"

    lines += [
        f"  Ticketing enabled: {ticketing_enabled}",
        f"  Ticketing mode: {ticketing_mode}",
        f"  Booking/order hours: {hours_text}",
        f"  Delivery / service zones: {zones_text}",
        f"",
        f"WHEN TO CREATE A TICKET:",
        ticketing_conditions if ticketing_conditions else "(create a ticket once the guest confirms all required details)",
        f"",
    ]

    # Inject form-based ticketing instructions if mode=form
    if ticketing_mode == "form" and isinstance(form_config, dict):
        trigger_field = form_config.get("trigger_field") or {}
        form_fields = form_config.get("fields") or []
        pre_form = str(form_config.get("pre_form_instructions") or "").strip()
        trigger_id = str(trigger_field.get("id") or "").strip()
        trigger_label = str(trigger_field.get("label") or trigger_id).strip()
        trigger_desc = str(trigger_field.get("description") or "").strip()

        lines += [
            f"FORM-BASED TICKETING FLOW:",
            f"  This service uses an inline form to collect guest details.",
            f"  The form UI will appear automatically once the agent signals readiness.",
            f"  The agent must NEVER ask for form fields via conversation.",
            f"  The agent must NEVER ask the guest to say 'yes confirm' or any confirmation phrase.",
            f"  The form submission itself IS the confirmation — no text-based confirmation step needed.",
            f"",
        ]
        if trigger_id:
            lines += [
                f"  TRIGGER FIELD: {trigger_label} (id: {trigger_id})",
                f"  Trigger description: {trigger_desc}" if trigger_desc else "",
                f"  The agent MUST first present the available options from the knowledge base,",
                f"  let the guest choose a SPECIFIC option, and then confirm + trigger the form.",
                f"  There is NO 'Shall I proceed?' step — once the guest picks a specific option, confirm and show the form.",
                f"",
                f"  HOW TO TRIGGER THE FORM (the generated prompt must teach the agent this):",
                f"  When the guest selects a SPECIFIC option (names it, points to it like 'the first one',",
                f"  'Terminal 1', 'candlelight therapy', or clearly indicates which one):",
                f"    1. Give a warm 1-2 sentence confirmation highlighting key features of their choice.",
                f"    2. End with 'Please fill in the booking details below.' (or similar).",
                f"    3. Set action='collect_info', pending_action='collect_form_details',",
                f"       pending_data_updates must include '{trigger_id}' with the guest's chosen value,",
                f"       and missing_fields must be empty.",
                f"  If the guest provides the trigger value upfront (e.g. 'book candlelight therapy'),",
                f"  skip browsing and confirm + trigger the form immediately.",
                f"",
                f"  CRITICAL — INTENT vs SELECTION (use conversation context):",
                f"  The trigger field '{trigger_id}' must have a SPECIFIC value before triggering the form.",
                f"  Use the conversation history to understand what a short reply like 'yes' refers to:",
                f"  - If you asked 'Would you like to book?' → 'yes' = intent only, ask which {trigger_id}.",
                f"  - If you asked 'Shall I proceed with Terminal 1?' → 'yes' = selection of Terminal 1.",
                f"  - If user names the option directly → that is the selection.",
                f"  The trigger value must ALWAYS be the actual option name (e.g. 'Terminal 1',",
                f"  'Prestige Suite'), NEVER a confirmation word like 'yes' or 'ok'.",
                f"  Derive the real value from what was discussed in the conversation.",
                f"",
                f"  RULE FOR CONFIRMATION MESSAGES:",
                f"  When the guest selects an option, give a brief warm confirmation that mentions",
                f"  1-2 key highlights of their choice naturally woven into a complete sentence, then let them",
                f"  know the booking form will appear.",
                f"  GOOD: 'Great choice! The Prestige Suite at 485 sq. ft. with a lavish king-size bed and",
                f"  elegant bathtub is perfect for a luxurious stay. Please fill in the booking details below.'",
                f"  BAD (NEVER do these):",
                f"    - 'The Prestige Suite, which includes:' (trailing colon with nothing after)",
                f"    - 'which features:. Please fill in...' (colon then period)",
                f"    - Listing ALL features as bullet points — just mention 1-2 highlights naturally",
                f"    - 'Reply yes confirm to proceed' — NEVER ask for a confirmation phrase",
                f"    - 'Shall I proceed with the booking?' — NO extra confirmation step for form mode",
                f"  Keep it to 2-3 sentences max. Always end with a complete sentence.",
                f"",
            ]
        if pre_form:
            lines += [
                f"  PRE-FORM INSTRUCTIONS: {pre_form}",
                f"",
            ]
        if form_fields:
            field_names = ", ".join(f.get("label", f.get("id", "?")) for f in form_fields)
            lines.append(f"  Form will collect: {field_names}")
            lines.append(f"  Do NOT ask the guest for these fields — the form handles it.")
            lines.append(f"")
    else:
        lines += [
            f"SLOTS TO COLLECT (if ticketing is enabled):",
            slots_text,
            f"",
        ]
        if ticketing_mode == "text" and ticketing_enabled:
            lines += [
                f"TEXT-BASED TICKETING: Before collecting any details, first confirm with the guest",
                f"what specific option/variant they want (e.g., room type, treatment type).",
                f"Only after they confirm their choice, proceed to collect the remaining details via conversation.",
                f"",
            ]
    if extracted_knowledge:
        lines += [
            f"KNOWLEDGE BASE FOR THIS SERVICE:",
            extracted_knowledge,  # no truncation — per-file extraction keeps each property small
            f"",
        ]
        if "=== PROPERTY:" in extracted_knowledge:
            lines += [
                f"MULTI-PROPERTY RULE:",
                f"  The knowledge base contains separate property/location sections.",
                f"  If conversation context already makes one property clear, use ONLY that property section",
                f"  plus any shared/common knowledge.",
                f"  If the property is unclear and the guest needs property-specific details or wants to proceed",
                f"  with a booking/request flow, ask a short clarification question first.",
                f"  Never mix details from one property section into another property's answer.",
                f"  Never continue transactional execution until the property is clear.",
                f"",
            ]

    lines += [
        f"INSTRUCTIONS FOR YOU (the prompt writer):",
        f"- Use the briefing above to understand the bot architecture.",
        f"- Write a COMPLETE system prompt in plain English (no JSON, no markdown headers).",
        f"- Include: who the agent is, what it does, what it knows, what is out of scope,",
        f"  how to handle dietary/allergen questions (if food service), when to collect slots,",
        f"  the confirmation/ticketing step (matching the ticketing mode: form or text),",
        f"  phase-aware complaint escalation behavior, and when to hand off (context_switched).",
        f"- TICKETING MODE RULE: The ticketing mode for this service is '{ticketing_mode}'.",
        f"  If form-based: the generated prompt MUST tell the agent to NEVER ask for 'yes confirm'",
        f"  or any confirmation phrase, NEVER collect form fields in conversation, and to immediately",
        f"  trigger the form after confirming the guest's trigger choice. The form submission is the",
        f"  confirmation. If text-based: include the standard confirmation step with summary + explicit confirm.",
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
        f"- CRITICAL TONE RULE: The generated prompt MUST instruct the agent to NEVER use",
        f"  technical/internal jargon in guest-facing responses. Specifically:",
        f"  - NEVER say: 'escalate', 'ticket', 'reference number', 'ticket ID', 'logged your complaint',",
        f"    'create a ticket', 'automated ticketing'",
        f"  - INSTEAD say: 'I have noted this', 'our team will look into this', 'I will make sure",
        f"    someone helps you with this', 'let me connect you with our team'",
        f"  - For complaints: ALWAYS empathize first ('I am sorry to hear that'), then offer help.",
        f"    Never jump straight to collecting details after a complaint.",
        f"  - For requests to speak with staff: provide contact information when available,",
        f"    do not create backend processes unless the guest explicitly asks for follow-up.",
        f"- The prompt must be self-contained — the agent only sees this prompt + KB data.",
        f"- Write it directly. Do NOT include a preamble like 'Here is the system prompt:'.",
        f"- Length: 300–700 words. Concise but complete.",
        f"",
        f"- FOOD & DINING MULTI-ITEM ORDERING — Apply this ONLY if the service involves food",
        f"  ordering, in-room dining, restaurant ordering, or any menu-based ordering. You can",
        f"  determine this from the service type, cuisine field, description, or KB content",
        f"  containing menus/dishes/food items. If this is NOT a food service, skip this entirely.",
        f"  When it IS a food/dining service, the generated prompt MUST include these rules:",
        f"    1. When the guest mentions a dish or item, first verify it exists in the KB menu.",
        f"       If it does not exist, politely inform the guest and suggest similar items from the menu.",
        f"    2. After acknowledging each item, ALWAYS ask 'Would you like anything else?' or",
        f"       'Is there anything else you would like to add?' — do NOT rush to the form or",
        f"       confirmation after just one item. Guests often order multiple items.",
        f"    3. Keep a running tally of what has been ordered so far and mention it naturally",
        f"       (e.g. 'So far I have 1x Margherita Pizza. Anything else?').",
        f"    4. Only proceed to the form/confirmation step when the guest explicitly signals",
        f"       they are done ordering (e.g. 'that is all', 'nothing else', 'no thanks',",
        f"       'just that', 'I am done', or similar). A single item mention is NOT a signal",
        f"       to finalize — always ask if there is more.",
        f"    5. For form-mode: the trigger value should be the full order summary",
        f"       (e.g. '2x Margherita Pizza, 1x Caesar Salad, 1x Mango Smoothie').",
        f"       For text-mode: collect remaining details only after the order is finalized.",
        f"    6. If the guest asks about ingredients, allergens, or dietary suitability of a",
        f"       specific dish, answer from the KB data before adding it to the order.",
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
