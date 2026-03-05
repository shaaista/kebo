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
    messageCount: document.getElementById('message-count'),
    lastIntent: document.getElementById('last-intent'),
    confidence: document.getElementById('confidence'),
    suggestedActions: document.getElementById('suggested-actions'),
    newSessionBtn: document.getElementById('new-session-btn'),
    resetBtn: document.getElementById('reset-btn'),
    viewHistoryBtn: document.getElementById('view-history-btn'),
    hotelCode: document.getElementById('hotel-code'),
    journeyPhase: document.getElementById('journey-phase'),
    historyModal: document.getElementById('history-modal'),
    historyContent: document.getElementById('history-content'),
    closeModal: document.getElementById('close-modal'),
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

        // Show suggested actions
        showSuggestedActions(data.suggested_actions);

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

    if (data && role === 'assistant') {
        const intentLabel = resolveDisplayIntent(data);
        const confidence = Number.isFinite(data.confidence) ? data.confidence : 0;
        html += `
            <div class="message-meta">
                <span class="intent">${intentLabel || 'unknown'}</span>
                <span class="confidence ${getConfidenceClass(confidence)}">${(confidence * 100).toFixed(0)}%</span>
            </div>
        `;
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
    elements.messageCount.textContent = data.metadata?.message_count || state.messages.length;
    elements.lastIntent.textContent = resolveDisplayIntent(data) || '-';

    if (data.confidence) {
        const pct = (data.confidence * 100).toFixed(0);
        elements.confidence.textContent = `${pct}%`;
        elements.confidence.className = `value ${getConfidenceClass(data.confidence)}`;
    }
}

function resolveDisplayIntent(data) {
    const apiIntent = String(data?.intent || '').trim();
    const metadata = data?.metadata || {};
    const rawIntent = String(
        metadata?.full_kb_raw_intent
        || metadata?.entities?.raw_intent
        || metadata?.raw_intent
        || ''
    ).trim().toLowerCase();
    if (rawIntent && ['room_booking', 'spa_booking', 'table_booking', 'order_food'].includes(rawIntent)) {
        return rawIntent;
    }
    return apiIntent || 'unknown';
}

// Update session info display
function updateSessionInfo() {
    elements.sessionId.textContent = state.sessionId.substring(0, 15) + '...';
    elements.sessionState.textContent = 'idle';
    elements.sessionState.className = 'value state-badge idle';
    elements.messageCount.textContent = '0';
    elements.lastIntent.textContent = '-';
    elements.confidence.textContent = '-';
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

// Utility: Get confidence class
function getConfidenceClass(confidence) {
    if (confidence >= 0.7) return 'high';
    if (confidence >= 0.4) return 'medium';
    return 'low';
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

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', init);
