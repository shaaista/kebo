// Chat Application State
const state = {
    sessionId: null,
    hotelCode: 'DEFAULT',
    phase: 'pre_booking',
    messages: [],
    isLoading: false,
    formFocusMode: false,
    suggestionRequestId: 0,
    testProfileAutoApply: true,
    testProfilesByPhase: {},
    activeTestProfile: null,
    // Booking context
    bookings: [],
    selectedBooking: null,
};

// DOM Elements
const elements = {
    chatMessages: document.getElementById('chat-messages'),
    chatForm: document.getElementById('chat-form'),
    messageInput: document.getElementById('message-input'),
    sendBtn: document.getElementById('send-btn'),
    sessionId: document.getElementById('session-id'),
    sessionState: document.getElementById('session-state'),
    ticketStatus: document.getElementById('ticket-status'),
    ticketDetailsWrap: document.getElementById('ticket-details-wrap'),
    ticketDetailsContent: document.getElementById('ticket-details-content'),
    messageCount: document.getElementById('message-count'),
    suggestedActions: document.getElementById('suggested-actions'),
    newSessionBtn: document.getElementById('new-session-btn'),
    resetBtn: document.getElementById('reset-btn'),
    viewHistoryBtn: document.getElementById('view-history-btn'),
    hotelCode: document.getElementById('hotel-code'),
    journeyPhase: document.getElementById('journey-phase'),
    autoPhaseProfile: document.getElementById('auto-phase-profile'),
    phaseProfileStatus: document.getElementById('phase-profile-status'),
    phaseProfileDetails: document.getElementById('phase-profile-details'),
    historyModal: document.getElementById('history-modal'),
    historyContent: document.getElementById('history-content'),
    closeModal: document.getElementById('close-modal'),
    ticketsModal: document.getElementById('tickets-modal'),
    ticketsContent: document.getElementById('tickets-content'),
    closeTicketsModal: document.getElementById('close-tickets-modal'),
    viewTicketsBtn: document.getElementById('view-tickets-btn'),
    debugPanel: document.getElementById('debug-panel'),
    debugContent: document.getElementById('debug-content'),
    toggleDebug: document.getElementById('toggle-debug'),
    // Booking elements
    bookingSelect: document.getElementById('booking-select'),
    bookingDetails: document.getElementById('booking-details'),
    createBookingBtn: document.getElementById('create-booking-btn'),
    createBookingModal: document.getElementById('create-booking-modal'),
    closeBookingModal: document.getElementById('close-booking-modal'),
    createBookingForm: document.getElementById('create-booking-form'),
};

// Initialize
function init() {
    generateSessionId();
    setupEventListeners();
    resizeMessageInput();
    loadProperties();
}

function hasActiveInlineForm() {
    return !!elements.chatMessages?.querySelector('.inline-form-container .inline-form');
}

function isFormFocusModeActive() {
    return state.formFocusMode || hasActiveInlineForm();
}

function setFormFocusMode(active) {
    state.formFocusMode = !!active;
    if (state.formFocusMode && elements.suggestedActions) {
        elements.suggestedActions.innerHTML = '';
    }
    if (state.formFocusMode) {
        // Invalidate any in-flight suggestion request so stale chips never reappear.
        state.suggestionRequestId += 1;
    }
}

async function loadProperties() {
    if (!elements.hotelCode) return;
    try {
        const response = await fetch('/api/chat/properties');
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        const payload = await response.json();
        const rows = Array.isArray(payload?.properties) ? payload.properties : [];
        if (!rows.length) {
            throw new Error('No properties returned');
        }
        const optionsHtml = rows
            .map((row) => {
                const code = String(row?.code || '').trim();
                if (!code) return '';
                const name = String(row?.name || code).trim();
                const city = String(row?.city || '').trim();
                const label = city ? `${name} (${city})` : name;
                return `<option value="${code}">${label}</option>`;
            })
            .join('');
        if (optionsHtml) {
            elements.hotelCode.innerHTML = optionsHtml;
            const existing = String(state.hotelCode || '').trim().toUpperCase();
            const selected = rows.find((row) => String(row?.code || '').trim().toUpperCase() === existing);
            const nextCode = String(selected?.code || rows[0]?.code || 'DEFAULT').trim() || 'DEFAULT';
            state.hotelCode = nextCode;
            elements.hotelCode.value = nextCode;
        }
    } catch (error) {
        elements.hotelCode.innerHTML = '<option value="DEFAULT">Default Property</option>';
        state.hotelCode = 'DEFAULT';
        elements.hotelCode.value = 'DEFAULT';
    } finally {
        loadTestProfiles();
        loadBookings();
    }
}

// Generate unique session ID
function generateSessionId() {
    state.sessionId = 'session_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
    setFormFocusMode(false);
    updateSessionInfo();
}

function normalizePhaseId(value) {
    const raw = String(value || '').trim().toLowerCase().replace(/-/g, '_').replace(/\s+/g, '_');
    if (!raw) return '';
    const aliases = {
        prebooking: 'pre_booking',
        booking: 'pre_checkin',
        precheckin: 'pre_checkin',
        duringstay: 'during_stay',
        instay: 'during_stay',
        in_stay: 'during_stay',
        postcheckout: 'post_checkout',
    };
    return aliases[raw] || raw;
}

function sanitizeTestProfile(input) {
    if (!input || typeof input !== 'object') return null;
    const fields = [
        'guest_id',
        'entity_id',
        'organisation_id',
        'room_number',
        'guest_phone',
        'guest_name',
        'group_id',
        'ticket_source',
        'flow',
    ];
    const profile = {};
    fields.forEach((field) => {
        const value = input[field];
        if (value === undefined || value === null) return;
        const text = String(value).trim();
        if (text) profile[field] = text;
    });
    if (!profile.organisation_id && profile.entity_id) {
        profile.organisation_id = profile.entity_id;
    }
    if (!profile.entity_id && profile.organisation_id) {
        profile.entity_id = profile.organisation_id;
    }
    return profile;
}

function getActivePhaseProfile() {
    const phaseId = normalizePhaseId(state.phase);
    if (!state.testProfileAutoApply || !phaseId) return null;
    return state.testProfilesByPhase[phaseId] || null;
}

function syncPhaseProfileUi() {
    const profile = getActivePhaseProfile();
    state.activeTestProfile = profile;

    if (elements.autoPhaseProfile) {
        elements.autoPhaseProfile.checked = !!state.testProfileAutoApply;
    }

    if (!elements.phaseProfileStatus || !elements.phaseProfileDetails) return;

    const phaseId = normalizePhaseId(state.phase) || 'unknown';
    if (!state.testProfileAutoApply) {
        elements.phaseProfileStatus.textContent = `Auto profile mapping is OFF for ${phaseId}.`;
        elements.phaseProfileDetails.textContent = 'Auto mapping disabled.';
        return;
    }

    if (!profile) {
        elements.phaseProfileStatus.textContent = `No mapped test profile for ${phaseId}.`;
        elements.phaseProfileDetails.textContent = 'No active profile.';
        return;
    }

    elements.phaseProfileStatus.textContent = `Mapped test profile active for ${phaseId}.`;
    elements.phaseProfileDetails.textContent = JSON.stringify(profile, null, 2);
}

async function loadTestProfiles() {
    try {
        const response = await fetch(`/api/chat/test-profiles?hotel_code=${encodeURIComponent(state.hotelCode || 'DEFAULT')}`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        const data = await response.json();
        const mapped = {};
        const rawProfiles = data && typeof data.profiles_by_phase === 'object'
            ? data.profiles_by_phase
            : {};

        Object.entries(rawProfiles).forEach(([phaseKey, profile]) => {
            const normalizedPhase = normalizePhaseId(phaseKey);
            if (!normalizedPhase) return;
            const normalizedProfile = sanitizeTestProfile(profile);
            if (normalizedProfile) {
                mapped[normalizedPhase] = normalizedProfile;
            }
        });

        state.testProfilesByPhase = mapped;
        state.testProfileAutoApply = data?.auto_apply_enabled !== false;
    } catch (error) {
        console.error('Failed to load chat test profiles:', error);
        state.testProfilesByPhase = {};
        state.testProfileAutoApply = false;
    } finally {
        syncPhaseProfileUi();
    }
}

function buildRequestMetadata(interactionMeta = null) {
    const phaseId = normalizePhaseId(state.phase) || 'pre_booking';
    const metadata = {
        phase: phaseId,
        chat_test_profile_applied: false,
        chat_test_profile_phase: phaseId,
    };

    if (interactionMeta && typeof interactionMeta === 'object') {
        const sourceType = String(interactionMeta.source_type || '').trim();
        const sourceLabel = String(interactionMeta.source_label || '').trim();
        const sourceText = String(interactionMeta.source_text || '').trim();
        if (sourceType) metadata.ui_source_type = sourceType;
        if (sourceLabel) metadata.ui_source_label = sourceLabel;
        if (sourceText) metadata.ui_source_text = sourceText;
        metadata.ui_event_at = new Date().toISOString();
    }

    const profile = getActivePhaseProfile();
    if (!profile) return metadata;

    Object.entries(profile).forEach(([key, value]) => {
        if (value === undefined || value === null) return;
        const text = String(value).trim();
        if (text) metadata[key] = text;
    });

    if (!metadata.organisation_id && metadata.entity_id) {
        metadata.organisation_id = metadata.entity_id;
    }
    if (!metadata.entity_id && metadata.organisation_id) {
        metadata.entity_id = metadata.organisation_id;
    }

    metadata.chat_test_profile_applied = true;

    // Inject booking context if a booking is selected
    const booking = state.selectedBooking;
    if (booking) {
        metadata.booking_id = booking.booking_id;
        metadata.booking_guest_id = booking.guest_id;
        metadata.booking_confirmation_code = booking.confirmation_code;
        metadata.booking_property_name = booking.property_name || '';
        metadata.booking_room_number = booking.room_number || '';
        metadata.booking_room_type = booking.room_type || '';
        metadata.booking_check_in_date = booking.check_in_date || '';
        metadata.booking_check_out_date = booking.check_out_date || '';
        metadata.booking_guest_name = booking.guest_name || '';
        metadata.booking_guest_phone = booking.guest_phone || '';
        metadata.booking_status = booking.status || '';
        metadata.booking_phase = booking.phase || '';
    }

    return metadata;
}

// ============ Booking Management ============

async function loadBookings() {
    if (!elements.bookingSelect) return;
    const phase = normalizePhaseId(state.phase);
    if (phase === 'pre_booking') {
        elements.bookingSelect.innerHTML = '<option value="">N/A (pre-booking)</option>';
        state.bookings = [];
        state.selectedBooking = null;
        syncBookingDetailsUi();
        return;
    }
    try {
        const url = `/admin/api/bookings?hotel_code=${encodeURIComponent(state.hotelCode || 'DEFAULT')}&phase=${encodeURIComponent(phase)}`;
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const bookings = Array.isArray(data?.bookings) ? data.bookings : [];
        state.bookings = bookings;
        if (!bookings.length) {
            elements.bookingSelect.innerHTML = '<option value="">No bookings for this phase</option>';
            state.selectedBooking = null;
            syncBookingDetailsUi();
            return;
        }
        const opts = bookings.map((b) => {
            const label = `${b.guest_name || 'Guest'} - ${b.room_number || 'No room'} - ${b.property_name || ''} (${b.check_in_date} to ${b.check_out_date})`;
            return `<option value="${b.booking_id}">${label}</option>`;
        });
        opts.unshift('<option value="">-- Select a booking --</option>');
        elements.bookingSelect.innerHTML = opts.join('');
        // Auto-select first booking
        if (bookings.length === 1) {
            elements.bookingSelect.value = String(bookings[0].booking_id);
            state.selectedBooking = bookings[0];
        } else {
            state.selectedBooking = null;
        }
        syncBookingDetailsUi();
    } catch (err) {
        console.error('Failed to load bookings:', err);
        elements.bookingSelect.innerHTML = '<option value="">Error loading bookings</option>';
        state.bookings = [];
        state.selectedBooking = null;
        syncBookingDetailsUi();
    }
}

function syncBookingDetailsUi() {
    if (!elements.bookingDetails) return;
    const b = state.selectedBooking;
    if (!b) {
        elements.bookingDetails.textContent = 'No booking selected.';
        return;
    }
    elements.bookingDetails.textContent =
        `Code: ${b.confirmation_code}\n` +
        `Guest: ${b.guest_name || '-'} (${b.guest_phone || '-'})\n` +
        `Property: ${b.property_name || '-'}\n` +
        `Room: ${b.room_number || '-'} (${b.room_type || '-'})\n` +
        `Dates: ${b.check_in_date} to ${b.check_out_date}\n` +
        `Status: ${b.status} | Phase: ${b.phase}`;
}

async function handleCreateBooking(e) {
    e.preventDefault();
    const form = e.target;
    const fd = new FormData(form);
    const body = {};
    for (const [k, v] of fd.entries()) {
        if (v) body[k] = k === 'num_guests' ? parseInt(v, 10) : v;
    }
    try {
        const resp = await fetch(`/admin/api/bookings?hotel_code=${encodeURIComponent(state.hotelCode || 'DEFAULT')}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            alert(`Failed: ${err.detail || resp.statusText}`);
            return;
        }
        const created = await resp.json();
        alert(`Booking created! Code: ${created.confirmation_code}`);
        form.reset();
        if (elements.createBookingModal) elements.createBookingModal.classList.remove('visible');
        await loadBookings();
    } catch (err) {
        alert(`Error: ${err.message}`);
    }
}

// Setup event listeners
function setupEventListeners() {
    // Form submit
    elements.chatForm.addEventListener('submit', handleSubmit);
    elements.messageInput.addEventListener('keydown', handleMessageInputKeydown);
    elements.messageInput.addEventListener('input', resizeMessageInput);

    // Hotel selection
    elements.hotelCode.addEventListener('change', (e) => {
        state.hotelCode = e.target.value;
        loadTestProfiles();
        loadBookings();
    });
    elements.journeyPhase.addEventListener('change', (e) => {
        state.phase = e.target.value || 'pre_booking';
        syncPhaseProfileUi();
        loadBookings();
    });
    // Booking selection
    if (elements.bookingSelect) {
        elements.bookingSelect.addEventListener('change', (e) => {
            const id = parseInt(e.target.value, 10);
            state.selectedBooking = state.bookings.find((b) => b.booking_id === id) || null;
            syncBookingDetailsUi();
        });
    }
    if (elements.createBookingBtn) {
        elements.createBookingBtn.addEventListener('click', () => {
            if (elements.createBookingModal) elements.createBookingModal.classList.add('visible');
        });
    }
    if (elements.closeBookingModal) {
        elements.closeBookingModal.addEventListener('click', () => {
            if (elements.createBookingModal) elements.createBookingModal.classList.remove('visible');
        });
    }
    if (elements.createBookingModal) {
        elements.createBookingModal.addEventListener('click', (e) => {
            if (e.target === elements.createBookingModal) elements.createBookingModal.classList.remove('visible');
        });
    }
    if (elements.createBookingForm) {
        elements.createBookingForm.addEventListener('submit', handleCreateBooking);
    }
    if (elements.autoPhaseProfile) {
        elements.autoPhaseProfile.addEventListener('change', (e) => {
            state.testProfileAutoApply = !!e.target.checked;
            syncPhaseProfileUi();
        });
    }

    // Action buttons
    elements.newSessionBtn.addEventListener('click', () => {
        generateSessionId();
        clearChat();
    });

    elements.resetBtn.addEventListener('click', resetSession);
    elements.viewHistoryBtn.addEventListener('click', viewHistory);
    elements.closeModal.addEventListener('click', () => {
        elements.historyModal.classList.remove('visible');
    });
    elements.viewTicketsBtn.addEventListener('click', viewTickets);
    elements.closeTicketsModal.addEventListener('click', () => {
        elements.ticketsModal.classList.remove('visible');
    });
    elements.ticketsModal.addEventListener('click', (e) => {
        if (e.target === elements.ticketsModal) {
            elements.ticketsModal.classList.remove('visible');
        }
    });

    // Scenario buttons
    document.querySelectorAll('.scenario-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const message = btn.dataset.message;
            sendMessage(message, {
                source_type: 'scenario_button',
                source_label: String(btn.textContent || '').trim(),
                source_text: message,
            });
        });
    });

    // Debug toggle
    elements.toggleDebug.addEventListener('click', () => {
        elements.debugPanel.classList.remove('visible');
    });

    // Keyboard shortcut for debug (Ctrl+D)
    document.addEventListener('keydown', (e) => {
        if (e.ctrlKey && e.key === 'd') {
            e.preventDefault();
            elements.debugPanel.classList.toggle('visible');
        }
    });

    // Click outside modal to close
    elements.historyModal.addEventListener('click', (e) => {
        if (e.target === elements.historyModal) {
            elements.historyModal.classList.remove('visible');
        }
    });
}

function handleMessageInputKeydown(e) {
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
    e.preventDefault();
    if (state.isLoading) return;
    const message = elements.messageInput.value.trim();
    if (!message) return;
    if (typeof elements.chatForm.requestSubmit === 'function') {
        elements.chatForm.requestSubmit();
        return;
    }
    elements.chatForm.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
}

function resizeMessageInput() {
    const input = elements.messageInput;
    if (!input) return;
    input.style.height = 'auto';
    input.style.height = `${input.scrollHeight}px`;
}

// Handle form submit
async function handleSubmit(e) {
    e.preventDefault();
    const message = elements.messageInput.value.trim();
    if (!message || state.isLoading) return;

    elements.messageInput.value = '';
    resizeMessageInput();
    await sendMessage(message, {
        source_type: 'typed_input',
        source_label: 'typed_input',
        source_text: message,
    });
}

// Send message to API
async function sendMessage(message, interactionMeta = null) {
    if (state.isLoading) return;

    state.isLoading = true;
    elements.sendBtn.disabled = true;
    clearTicketDetails();

    // Add user message to UI
    addMessageToUI('user', message);

    // Show loading indicator
    const loadingEl = showLoading();

    try {
        const response = await fetch('/api/chat/message', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                session_id: state.sessionId,
                message: message,
                hotel_code: state.hotelCode,
                channel: 'web_widget',
                metadata: buildRequestMetadata(interactionMeta),
            }),
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();

        // Remove loading indicator
        loadingEl.remove();

        // Add bot response to UI
        const displayMessage = resolveAssistantDisplayMessage(data);
        addMessageToUI('assistant', displayMessage, data);

        // Attach inline form if orchestration decided to collect info
        const formFields = Array.isArray(data.metadata?.form_fields)
            ? data.metadata.form_fields
            : (Array.isArray(data.form_fields) ? data.form_fields : []);
        const rawFormTrigger = data.metadata?.form_trigger;
        const explicitFormTrigger = (
            rawFormTrigger === true
            || rawFormTrigger === 'true'
            || rawFormTrigger === 1
            || rawFormTrigger === '1'
        );
        const stateAwaitingInfo = String(data.state || '').trim().toLowerCase() === 'awaiting_info';
        const serviceLabel = String(
            data.service_llm_label || data.metadata?.service_llm_label || ''
        ).trim().toLowerCase();
        const messageText = String(
            data.display_message || data.metadata?.display_message || data.message || ''
        ).trim().toLowerCase();
        const messageHintsCollection = (
            messageText.includes('please fill in the details below')
            || messageText.includes('please fill in the booking details below')
            || messageText.includes('please fill in the form below')
        );
        const inferredFormTrigger = (
            stateAwaitingInfo
            && !!serviceLabel
            && serviceLabel !== 'main'
            && formFields.length > 0
            && messageHintsCollection
        );
        const shouldShowInlineForm = (explicitFormTrigger || inferredFormTrigger) && formFields.length > 0;
        const orchestrationDecision = data.metadata?.orchestration_decision || data.metadata?.decision || {};
        const resolvedFormServiceId = String(
            data.metadata?.form_service_id
            || data.form_service_id
            || data.metadata?.pending_service_id
            || data.metadata?.orchestration_target_service_id
            || orchestrationDecision?.target_service_id
            || serviceLabel
            || ''
        ).trim();

        console.log('[form_trigger]', {
            state: data.state,
            form_trigger_raw: rawFormTrigger,
            explicit_form_trigger: explicitFormTrigger,
            inferred_form_trigger: inferredFormTrigger,
            should_show_form: shouldShowInlineForm,
            form_service_id: resolvedFormServiceId,
            form_fields_count: formFields.length,
            service_llm_label: data.service_llm_label || data.metadata?.service_llm_label,
        });
        if (shouldShowInlineForm) {
            setFormFocusMode(true);
            attachInlineForm(formFields, resolvedFormServiceId, data);
        } else if (!hasActiveInlineForm()) {
            setFormFocusMode(false);
        }

        // Update session info
        updateSessionInfoFromResponse(data);

        // Suppress suggestion chips entirely when a form is displayed
        const formWasTriggered = shouldShowInlineForm;
        if (formWasTriggered) {
            elements.suggestedActions.innerHTML = '';
        } else {
            // Always prefer the dedicated suggestions LLM layer for contextual chips.
            // Keep deterministic runtime chips only for strict states like confirmation/escalation.
            const runtimeSuggestions = Array.isArray(data.suggested_actions) ? data.suggested_actions : [];
            const stateValue = String(data.state || '').toLowerCase();
            const useRuntimeDirectly = (
                runtimeSuggestions.length > 0
                && (stateValue === 'awaiting_confirmation' || stateValue === 'escalated')
            );
            if (useRuntimeDirectly) {
                showSuggestedActions(runtimeSuggestions);
            } else {
                fetchAndShowSuggestions(data.message, message, state.sessionId, runtimeSuggestions);
            }
        }

        // Update debug panel
        updateDebug(data);

    } catch (error) {
        console.error('Error:', error);
        loadingEl.remove();
        addMessageToUI('assistant', 'Sorry, there was an error processing your request. Please try again.');
    } finally {
        state.isLoading = false;
        elements.sendBtn.disabled = false;
        elements.messageInput.focus();
    }
}

function resolveAssistantDisplayMessage(data) {
    if (!data || typeof data !== 'object') return '';
    const displayField = String(data.display_message || '').trim();
    if (displayField) return displayField;
    const metadataDisplay = String(data.metadata?.display_message || '').trim();
    if (metadataDisplay) return metadataDisplay;
    return String(data.message || '').trim();
}

// Add message to UI
function addMessageToUI(role, content, data = null) {
    // Remove welcome message if exists
    const welcome = elements.chatMessages.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    const messageEl = document.createElement('div');
    messageEl.className = `message ${role}`;

    const renderedContent = role === 'assistant'
        ? renderAssistantMessageHtml(content)
        : escapeHtml(content);
    let html = `<div class="message-content">${renderedContent}</div>`;

    if (role === 'assistant' && data) {
        const label = data.service_llm_label || (data.metadata && data.metadata.service_llm_label);
        if (label) {
            const display = label === 'main'
                ? 'main orchestrator'
                : `${label} agent`;
            html += `<div class="llm-source-label">answered by: ${escapeHtml(display)}</div>`;
        }
    }

    messageEl.innerHTML = html;
    elements.chatMessages.appendChild(messageEl);
    scrollToBottom();

    const canonicalContent = (
        role === 'assistant' && data && typeof data === 'object'
    )
        ? String(data.message || content || '')
        : String(content || '');
    state.messages.push({
        role,
        content: canonicalContent,
        display_content: String(content || ''),
        data,
    });
}

function renderAssistantMessageHtml(text) {
    const escaped = escapeHtml(text);
    if (!escaped) return '';
    let rendered = escaped;
    // Bold: **text** or __text__
    rendered = rendered.replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>');
    rendered = rendered.replace(/__([\s\S]+?)__/g, '<strong>$1</strong>');
    // Italic: *text* or _text_ (but not inside bold markers)
    rendered = rendered.replace(/(?<!\w)\*((?!\s)[^*]+(?<!\s))\*(?!\w)/g, '<em>$1</em>');
    rendered = rendered.replace(/(?<!\w)_((?!\s)[^_]+(?<!\s))_(?!\w)/g, '<em>$1</em>');
    // Markdown headers: ### heading, ## heading, # heading → just plain bold text
    rendered = rendered.replace(/^#{1,3}\s+(.+)$/gm, '<strong>$1</strong>');
    return rendered;
}

function normalizeSafeLink(url) {
    const raw = String(url || '').trim();
    if (!raw) return '';
    const decoder = document.createElement('textarea');
    decoder.innerHTML = raw;
    const decoded = String(decoder.value || raw).trim();
    const withProtocol = /^https?:\/\//i.test(decoded) ? decoded : `https://${decoded}`;
    try {
        const parsed = new URL(withProtocol);
        if (!['http:', 'https:'].includes(parsed.protocol)) return '';
        return parsed.href;
    } catch (error) {
        return '';
    }
}

function buildAssistantLinkHtml(url, label) {
    const href = normalizeSafeLink(url);
    const text = String(label || url || '').trim();
    if (!href || !text) return text;
    return `<a class="chat-link" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${text}</a>`;
}

function renderMarkdownLinks(rendered) {
    return rendered.replace(/\[([^\]\n]+?)\]\((https?:\/\/[^\s)]+)\)/g, (_match, label, url) => {
        return buildAssistantLinkHtml(url, label);
    });
}

function linkifyPlainUrls(rendered) {
    return rendered.replace(/(^|[\s(>])((?:https?:\/\/|www\.)[^\s<]+)/gi, (_match, prefix, url) => {
        const trailing = (url.match(/[).,!?:;]+$/) || [''])[0];
        const cleanUrl = trailing ? url.slice(0, -trailing.length) : url;
        const linked = buildAssistantLinkHtml(cleanUrl, cleanUrl);
        return `${prefix}${linked}${trailing}`;
    });
}

function applyAssistantTextFormatting(rendered) {
    let output = rendered;
    output = output.replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>');
    output = output.replace(/__([\s\S]+?)__/g, '<strong>$1</strong>');
    output = output.replace(/(?<!\w)\*((?!\s)[^*]+(?<!\s))\*(?!\w)/g, '<em>$1</em>');
    output = output.replace(/(?<!\w)_((?!\s)[^_]+(?<!\s))_(?!\w)/g, '<em>$1</em>');
    output = output.replace(/^#{1,3}\s+(.+)$/gm, '<strong>$1</strong>');
    return output;
}

function renderAssistantMessageHtml(text) {
    const escaped = escapeHtml(text);
    if (!escaped) return '';
    let rendered = applyAssistantTextFormatting(escaped);
    rendered = renderMarkdownLinks(rendered);
    rendered = linkifyPlainUrls(rendered);
    return rendered;
}

// Show loading indicator
function showLoading() {
    const loadingEl = document.createElement('div');
    loadingEl.className = 'message assistant loading';
    loadingEl.innerHTML = '<span></span><span></span><span></span>';
    elements.chatMessages.appendChild(loadingEl);
    scrollToBottom();
    return loadingEl;
}

// Update session info from response
function updateSessionInfoFromResponse(data) {
    elements.sessionState.textContent = data.state;
    elements.sessionState.className = `value state-badge ${data.state}`;
    const ticketStatus = resolveTicketStatus(data);
    elements.ticketStatus.textContent = ticketStatus.label;
    elements.ticketStatus.className = `value ticket-badge ${ticketStatus.badge}`;
    renderCreatedTicketDetails(data);
    elements.messageCount.textContent = data.metadata?.message_count || state.messages.length;
}


function resolveTicketStatus(data) {
    const metadata = data?.metadata || {};
    const ticketId = String(metadata.ticket_id || '').trim();
    const ticketState = String(metadata.ticket_status || '').trim().toLowerCase();
    const ticketError = String(metadata.ticket_create_error || '').trim();
    const skipReason = String(
        metadata.ticket_create_skip_reason
        || metadata.ticket_skip_reason
        || ''
    ).trim();

    if (metadata.ticket_created === true || ticketId) {
        const stateSuffix = ticketState ? ` (${ticketState})` : '';
        const idLabel = ticketId || 'unknown-id';
        return {
            label: `Created: ${idLabel}${stateSuffix}`,
            badge: 'created',
        };
    }

    if (ticketError) {
        return {
            label: `Not created: ${ticketError}`,
            badge: 'failed',
        };
    }

    if (metadata.ticket_created === false || skipReason) {
        return {
            label: skipReason ? `Not created: ${skipReason}` : 'Not created',
            badge: 'not-created',
        };
    }

    if (metadata.ticketing_required === true && metadata.ticketing_create_allowed === false) {
        return {
            label: 'Not created: gated',
            badge: 'not-created',
        };
    }

    if (metadata.ticketing_required === true) {
        return {
            label: 'Ticket required',
            badge: 'pending',
        };
    }

    return {
        label: 'No ticket action',
        badge: 'idle',
    };
}

// Update session info display
function updateSessionInfo() {
    elements.sessionId.textContent = state.sessionId.substring(0, 15) + '...';
    elements.sessionState.textContent = 'idle';
    elements.sessionState.className = 'value state-badge idle';
    elements.ticketStatus.textContent = 'No ticket action';
    elements.ticketStatus.className = 'value ticket-badge idle';
    clearTicketDetails();
    elements.messageCount.textContent = '0';
}

function clearTicketDetails() {
    if (!elements.ticketDetailsWrap || !elements.ticketDetailsContent) return;
    elements.ticketDetailsWrap.classList.add('hidden');
    elements.ticketDetailsContent.textContent = 'No ticket created in this turn.';
}

function renderCreatedTicketDetails(data) {
    if (!elements.ticketDetailsWrap || !elements.ticketDetailsContent) return;
    const details = resolveCreatedTicketDetails(data);
    if (!details) return;
    elements.ticketDetailsContent.textContent = JSON.stringify(details, null, 2);
    elements.ticketDetailsWrap.classList.remove('hidden');
}

function resolveCreatedTicketDetails(data) {
    const metadata = data?.metadata || {};
    const ticketId = String(metadata.ticket_id || '').trim();
    const ticketCreated = metadata.ticket_created === true || !!ticketId;
    if (!ticketCreated) return null;

    const apiResponse = metadata.ticket_api_response;
    if (apiResponse && typeof apiResponse === 'object') {
        const rawRecord = apiResponse.ticket_record || apiResponse.record || apiResponse.ticket;
        if (rawRecord && typeof rawRecord === 'object') {
            return rawRecord;
        }
    }

    if (metadata.ticket_record && typeof metadata.ticket_record === 'object') {
        return metadata.ticket_record;
    }

    const fallback = {};
    const keys = [
        'ticket_id',
        'ticket_status',
        'ticket_category',
        'ticket_sub_category',
        'ticket_priority',
        'ticket_summary',
        'ticket_source',
        'room_number',
        'ticket_service_id',
        'ticket_service_name',
    ];
    keys.forEach((key) => {
        if (metadata[key] !== undefined && metadata[key] !== null && String(metadata[key]).trim() !== '') {
            fallback[key] = metadata[key];
        }
    });
    if (apiResponse && typeof apiResponse === 'object') {
        fallback.ticket_api_response = apiResponse;
    }
    return Object.keys(fallback).length > 0 ? fallback : null;
}

// Fetch context-aware suggestions from LLM and display them.
// Falls back to runtime suggestions if LLM suggestions are unavailable.
async function fetchAndShowSuggestions(lastBotMessage, userMessage, sessionId, fallbackSuggestions = []) {
    const requestId = ++state.suggestionRequestId;
    elements.suggestedActions.innerHTML = '';
    if (isFormFocusModeActive()) {
        return;
    }
    let resolved = [];
    try {
        const response = await fetch('/api/chat/suggestions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                last_bot_message: lastBotMessage,
                user_message: userMessage,
                hotel_code: state.hotelCode,
                current_phase: state.phase,
                session_id: sessionId || state.sessionId,
                fallback_suggestions: Array.isArray(fallbackSuggestions) ? fallbackSuggestions : [],
            }),
        });
        if (!response.ok) {
            if (requestId !== state.suggestionRequestId || isFormFocusModeActive()) return;
            if (Array.isArray(fallbackSuggestions) && fallbackSuggestions.length > 0) {
                showSuggestedActions(fallbackSuggestions);
            }
            return;
        }
        const data = await response.json();
        resolved = Array.isArray(data.suggestions) ? data.suggestions : [];
    } catch (e) {
        resolved = [];
    }

    if (requestId !== state.suggestionRequestId || isFormFocusModeActive()) {
        return;
    }

    if (resolved.length > 0) {
        showSuggestedActions(resolved);
        return;
    }
    if (Array.isArray(fallbackSuggestions) && fallbackSuggestions.length > 0) {
        showSuggestedActions(fallbackSuggestions);
    }
}

// Show suggested actions
function showSuggestedActions(actions) {
    if (isFormFocusModeActive()) {
        elements.suggestedActions.innerHTML = '';
        return;
    }
    elements.suggestedActions.innerHTML = '';

    if (!actions || actions.length === 0) return;

    actions.forEach(action => {
        const btn = document.createElement('button');
        btn.className = 'suggested-action';
        btn.textContent = action;
        btn.addEventListener('click', () => {
            sendMessage(action, {
                source_type: 'suggested_action',
                source_label: action,
                source_text: action,
            });
        });
        elements.suggestedActions.appendChild(btn);
    });
}

// Clear chat
function clearChat() {
    elements.chatMessages.innerHTML = `
        <div class="welcome-message">
            <p>Start a conversation to test the bot.</p>
            <p>Use the quick test buttons on the left or type your own messages.</p>
        </div>
    `;
    state.messages = [];
    setFormFocusMode(false);
    elements.suggestedActions.innerHTML = '';
    clearTicketDetails();
    updateSessionInfo();
}

// Reset session state
async function resetSession() {
    try {
        const response = await fetch(`/api/chat/session/${state.sessionId}/reset`, {
            method: 'POST',
        });

        if (response.ok) {
            elements.sessionState.textContent = 'idle';
            elements.sessionState.className = 'value state-badge idle';
            elements.ticketStatus.textContent = 'No ticket action';
            elements.ticketStatus.className = 'value ticket-badge idle';
            clearTicketDetails();
            addMessageToUI('assistant', 'Session state has been reset. How can I help you?');
        }
    } catch (error) {
        console.error('Error resetting session:', error);
    }
}

// View history
async function viewHistory() {
    elements.historyModal.classList.add('visible');
    elements.historyContent.innerHTML = 'Loading...';

    try {
        const response = await fetch(`/api/chat/session/${state.sessionId}`);

        if (!response.ok) {
            throw new Error('Session not found');
        }

        const data = await response.json();

        let html = `
            <div style="margin-bottom: 15px; padding: 10px; background: #f9f9fa; border-radius: 8px;">
                <strong>Session ID:</strong> ${data.session_id}<br>
                <strong>State:</strong> ${data.state}<br>
                <strong>Hotel:</strong> ${data.hotel_code}<br>
                <strong>Created:</strong> ${new Date(data.created_at).toLocaleString()}
            </div>
            <hr style="margin: 15px 0; border: none; border-top: 1px solid #e5e7eb;">
        `;

        if (data.messages.length === 0) {
            html += '<p style="color: #6b7280;">No messages yet</p>';
        } else {
            data.messages.forEach(msg => {
                html += `
                    <div class="history-message ${msg.role}">
                        <div class="role">${msg.role}</div>
                        <div>${escapeHtml(msg.content)}</div>
                        <div class="time">${new Date(msg.timestamp).toLocaleTimeString()}</div>
                    </div>
                `;
            });
        }

        elements.historyContent.innerHTML = html;

    } catch (error) {
        elements.historyContent.innerHTML = `<p style="color: #ef4444;">Error loading history: ${error.message}</p>`;
    }
}

// Update debug panel
function updateDebug(data) {
    elements.debugContent.textContent = JSON.stringify(data, null, 2);
}

// Utility: Escape HTML
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Utility: Scroll to bottom
function scrollToBottom() {
    elements.chatMessages.scrollTop = elements.chatMessages.scrollHeight;
}

// View local tickets
async function viewTickets() {
    elements.ticketsModal.classList.add('visible');
    elements.ticketsContent.innerHTML = 'Loading...';

    try {
        const response = await fetch('/admin/api/tickets');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        const tickets = data.tickets || [];

        if (tickets.length === 0) {
            elements.ticketsContent.innerHTML = '<p style="color:#6b7280;">No tickets yet.</p>';
            return;
        }

        let html = `<p style="margin-bottom:12px;color:#6b7280;">${tickets.length} ticket(s) found - newest first</p>`;
        tickets.forEach(t => {
            const created = t.created_at ? new Date(t.created_at).toLocaleString() : '-';
            const status = t.status || 'open';
            const statusColor = status === 'open' ? '#ff5a7e' : '#6b7280';
            const fields = Object.entries(t)
                .filter(([k]) => !['created_at', 'updated_at', 'ticket_id', 'status', 'id'].includes(k))
                .map(([k, v]) => {
                    if (!v && v !== 0) return '';
                    return `<div><span style="color:#6b7280;min-width:130px;display:inline-block;">${k}:</span> ${escapeHtml(String(v))}</div>`;
                })
                .filter(Boolean)
                .join('');

            html += `
                <div style="border:1px solid #e5e7eb;border-radius:8px;padding:14px;margin-bottom:12px;background:#fff;">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                        <strong style="font-size:15px;">${escapeHtml(t.ticket_id || t.id || '-')}</strong>
                        <span style="background:${statusColor};color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">${escapeHtml(status)}</span>
                    </div>
                    <div style="font-size:13px;line-height:1.7;">${fields}</div>
                    <div style="font-size:11px;color:#6b7280;margin-top:6px;">Created: ${created}</div>
                </div>
            `;
        });

        elements.ticketsContent.innerHTML = html;
    } catch (error) {
        elements.ticketsContent.innerHTML = `<p style="color:#ef4444;">Error loading tickets: ${error.message}</p>`;
    }
}

// ─── Inline Form ─────────────────────────────────────────────────────────────

/**
 * Attach an inline booking form to the last assistant message bubble.
 * @param {Array}  fields     - [{id, label, type, required}, ...]
 * @param {string} serviceId  - service_id to submit against
 * @param {object} responseData - full ChatResponse (for metadata)
 */
/** Detect phone fields by type OR by id/label keywords */
function _isPhoneField(field) {
    if (field.type === 'tel') return true;
    const hint = ((field.id || '') + ' ' + (field.label || '')).toLowerCase();
    return /\b(phone|mobile|contact|cell|whatsapp)\b/.test(hint);
}

/**
 * Compute min/max date bounds for date inputs based on the current journey
 * phase and the selected booking. In pre_checkin and during_stay, dates are
 * clamped to the guest's stay window so the browser's native date picker
 * greys out anything outside it. Returns YYYY-MM-DD strings; '' = unbounded.
 */
function _getStayDateBounds() {
    const todayStr = new Date().toISOString().split('T')[0];
    const phase    = normalizePhaseId(state.phase);
    const booking  = state.selectedBooking;
    const checkIn  = (booking && booking.check_in_date)  || '';
    const checkOut = (booking && booking.check_out_date) || '';

    if (phase === 'pre_checkin' && checkIn && checkOut) {
        // Stay hasn't started yet — earliest selectable is later of today
        // and check-in; latest is check-out.
        const min = checkIn > todayStr ? checkIn : todayStr;
        return { min, max: checkOut };
    }
    if (phase === 'during_stay' && checkOut) {
        // Already in stay — earliest is today, latest is check-out.
        return { min: todayStr, max: checkOut };
    }
    // pre_booking and any other phase: keep prior behavior (no past dates).
    return { min: todayStr, max: '' };
}

function attachInlineForm(fields, serviceId, responseData) {
    const messages = elements.chatMessages.querySelectorAll('.message.assistant');
    const lastMessage = messages[messages.length - 1];
    if (!lastMessage) return;

    // Common country codes for the phone dropdown
    const _countryCodes = [
        { code: '+91',  flag: '🇮🇳', name: 'India' },
        { code: '+1',   flag: '🇺🇸', name: 'US/Canada' },
        { code: '+44',  flag: '🇬🇧', name: 'UK' },
        { code: '+971', flag: '🇦🇪', name: 'UAE' },
        { code: '+65',  flag: '🇸🇬', name: 'Singapore' },
        { code: '+61',  flag: '🇦🇺', name: 'Australia' },
        { code: '+49',  flag: '🇩🇪', name: 'Germany' },
        { code: '+33',  flag: '🇫🇷', name: 'France' },
        { code: '+81',  flag: '🇯🇵', name: 'Japan' },
        { code: '+86',  flag: '🇨🇳', name: 'China' },
        { code: '+966', flag: '🇸🇦', name: 'Saudi Arabia' },
        { code: '+974', flag: '🇶🇦', name: 'Qatar' },
        { code: '+60',  flag: '🇲🇾', name: 'Malaysia' },
        { code: '+66',  flag: '🇹🇭', name: 'Thailand' },
        { code: '+7',   flag: '🇷🇺', name: 'Russia' },
        { code: '+55',  flag: '🇧🇷', name: 'Brazil' },
        { code: '+27',  flag: '🇿🇦', name: 'South Africa' },
        { code: '+234', flag: '🇳🇬', name: 'Nigeria' },
        { code: '+254', flag: '🇰🇪', name: 'Kenya' },
        { code: '+82',  flag: '🇰🇷', name: 'South Korea' },
    ];

    // Build field HTML
    let fieldsHtml = '';
    fields.forEach(field => {
        const nameAttr = `name="${escapeAttr(field.id)}"`;
        const requiredAttr = field.required ? ' required' : '';
        let inputHtml;
        if (field.type === 'textarea') {
            inputHtml = `<textarea ${nameAttr} rows="3"${requiredAttr} class="inline-form-input"></textarea>`;
        } else if (field.type === 'date') {
            const { min: minDate, max: maxDate } = _getStayDateBounds();
            const minAttr = minDate ? ` min="${minDate}"` : '';
            const maxAttr = maxDate ? ` max="${maxDate}"` : '';
            inputHtml = `<input type="date" ${nameAttr}${requiredAttr}${minAttr}${maxAttr} class="inline-form-input">`;
        } else if (_isPhoneField(field)) {
            const ccOptions = _countryCodes.map(cc =>
                `<option value="${cc.code}"${cc.code === '+91' ? ' selected' : ''}>${cc.flag} ${cc.code}</option>`
            ).join('');
            inputHtml = `<div class="inline-form-phone-group">` +
                `<select class="inline-form-cc-select" data-for="${escapeAttr(field.id)}">${ccOptions}</select>` +
                `<input type="tel" ${nameAttr}${requiredAttr} class="inline-form-input inline-form-phone-input" placeholder="Phone number">` +
                `</div>`;
        } else {
            inputHtml = `<input type="${escapeAttr(field.type)}" ${nameAttr}${requiredAttr} class="inline-form-input">`;
        }
        const requiredStar = field.required
            ? '<span class="inline-form-required-star">*</span>'
            : '';
        fieldsHtml += `
            <div class="inline-form-field" data-field-id="${escapeAttr(field.id)}">
                <label class="inline-form-label">${escapeHtml(field.label)}${requiredStar}</label>
                ${inputHtml}
            </div>`;
    });

    const container = document.createElement('div');
    container.className = 'inline-form-container';
    container.dataset.serviceId = serviceId;
    container.innerHTML = `
        <div class="inline-form-errors" style="display:none;"></div>
        <form class="inline-form" novalidate>
            <div class="inline-form-fields">${fieldsHtml}</div>
            <button type="submit" class="inline-form-submit">Submit</button>
        </form>`;

    lastMessage.appendChild(container);
    scrollToBottom();

    // Enforce min/max-date on date inputs: reject anything outside the
    // allowed window (past dates, or — in pre_checkin/during_stay — dates
    // outside the guest's stay).
    container.querySelectorAll('input[type="date"]').forEach(dateInput => {
        dateInput.addEventListener('change', () => {
            const minVal = dateInput.min;
            const maxVal = dateInput.max;
            if (minVal && dateInput.value && dateInput.value < minVal) {
                dateInput.value = '';
                return;
            }
            if (maxVal && dateInput.value && dateInput.value > maxVal) {
                dateInput.value = '';
            }
        });
    });

    // Clear per-field error styling on input/change
    container.querySelectorAll('.inline-form-input').forEach(inp => {
        const clearFieldError = () => {
            inp.classList.remove('inline-form-input-error');
            const fieldDiv = inp.closest('.inline-form-field');
            if (fieldDiv) {
                fieldDiv.querySelectorAll('.inline-form-field-error').forEach(el => el.remove());
            }
        };
        inp.addEventListener('input', clearFieldError);
        inp.addEventListener('change', clearFieldError);
    });

    const form        = container.querySelector('.inline-form');
    const errorsDiv   = container.querySelector('.inline-form-errors');
    const submitBtn   = container.querySelector('.inline-form-submit');

    /** Helper: show per-field errors on the form */
    function showFieldErrors(errorList) {
        errorList.forEach(fe => {
            const fid = fe.id || fe.field_id;
            const fieldDiv = form.querySelector(`[data-field-id="${fid}"]`);
            if (!fieldDiv) return;
            const inp = fieldDiv.querySelector('.inline-form-input');
            if (inp) inp.classList.add('inline-form-input-error');
            // Insert error ABOVE the input
            const errSpan = document.createElement('span');
            errSpan.className = 'inline-form-field-error';
            errSpan.textContent = fe.message;
            const label = fieldDiv.querySelector('.inline-form-label');
            if (label && label.nextSibling) {
                fieldDiv.insertBefore(errSpan, label.nextSibling);
            } else {
                fieldDiv.appendChild(errSpan);
            }
        });
        errorsDiv.innerHTML = '<strong>Please fix the highlighted fields:</strong>';
        errorsDiv.style.display = 'block';
        errorsDiv.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    form.addEventListener('submit', async (evt) => {
        evt.preventDefault();

        // Clear previous per-field error highlights
        form.querySelectorAll('.inline-form-input').forEach(inp => {
            inp.classList.remove('inline-form-input-error');
        });
        form.querySelectorAll('.inline-form-field-error').forEach(el => el.remove());
        errorsDiv.style.display = 'none';

        // Collect values
        const formValues = {};
        fields.forEach(field => {
            const el = form.querySelector(`[name="${field.id}"]`);
            let val = el ? el.value.trim() : '';
            // For phone fields, prepend the selected country code
            if (_isPhoneField(field) && val) {
                const ccSelect = form.querySelector(`select[data-for="${field.id}"]`);
                if (ccSelect) {
                    const cc = ccSelect.value;
                    // Only prepend if user hasn't already typed the code
                    if (!val.startsWith('+')) {
                        val = cc + val;
                    }
                }
            }
            formValues[field.id] = val;
        });

        // Validation is intentionally bypassed: submit exactly what the guest entered.
        submitBtn.disabled = true;
        submitBtn.textContent = 'Submitting\u2026';

        try {
            const reqMeta = buildRequestMetadata(null);
            const res = await fetch('/api/chat/form-submit', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    session_id: state.sessionId,
                    hotel_code: state.hotelCode,
                    service_id: serviceId,
                    form_data: formValues,
                    metadata: reqMeta,
                }),
            });

            const result = await res.json();

            if (result.success) {
                // Replace form with success message
                container.innerHTML =
                    `<div class="inline-form-success">${escapeHtml(result.message)}</div>`;
                setFormFocusMode(false);
                // Update session info panel
                const confirmData = {
                    state: 'completed',
                    metadata: {
                        ticket_created: true,
                        ticket_id: result.ticket_id || '',
                        message_count: state.messages.length + 1,
                    },
                };
                updateSessionInfoFromResponse(confirmData);
                elements.suggestedActions.innerHTML = '';
            } else {
                if (Array.isArray(result.errors) && result.errors.length > 0) {
                    const submitErrors = result.errors.map(e => ({
                        id: e.field_id || e.id,
                        field_id: e.field_id || e.id,
                        message: e.message || 'Please correct this field.',
                    }));
                    showFieldErrors(submitErrors);
                } else {
                    errorsDiv.innerHTML = `<strong>${escapeHtml(result.message || 'Submission failed. Please try again.')}</strong>`;
                    errorsDiv.style.display = 'block';
                }
                submitBtn.disabled = false;
                submitBtn.textContent = 'Submit';
            }
        } catch (err) {
            errorsDiv.innerHTML = '<strong>Network error. Please try again.</strong>';
            errorsDiv.style.display = 'block';
            submitBtn.disabled = false;
            submitBtn.textContent = 'Submit';
        }
    });
}

/**
 * Validate form field values.
 * Returns an array of human-readable error strings (empty = all valid).
 */
function validateFormFields(fields, values) {
    const errors = [];
    let checkinDate  = null;
    let checkoutDate = null;
    const { min: minDate, max: maxDate } = _getStayDateBounds();

    fields.forEach(field => {
        const val = String(values[field.id] || '').trim();

        if (field.required && !val) {
            errors.push(`${field.label} is required.`);
            return;
        }
        if (!val) return; // optional empty field – skip further checks

        if (field.type === 'date') {
            const d = new Date(val);
            if (isNaN(d.getTime())) {
                errors.push(`${field.label} must be a valid date.`);
                return;
            }
            if (minDate && val < minDate) {
                if (maxDate) {
                    errors.push(`${field.label} must be within your stay (${minDate} to ${maxDate}).`);
                } else {
                    errors.push(`${field.label} cannot be in the past.`);
                }
                return;
            }
            if (maxDate && val > maxDate) {
                errors.push(`${field.label} must be within your stay (${minDate} to ${maxDate}).`);
                return;
            }
            const id = field.id.toLowerCase();
            if (id.includes('checkin') || id.includes('check_in') || id.includes('arrival') || id.includes('start')) {
                checkinDate = d;
            }
            if (id.includes('checkout') || id.includes('check_out') || id.includes('departure') || id.includes('end')) {
                checkoutDate = d;
            }
        }

        if (field.type === 'number') {
            const n = Number(val);
            if (isNaN(n) || n <= 0 || !Number.isInteger(n)) {
                errors.push(`${field.label} must be a positive whole number.`);
            }
        }

        if (field.type === 'tel') {
            const digits = val.replace(/\D/g, '');
            if (digits.length < 7) {
                errors.push(`${field.label}: Please enter a valid phone number.`);
            }
        }

        if (field.type === 'email') {
            if (!val.includes('@') || val.indexOf('.', val.indexOf('@')) === -1) {
                errors.push(`${field.label} must be a valid email address.`);
            }
        }
    });

    if (checkinDate && checkoutDate && checkoutDate <= checkinDate) {
        errors.push('Check-out date must be after check-in date.');
    }

    return errors;
}

/**
 * Validate form field values — returns per-field error objects.
 * Each entry: { id: field.id, message: "..." }
 */
function validateFormFieldsDetailed(fields, values) {
    const errors = [];
    let checkinDate  = null;
    let checkoutDate = null;
    let checkinFieldId = null;
    let checkoutFieldId = null;
    const { min: minDate, max: maxDate } = _getStayDateBounds();

    fields.forEach(field => {
        const val = String(values[field.id] || '').trim();

        if (field.required && !val) {
            errors.push({ id: field.id, message: `${field.label} is required.` });
            return;
        }
        if (!val) return;

        if (field.type === 'date') {
            const d = new Date(val);
            if (isNaN(d.getTime())) {
                errors.push({ id: field.id, message: 'Enter a valid date.' });
                return;
            }
            if (minDate && val < minDate) {
                if (maxDate) {
                    errors.push({ id: field.id, message: `Must be within your stay (${minDate} to ${maxDate}).` });
                } else {
                    errors.push({ id: field.id, message: 'Date cannot be in the past.' });
                }
                return;
            }
            if (maxDate && val > maxDate) {
                errors.push({ id: field.id, message: `Must be within your stay (${minDate} to ${maxDate}).` });
                return;
            }
            const idLow = field.id.toLowerCase();
            if (idLow.includes('checkin') || idLow.includes('check_in') || idLow.includes('arrival') || idLow.includes('start')) {
                checkinDate = d;
                checkinFieldId = field.id;
            }
            if (idLow.includes('checkout') || idLow.includes('check_out') || idLow.includes('departure') || idLow.includes('end')) {
                checkoutDate = d;
                checkoutFieldId = field.id;
            }
        }

        if (_isPhoneField(field)) {
            // val already has country code prepended (e.g. "+919876543210")
            // Only do a minimal sanity check — LLM handles country-specific rules
            const localDigits = val.replace(/^\+\d{1,4}/, '').replace(/\D/g, '');
            if (localDigits.length < 7) {
                errors.push({ id: field.id, message: 'Please enter a valid phone number.' });
            }
        } else if (field.type === 'number') {
            const n = Number(val);
            if (isNaN(n) || n <= 0 || !Number.isInteger(n)) {
                errors.push({ id: field.id, message: 'Must be a positive whole number.' });
            }
        }

        if (field.type === 'email') {
            if (!val.includes('@') || val.indexOf('.', val.indexOf('@')) === -1) {
                errors.push({ id: field.id, message: 'Enter a valid email address.' });
            }
        }
    });

    if (checkinDate && checkoutDate && checkoutDate <= checkinDate && checkoutFieldId) {
        errors.push({ id: checkoutFieldId, message: 'Check-out must be after check-in.' });
    }

    return errors;
}

/** Escape a value for use inside an HTML attribute. */
function escapeAttr(str) {
    return String(str || '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

// ─────────────────────────────────────────────────────────────────────────────

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', init);
