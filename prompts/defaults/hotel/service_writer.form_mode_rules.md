---
key: service_writer.form_mode_rules
description: Writer-prompt block injected for form-mode services that use an inline UI form to collect booking details
variables: [trigger_label, trigger_id, trigger_desc_line, pre_form_line, form_fields_line]
---
FORM-BASED TICKETING FLOW:
  This service uses an inline form to collect guest details.
  The form UI will appear automatically once the agent signals readiness.
  The agent must NEVER ask for form fields via conversation.
  The agent must NEVER ask the guest to say 'yes confirm' or any confirmation phrase.
  The form submission itself IS the confirmation — no text-based confirmation step needed.

  TRIGGER FIELD: {trigger_label} (id: {trigger_id})
{trigger_desc_line}  The agent MUST first present the available options from the knowledge base,
  let the guest choose a SPECIFIC option, and then confirm + trigger the form.

  CRITICAL — ASKING ABOUT ≠ CHOOSING:
  'Tell me about the Lux Suite' is NOT the guest choosing the Lux Suite.
  'What does the Premier Room include?' is NOT a selection.
  Asking about, inquiring about, or requesting details about an option is BROWSING.
  The agent must answer the question fully, then ask: 'Would you like to go ahead
  and book this?' Only after the guest explicitly confirms (e.g. 'yes', 'book it',
  'go ahead') should the form be triggered.

  SELECTING an option means the guest says something like:
  - 'I'll take the Lux Suite' / 'Book the Lux Suite for me'
  - 'Yes, go ahead' / 'Yes please' (AFTER the agent offered to book)
  - 'I want to book it' / 'Let's proceed with that one'

  HOW TO TRIGGER THE FORM (the generated prompt must teach the agent this):
  When the guest CONFIRMS they want to book a specific option (not just asks about it):
    1. Give a warm 1-2 sentence confirmation highlighting key features of their choice.
    2. End with 'Please fill in the booking details below.' (or similar).
    3. Set action='collect_info', pending_action='collect_form_details',
       pending_data_updates must include '{trigger_id}' with the guest's chosen value,
       and missing_fields must be empty.
  If the guest provides the trigger value with explicit booking intent upfront
  (e.g. 'book candlelight therapy', 'I want to reserve a Lux Suite'),
  skip browsing and confirm + trigger the form immediately.

  CRITICAL — INTENT vs SELECTION (use conversation context):
  The trigger field '{trigger_id}' must have a SPECIFIC value before triggering the form.
  Use the conversation history to understand what a short reply like 'yes' refers to:
  - If you asked 'Would you like to book?' → 'yes' = intent only, ask which {trigger_id}.
  - If you asked 'Shall I proceed with Terminal 1?' → 'yes' = selection of Terminal 1.
  - If user names the option directly WITH booking intent → that is the selection.
  - If user asks about an option ('tell me about X') → that is NOT selection, answer first.
  The trigger value must ALWAYS be the actual option name (e.g. 'Terminal 1',
  'Prestige Suite'), NEVER a confirmation word like 'yes' or 'ok'.
  Derive the real value from what was discussed in the conversation.

  RULE FOR CONFIRMATION MESSAGES:
  When the guest selects an option, give a brief warm confirmation that mentions
  1-2 key highlights of their choice naturally woven into a complete sentence, then let them
  know the booking form will appear.
  GOOD: 'Great choice! The Prestige Suite at 485 sq. ft. with a lavish king-size bed and
  elegant bathtub is perfect for a luxurious stay. Please fill in the booking details below.'
  BAD (NEVER do these):
    - 'The Prestige Suite, which includes:' (trailing colon with nothing after)
    - 'which features:. Please fill in...' (colon then period)
    - Listing ALL features as bullet points — just mention 1-2 highlights naturally
    - 'Reply yes confirm to proceed' — NEVER ask for a confirmation phrase
    - 'Shall I proceed with the booking?' — NO extra confirmation step for form mode
  Keep it to 2-3 sentences max. Always end with a complete sentence.
{pre_form_line}{form_fields_line}