// Chat Application State
const state = {
    sessionId: null,
    hotelCode: 'DEFAULT',
    phase: 'pre_booking',
    messages: [],
    isLoading: false,
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
};

// Initialize
function init() {
    generateSessionId();
    setupEventListeners();
}

// Generate unique session ID
function generateSessionId() {
    state.sessionId = 'session_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
    updateSessionInfo();
}

// Setup event listeners
function setupEventListeners() {
    // Form submit
    elements.chatForm.addEventListener('submit', handleSubmit);

    // Hotel selection
    elements.hotelCode.addEventListener('change', (e) => {
        state.hotelCode = e.target.value;
    });
    elements.journeyPhase.addEventListener('change', (e) => {
        state.phase = e.target.value || 'pre_booking';
    });

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
            sendMessage(message);
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

// Handle form submit
async function handleSubmit(e) {
    e.preventDefault();
    const message = elements.messageInput.value.trim();
    if (!message || state.isLoading) return;

    elements.messageInput.value = '';
    await sendMessage(message);
}

// Send message to API
async function sendMessage(message) {
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
                metadata: {
                    phase: state.phase,
                },
            }),
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();

        // Remove loading indicator
        loadingEl.remove();

        // Add bot response to UI
        addMessageToUI('assistant', data.message, data);

        // Update session info
        updateSessionInfoFromResponse(data);

        // Use orchestrator suggestions if present (fully context-aware),
        // otherwise fall back to the suggestions endpoint with session context
        if (data.suggested_actions && data.suggested_actions.length > 0) {
            showSuggestedActions(data.suggested_actions);
        } else {
            fetchAndShowSuggestions(data.message, message, state.sessionId);
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

// Add message to UI
function addMessageToUI(role, content, data = null) {
    // Remove welcome message if exists
    const welcome = elements.chatMessages.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    const messageEl = document.createElement('div');
    messageEl.className = `message ${role}`;

    let html = `<div class="message-content">${escapeHtml(content)}</div>`;

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

    state.messages.push({ role, content, data });
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

// Fetch context-aware suggestions from LLM and display them (fallback only)
async function fetchAndShowSuggestions(lastBotMessage, userMessage, sessionId) {
    elements.suggestedActions.innerHTML = '';
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
            }),
        });
        if (!response.ok) return;
        const data = await response.json();
        showSuggestedActions(data.suggestions);
    } catch (e) {
        // silently fail — suggestions are optional UI
    }
}

// Show suggested actions
function showSuggestedActions(actions) {
    elements.suggestedActions.innerHTML = '';

    if (!actions || actions.length === 0) return;

    actions.forEach(action => {
        const btn = document.createElement('button');
        btn.className = 'suggested-action';
        btn.textContent = action;
        btn.addEventListener('click', () => {
            sendMessage(action);
        });
        elements.suggestedActions.appendChild(btn);
    });
}

// Clear chat
function clearChat() {
    elements.chatMessages.innerHTML = `
        <div class="welcome-message">
            <p>👋 Start a conversation to test the bot.</p>
            <p>Use the quick test buttons on the left or type your own messages.</p>
        </div>
    `;
    state.messages = [];
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
            addMessageToUI('assistant', '🔄 Session state has been reset. How can I help you?');
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
            <div style="margin-bottom: 15px; padding: 10px; background: #f1f5f9; border-radius: 8px;">
                <strong>Session ID:</strong> ${data.session_id}<br>
                <strong>State:</strong> ${data.state}<br>
                <strong>Hotel:</strong> ${data.hotel_code}<br>
                <strong>Created:</strong> ${new Date(data.created_at).toLocaleString()}
            </div>
            <hr style="margin: 15px 0; border: none; border-top: 1px solid #e2e8f0;">
        `;

        if (data.messages.length === 0) {
            html += '<p style="color: #64748b;">No messages yet</p>';
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
            elements.ticketsContent.innerHTML = '<p style="color:#64748b;">No tickets yet.</p>';
            return;
        }

        let html = `<p style="margin-bottom:12px;color:#64748b;">${tickets.length} ticket(s) found — newest first</p>`;
        tickets.forEach(t => {
            const created = t.created_at ? new Date(t.created_at).toLocaleString() : '-';
            const status = t.status || 'open';
            const statusColor = status === 'open' ? '#16a34a' : '#64748b';
            const fields = Object.entries(t)
                .filter(([k]) => !['created_at', 'updated_at', 'ticket_id', 'status', 'id'].includes(k))
                .map(([k, v]) => {
                    if (!v && v !== 0) return '';
                    return `<div><span style="color:#64748b;min-width:130px;display:inline-block;">${k}:</span> ${escapeHtml(String(v))}</div>`;
                })
                .filter(Boolean)
                .join('');

            html += `
                <div style="border:1px solid #e2e8f0;border-radius:8px;padding:14px;margin-bottom:12px;background:#fff;">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                        <strong style="font-size:15px;">🎫 ${escapeHtml(t.ticket_id || t.id || '-')}</strong>
                        <span style="background:${statusColor};color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">${escapeHtml(status)}</span>
                    </div>
                    <div style="font-size:13px;line-height:1.7;">${fields}</div>
                    <div style="font-size:11px;color:#94a3b8;margin-top:6px;">Created: ${created}</div>
                </div>
            `;
        });

        elements.ticketsContent.innerHTML = html;
    } catch (error) {
        elements.ticketsContent.innerHTML = `<p style="color:#ef4444;">Error loading tickets: ${error.message}</p>`;
    }
}

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', init);
