"""
LLM Client - OpenAI Integration

Handles all LLM calls with proper error handling and logging.
"""

import json
from typing import Optional
from openai import AsyncOpenAI

from config.settings import settings
from services.config_service import config_service


class LLMClient:
    """Async OpenAI client wrapper."""

    def __init__(self):
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.model = settings.openai_model
        self.max_tokens = settings.llm_max_tokens
        self.temperature = settings.llm_temperature

    @staticmethod
    def _render_admin_prompt(template: str, context: dict) -> str:
        """Render simple placeholders from admin-authored prompt text."""
        if not template:
            return ""

        rendered = template
        replacements = {
            "{bot_name}": str(context.get("bot_name", "Assistant")),
            "{business_name}": str(context.get("hotel_name", context.get("hotel_code", "Business"))),
            "{hotel_name}": str(context.get("hotel_name", context.get("hotel_code", "Business"))),
            "{city}": str(context.get("city", "")),
            "{business_type}": str(context.get("business_type", "generic")),
        }
        for token, value in replacements.items():
            rendered = rendered.replace(token, value)
        return rendered

    @staticmethod
    def _list_or_fallback(items: list, fallback: str) -> str:
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        if not cleaned:
            return f"- {fallback}"
        return "\n".join(f"- {item}" for item in cleaned)

    async def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Send chat completion request to OpenAI.

        Args:
            messages: List of message dicts with 'role' and 'content'
            model: Override default model
            temperature: Override default temperature
            max_tokens: Override default max tokens

        Returns:
            Assistant's response text
        """
        try:
            response = await self.client.chat.completions.create(
                model=model or self.model,
                messages=messages,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=max_tokens or self.max_tokens,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            print(f"LLM Chat Error: {e}")
            return "I'm having trouble processing that right now. Could you please try again?"

    async def chat_with_json(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> dict:
        """
        Send chat request expecting JSON response.

        Returns:
            Parsed JSON dict
        """
        try:
            response = await self.client.chat.completions.create(
                model=model or self.model,
                messages=messages,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=self.max_tokens,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            return json.loads(content)
        except json.JSONDecodeError as e:
            print(f"JSON Parse Error: {e}")
            return {"intent": "unclear", "confidence": 0.3, "entities": {}}
        except Exception as e:
            print(f"LLM Error: {e}")
            return {"intent": "unclear", "confidence": 0.3, "entities": {}}

    async def classify_intent(
        self,
        user_message: str,
        conversation_history: list[dict],
        context: dict,
    ) -> dict:
        """
        Classify user intent with confidence score.

        Returns:
            {
                "intent": str,
                "confidence": float,
                "entities": dict,
                "reasoning": str
            }
        """
        prompt_config = config_service.get_prompts()
        nlu_policy = config_service.get_nlu_policy()
        classifier_prompt = str(prompt_config.get("classifier_prompt", "")).strip()
        nlu_dos = self._list_or_fallback(nlu_policy.get("dos", []), "Classify based on the closest supported workflow.")
        nlu_donts = self._list_or_fallback(nlu_policy.get("donts", []), "Do not fabricate unsupported workflows.")
        intent_catalog = context.get("intent_catalog") or context.get("capabilities", {}).get("intents", [])
        service_catalog = context.get("service_catalog") or context.get("capabilities", {}).get("service_catalog", [])
        faq_bank = context.get("faq_bank") or context.get("capabilities", {}).get("faq_bank", [])
        tools = context.get("tools") or context.get("capabilities", {}).get("tools", [])
        memory_summary = str(context.get("conversation_summary", "")).strip()
        memory_facts = context.get("memory_facts", {})
        if not isinstance(memory_facts, dict):
            memory_facts = {}
        memory_recent_changes = context.get("memory_recent_changes", [])
        if not isinstance(memory_recent_changes, list):
            memory_recent_changes = []

        intent_lines = []
        for intent_cfg in intent_catalog:
            if not isinstance(intent_cfg, dict):
                continue
            intent_id = str(intent_cfg.get("id", "")).strip()
            if not intent_id:
                continue
            label = str(intent_cfg.get("label") or intent_id).strip()
            enabled = bool(intent_cfg.get("enabled", True))
            maps_to = str(intent_cfg.get("maps_to") or "").strip()
            mapping_text = f", maps_to={maps_to}" if maps_to else ""
            intent_lines.append(f"- {intent_id} ({label}) enabled={enabled}{mapping_text}")
        intent_catalog_str = "\n".join(intent_lines) if intent_lines else "- No custom intent catalog configured."

        service_lines = []
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            service_id = str(service.get("id", "")).strip()
            name = str(service.get("name", "")).strip()
            if not service_id and not name:
                continue
            service_type = str(service.get("type", "service")).strip()
            description = str(service.get("description") or service.get("cuisine") or "").strip()
            active = bool(service.get("is_active", True))
            status = "active" if active else "inactive"
            label = f"{name} [{service_id}]" if name and service_id else (name or service_id)
            detail = f": {description}" if description else ""
            service_lines.append(f"- {label} ({service_type}, {status}){detail}")
        service_catalog_str = "\n".join(service_lines) if service_lines else "- No service catalog configured."

        faq_lines = []
        for faq in faq_bank:
            if not isinstance(faq, dict):
                continue
            faq_id = str(faq.get("id") or "").strip()
            question = str(faq.get("question") or "").strip()
            if not faq_id and not question:
                continue
            status = "enabled" if faq.get("enabled", True) else "disabled"
            detail = f"{question}" if question else faq_id
            faq_lines.append(f"- {detail} [{faq_id or 'no_id'}] ({status})")
        faq_bank_str = "\n".join(faq_lines) if faq_lines else "- No FAQ bank entries configured."

        tool_lines = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_id = str(tool.get("id") or "").strip()
            name = str(tool.get("name") or "").strip()
            if not tool_id and not name:
                continue
            status = "enabled" if tool.get("enabled", True) else "disabled"
            tool_type = str(tool.get("type") or "workflow").strip()
            label = f"{name} [{tool_id}]" if name and tool_id else (name or tool_id)
            tool_lines.append(f"- {label} ({tool_type}, {status})")
        tools_str = "\n".join(tool_lines) if tool_lines else "- No tools configured."

        system_prompt = """You are an intent classifier for a configurable business chatbot. Analyze the user message and classify it.

AVAILABLE INTENTS:
- greeting: Hello, hi, good morning, etc.
- menu_request: Asking to see menus/offers/catalog options
- order_food: Food ordering (if the business supports it)
- order_status: Checking order/request status
- table_booking: Reserve table/slot/appointment style booking
- room_service: Service request intent (housekeeping/amenities/support tasks)
- health_support: Medication or medical-assistance requests needing safe human handoff
- complaint: Issues, problems, not working, bad experience
- faq: General business information and FAQs
- confirmation_yes: Yes, confirm, proceed, ok, sure
- confirmation_no: No, cancel, don't want, stop
- human_request: Want to talk to human, agent, manager, real person
- unclear: Can't determine intent
- out_of_scope: Request outside business services

CONTEXT:
- Business: {hotel_name}
- Business Type: {business_type}
- Guest: {guest_name}
- Current State: {state}
- Pending Action: {pending_action}
- Enabled Intents: {enabled_intents}

LONG-TERM MEMORY SUMMARY:
{conversation_summary}

MEMORY FACTS (LATEST VALUES):
{memory_facts}

RECENT FACT CHANGES:
{memory_recent_changes}

ADMIN INTENT CATALOG:
{intent_catalog}

ADMIN SERVICE CATALOG:
{service_catalog}

ADMIN FAQ BANK:
{faq_bank}

ADMIN TOOLS:
{tools}

ADMIN CLASSIFIER INSTRUCTIONS:
{classifier_prompt}

NLU POLICY DOS:
{nlu_dos}

NLU POLICY DONTS:
{nlu_donts}

CONVERSATION HISTORY:
{history}

Respond in JSON format:
{{
    "intent": "intent_name",
    "confidence": 0.0-1.0,
    "entities": {{"items": ["item1", "item2"], "restaurant": "restaurant name", "party_size": "2", "time": "7 PM", "date": "today"}},
    "reasoning": "brief explanation"
}}

ENTITY EXTRACTION RULES:
- For order_food: ALWAYS extract food item names into "items" as a LIST, e.g. {{"items": ["margherita pizza", "coke"]}}
- For table_booking: extract "restaurant", "party_size", "time", "date" if mentioned
- For menu_request: extract "restaurant" if a specific restaurant is mentioned
- For health_support: extract "urgency" (emergency/non_emergency) when possible
- Only include entities that are actually mentioned in the message

IMPORTANT INTENT ENABLEMENT RULE:
- If a specific workflow intent appears disabled in Enabled Intents, avoid over-committing.
- Prefer "faq", "human_request", or "unclear" with lower confidence instead.
- Return a CORE intent from AVAILABLE INTENTS only.
- If a custom/admin intent matches better, include it in entities.custom_intent and map to nearest core intent.
- If a user message strongly matches an enabled FAQ bank question, prefer "faq"."""

        # Build history string
        history_str = "\n".join([
            f"{msg['role'].upper()}: {msg['content']}"
            for msg in conversation_history[-5:]
        ]) or "No previous messages"

        formatted_prompt = system_prompt.format(
            hotel_name=context.get("hotel_name", context.get("hotel_code", "Hotel")),
            business_type=context.get("business_type", "generic"),
            guest_name=context.get("guest_name", "Guest"),
            state=context.get("state", "idle"),
            pending_action=context.get("pending_action", "None"),
            enabled_intents=", ".join(context.get("enabled_intents", [])) or "not provided",
            conversation_summary=memory_summary or "No long-term summary yet.",
            memory_facts=json.dumps(memory_facts, ensure_ascii=False),
            memory_recent_changes=json.dumps(memory_recent_changes[-5:], ensure_ascii=False),
            intent_catalog=intent_catalog_str,
            service_catalog=service_catalog_str,
            faq_bank=faq_bank_str,
            tools=tools_str,
            classifier_prompt=classifier_prompt or "None configured.",
            nlu_dos=nlu_dos,
            nlu_donts=nlu_donts,
            history=history_str,
        )

        messages = [
            {"role": "system", "content": formatted_prompt},
            {"role": "user", "content": user_message},
        ]

        return await self.chat_with_json(messages, temperature=0.1)

    async def generate_response(
        self,
        user_message: str,
        intent: str,
        entities: dict,
        conversation_history: list[dict],
        context: dict,
    ) -> str:
        """
        Generate contextual response based on intent.

        Returns:
            Response text for the user
        """
        # Extract capabilities from context
        capabilities = context.get("capabilities", {})
        capability_flags = capabilities.get("services", {})
        capability_configs = capabilities.get("capabilities", {})
        service_catalog = capabilities.get("service_catalog", [])
        faq_bank = capabilities.get("faq_bank", [])
        tools = capabilities.get("tools", [])
        business_type = context.get("business_type", "generic")

        # Build dynamic capability list
        caps_list = []
        for capability_id, capability_data in capability_configs.items():
            if not isinstance(capability_data, dict):
                continue
            if not capability_data.get("enabled", False):
                continue
            label = str(capability_id).replace("_", " ").title()
            description = str(capability_data.get("description") or "").strip()
            hours = str(capability_data.get("hours") or "").strip()
            extras = [part for part in (description, f"hours: {hours}" if hours else "") if part]
            suffix = f" ({'; '.join(extras)})" if extras else ""
            caps_list.append(f"- {label}: Available{suffix}")

        if not caps_list:
            for capability_id, enabled in capability_flags.items():
                if capability_id.endswith("_hours") or not enabled:
                    continue
                label = str(capability_id).replace("_", " ").title()
                hours = str(capability_flags.get(f"{capability_id}_hours") or "").strip()
                suffix = f" (hours: {hours})" if hours else ""
                caps_list.append(f"- {label}: Available{suffix}")

        caps_str = "\n".join(caps_list) if caps_list else "No enabled capabilities configured."

        # Build dynamic service catalog list
        service_lines = []
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            service_name = str(service.get("name") or "").strip()
            service_id = str(service.get("id") or "").strip()
            if not service_name and not service_id:
                continue
            service_type = str(service.get("type") or "service").strip()
            description = str(service.get("description") or service.get("cuisine") or "").strip()
            is_active = bool(service.get("is_active", True))
            status = "active" if is_active else "inactive"

            hours_value = service.get("hours")
            if isinstance(hours_value, dict):
                open_time = str(hours_value.get("open") or "").strip()
                close_time = str(hours_value.get("close") or "").strip()
                hours_text = f"{open_time}-{close_time}" if (open_time or close_time) else "not specified"
            else:
                hours_text = str(hours_value or "not specified").strip()

            delivery_zones = service.get("delivery_zones") or []
            delivery_text = ", ".join(str(zone) for zone in delivery_zones) if delivery_zones else "unspecified"
            heading = f"{service_name} [{service_id}]" if service_name and service_id else (service_name or service_id)
            detail_parts = [f"type={service_type}", f"status={status}", f"hours={hours_text}", f"zones={delivery_text}"]
            if description:
                detail_parts.append(f"description={description}")
            service_lines.append(f"- {heading}: " + ", ".join(detail_parts))

        services_str = "\n".join(service_lines) if service_lines else "No services configured."

        faq_lines = []
        for faq in faq_bank:
            if not isinstance(faq, dict):
                continue
            if not faq.get("enabled", True):
                continue
            question = str(faq.get("question") or "").strip()
            answer = str(faq.get("answer") or "").strip()
            faq_id = str(faq.get("id") or "").strip()
            if not question or not answer:
                continue
            faq_lines.append(f"- [{faq_id}] Q: {question} | A: {answer}")
        faq_bank_str = "\n".join(faq_lines) if faq_lines else "No enabled FAQ bank entries."

        tool_lines = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            tool_id = str(tool.get("id") or "").strip()
            if not name and not tool_id:
                continue
            label = f"{name} [{tool_id}]" if name and tool_id else (name or tool_id)
            status = "enabled" if tool.get("enabled", True) else "disabled"
            tool_type = str(tool.get("type") or "workflow").strip()
            description = str(tool.get("description") or "").strip()
            suffix = f": {description}" if description else ""
            tool_lines.append(f"- {label} ({tool_type}, {status}){suffix}")
        tools_str = "\n".join(tool_lines) if tool_lines else "No tools configured."

        prompt_config = config_service.get_prompts()
        nlu_policy = config_service.get_nlu_policy()
        admin_system_prompt = self._render_admin_prompt(
            str(prompt_config.get("system_prompt", "")).strip(),
            context,
        )
        response_style = str(prompt_config.get("response_style", "")).strip()
        nlu_dos = self._list_or_fallback(nlu_policy.get("dos", []), "Stay aligned with configured capabilities.")
        nlu_donts = self._list_or_fallback(nlu_policy.get("donts", []), "Do not promise unavailable actions.")
        memory_summary = str(context.get("conversation_summary", "")).strip()
        memory_facts = context.get("memory_facts", {})
        if not isinstance(memory_facts, dict):
            memory_facts = {}
        memory_recent_changes = context.get("memory_recent_changes", [])
        if not isinstance(memory_recent_changes, list):
            memory_recent_changes = []

        system_prompt = """You are a friendly AI assistant named {bot_name} for {hotel_name} in {city}.
Business type: {business_type}

ADMIN SYSTEM PROMPT (HIGHEST PRIORITY):
{admin_system_prompt}

IMPORTANT - ACTUAL BUSINESS CAPABILITIES (do NOT promise anything outside this list):
{capabilities_str}

SERVICE CATALOG (if applicable):
{services_str}

ADMIN FAQ BANK (authoritative Q/A pairs):
{faq_bank}

ADMIN TOOLS:
{tools}

STRICT RULES:
- ONLY offer actions for capabilities marked Available and services marked status=active
- Do not promise unsupported workflows
- If a delivery/catalog outlet is marked "dine-in only", do not offer delivery from it
- If unsure about availability, offer to connect with staff

NLU POLICY DOS:
{nlu_dos}

NLU POLICY DONTS:
{nlu_donts}

CURRENT CONTEXT:
- User: {guest_name}
- Room: {room_number}
- Conversation State: {state}
- Pending Action: {pending_action}

LONG-TERM MEMORY SUMMARY:
{conversation_summary}

MEMORY FACTS (LATEST VALUES):
{memory_facts}

RECENT FACT CHANGES:
{memory_recent_changes}

DETECTED INTENT: {intent}
EXTRACTED ENTITIES: {entities}

CONVERSATION HISTORY:
{history}

RESPONSE GUIDELINES:
1. Be helpful, friendly, and concise
2. If confirming an action, list details clearly and ask for confirmation
3. If state is "awaiting_confirmation", respect the pending action
4. NEVER promise something not in the capabilities list above
5. If unsure, offer to connect with staff
6. Keep responses under 150 words
7. Response style preference: {response_style}

Respond naturally to the user's message."""

        # Build history string
        history_str = "\n".join([
            f"{msg['role'].upper()}: {msg['content']}"
            for msg in conversation_history[-6:]
        ]) or "No previous messages"

        # Get bot_name from capabilities (synced from admin portal config)
        bot_name = capabilities.get("bot_name", "Assistant")

        formatted_prompt = system_prompt.format(
            bot_name=bot_name,
            hotel_name=context.get("hotel_name", context.get("hotel_code", "Hotel")).replace("_", " "),
            business_type=business_type,
            city=context.get("city", ""),
            admin_system_prompt=admin_system_prompt or "No custom system prompt configured.",
            capabilities_str=caps_str,
            services_str=services_str,
            faq_bank=faq_bank_str,
            tools=tools_str,
            nlu_dos=nlu_dos,
            nlu_donts=nlu_donts,
            guest_name=context.get("guest_name", "Guest"),
            room_number=context.get("room_number", "Not specified"),
            state=context.get("state", "idle"),
            pending_action=context.get("pending_action", "None"),
            conversation_summary=memory_summary or "No long-term summary yet.",
            memory_facts=json.dumps(memory_facts, ensure_ascii=False),
            memory_recent_changes=json.dumps(memory_recent_changes[-5:], ensure_ascii=False),
            intent=intent,
            entities=json.dumps(entities),
            response_style=response_style or "Default",
            history=history_str,
        )

        messages = [
            {"role": "system", "content": formatted_prompt},
            {"role": "user", "content": user_message},
        ]

        return await self.chat(messages, temperature=0.7)


# Global instance
llm_client = LLMClient()
