---
key: service_writer.multi_property_rule
description: Writer-prompt block injected when a service KB contains multiple property/location sections
variables: []
---
MULTI-PROPERTY RULE:
  The knowledge base contains separate property/location sections.
  If conversation context already makes one property clear, use ONLY that property section
  plus any shared/common knowledge.
  If the property is unclear and the guest needs property-specific details or wants to proceed
  with a booking/request flow, ask a short clarification question first.
  Never mix details from one property section into another property's answer.
  Never continue transactional execution until the property is clear.
