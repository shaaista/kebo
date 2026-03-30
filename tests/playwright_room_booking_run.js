const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const BASE_URL = 'http://127.0.0.1:8000';
const PROPERTY_CODE = 'playwright_roomtest';
const SERVICE_ID = 'room_booking_request';
const SERVICE_NAME = 'Room Booking Request';

let serverProcess = null;

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function nowPlusDays(days) {
  const dt = new Date();
  dt.setDate(dt.getDate() + days);
  const yyyy = dt.getFullYear();
  const mm = String(dt.getMonth() + 1).padStart(2, '0');
  const dd = String(dt.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

async function waitForServer(url, timeoutMs = 120000) {
  const start = Date.now();
  while (true) {
    try {
      const res = await fetch(url);
      if (res.ok) return;
    } catch (err) {
      // keep retrying
    }
    if (Date.now() - start > timeoutMs) {
      throw new Error(`Server did not become ready within ${timeoutMs}ms: ${url}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

async function startServer() {
  const env = { ...process.env, DEBUG: 'true' };
  serverProcess = spawn(
    'python',
    ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', '8000'],
    {
      cwd: process.cwd(),
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    }
  );
  serverProcess.stdout.on('data', (buf) => process.stdout.write(`[server] ${buf.toString()}`));
  serverProcess.stderr.on('data', (buf) => process.stderr.write(`[server-err] ${buf.toString()}`));
  await waitForServer(`${BASE_URL}/health`);
}

async function stopServer() {
  if (!serverProcess || serverProcess.killed) return;
  serverProcess.kill('SIGTERM');
  await new Promise((resolve) => setTimeout(resolve, 1500));
  if (!serverProcess.killed) {
    serverProcess.kill('SIGKILL');
  }
}

async function openTab(page, tabName) {
  await page.click(`.tab[data-tab="${tabName}"]`);
  await page.waitForTimeout(300);
}

async function sendChatMessage(page, message) {
  const chatRespPromise = page.waitForResponse(
    (resp) => resp.url().includes('/api/chat/message') && resp.request().method() === 'POST',
    { timeout: 180000 }
  );
  await page.fill('#message-input', message);
  await page.press('#message-input', 'Enter');
  const chatResp = await chatRespPromise;
  let payload = {};
  try {
    payload = await chatResp.json();
  } catch (err) {
    payload = {};
  }
  await page.waitForTimeout(1200);
  const assistantMessages = page.locator('#chat-messages .message.assistant .message-content');
  const count = await assistantMessages.count();
  if (count > 0) {
    return (await assistantMessages.nth(count - 1).innerText()).trim();
  }
  return String(payload.display_message || payload.message || '').trim();
}

async function run() {
  const summary = {
    property_code: PROPERTY_CODE,
    service_id: SERVICE_ID,
    kb_upload_ok: false,
    rag_reindex_ok: false,
    pull_from_kb_reason: '',
    extracted_kb_chars: 0,
    service_saved: false,
    service_persisted_after_reload: false,
    chat_info_response_chars: 0,
    inline_form_visible: false,
    inline_form_submit_success: false,
    inline_form_submit_error: '',
  };

  let browser;
  try {
    await startServer();
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext();
    const page = await context.newPage();

    await page.goto(`${BASE_URL}/admin`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#cfg-property-code', { timeout: 60000 });

    await page.fill('#cfg-property-code', PROPERTY_CODE);
    await page.fill('#cfg-business-name', 'Playwright Test Property');
    await page.fill('#cfg-city', 'Mumbai');
    await page.fill('#cfg-bot-name', 'PlayBot');
    await page.selectOption('#cfg-business-type', 'hotel');
    await page.fill('#cfg-location', 'Mumbai, India');
    await page.fill('#cfg-contact-email', 'playwright@test.com');
    await page.fill('#cfg-contact-phone', '+919999999999');
    await page.fill('#cfg-website', 'https://example.com');
    await page.fill('#cfg-address', 'Playwright Address');
    await page.fill('#cfg-welcome-message', 'Hello from Playwright');

    const saveBusinessResp = page.waitForResponse(
      (resp) =>
        resp.url().includes('/admin/api/config/onboarding/business') &&
        resp.request().method() === 'PUT',
      { timeout: 120000 }
    );
    await page.evaluate(() => saveBusinessInfo());
    const saveBusiness = await saveBusinessResp;
    assert(saveBusiness.ok(), 'Business info save failed');

    await openTab(page, 'rag');
    await page.fill('#rag-tenant-id', PROPERTY_CODE);

    const kbFilePath = path.join(process.cwd(), 'ROHL_Test_property.txt');
    await page.setInputFiles('#rag-upload-files', kbFilePath);

    const uploadResp = page.waitForResponse(
      (resp) =>
        resp.url().includes('/admin/api/rag/upload') &&
        resp.request().method() === 'POST',
      { timeout: 180000 }
    );
    await page.click('#rag-upload-btn');
    const upload = await uploadResp;
    const uploadJson = await upload.json();
    summary.kb_upload_ok = upload.ok() && Number(uploadJson.uploaded_count || 0) > 0;
    assert(summary.kb_upload_ok, `KB upload failed: ${JSON.stringify(uploadJson)}`);

    const reindexResp = page.waitForResponse(
      (resp) =>
        resp.url().includes('/admin/api/rag/reindex') &&
        resp.request().method() === 'POST',
      { timeout: 240000 }
    );
    await page.click('#rag-reindex-btn');
    const reindex = await reindexResp;
    const reindexJson = await reindex.json();
    summary.rag_reindex_ok = reindex.ok() && Number(reindexJson.chunks_indexed || 0) > 0;
    assert(summary.rag_reindex_ok, `RAG reindex failed: ${JSON.stringify(reindexJson)}`);

    await openTab(page, 'services');
    await page.click('button:has-text("+ Add Service")');
    await page.waitForSelector('#add-service-modal.active', { timeout: 30000 });

    await page.fill('#new-svc-id', SERVICE_ID);
    await page.fill('#new-svc-name', SERVICE_NAME);
    await page.selectOption('#new-svc-type', 'service');
    await page.fill('#new-svc-user-intent', 'Guests can explore rooms and request the staff to book it.');
    await page.fill('#new-svc-desc', 'Allows guests to explore available rooms and submit a booking request to the staff.');
    await page.selectOption('#new-svc-ticketing-mode', 'form');
    await page.fill('#new-svc-ticketing-conditions', 'Create a booking ticket when guest confirms room type and dates, and provides contact details.');
    await page.fill('#svc-trigger-field-id', 'room_type');
    await page.fill('#svc-trigger-field-label', 'Room Type');
    await page.fill('#svc-trigger-field-desc', 'Which room type guest confirms');
    await page.fill('#svc-pre-form-instructions', 'Confirm room type before showing form');

    await page.evaluate(() => {
      resetFormBuilder('svc');
      addFormField('svc', { id: 'guest_name', label: 'Guest Name', type: 'text', required: true, validation_prompt: '' });
      addFormField('svc', { id: 'phone', label: 'Phone Number', type: 'tel', required: true, validation_prompt: '' });
      addFormField('svc', { id: 'checkin', label: 'Check In Date', type: 'date', required: true, validation_prompt: '' });
      addFormField('svc', { id: 'checkout', label: 'Check Out Date', type: 'date', required: true, validation_prompt: '' });
      addFormField('svc', { id: 'request', label: 'Special Requests', type: 'textarea', required: false, validation_prompt: '' });
    });
    const formRows = await page.locator('#svc-form-fields-list .form-builder-field-row').count();
    assert(formRows === 5, `Expected 5 form fields, found ${formRows}`);

    const pullResp = page.waitForResponse(
      (resp) =>
        resp.url().includes('/admin/api/config/service-kb/preview-extract') &&
        resp.request().method() === 'POST',
      { timeout: 240000 }
    );
    await page.click('#pull-kb-btn');
    const pull = await pullResp;
    const pullJson = await pull.json();
    summary.pull_from_kb_reason = String(pullJson.reason || '');
    const kbText = String(await page.inputValue('#new-svc-kb-knowledge'));
    summary.extracted_kb_chars = kbText.trim().length;
    assert(summary.extracted_kb_chars > 0, `Pull from KB returned empty extraction: ${JSON.stringify(pullJson)}`);

    const createSvcResp = page.waitForResponse(
      (resp) =>
        /\/admin\/api\/config\/services$/.test(new URL(resp.url()).pathname) &&
        resp.request().method() === 'POST',
      { timeout: 120000 }
    );
    await page.click('#save-service-btn');
    const createSvc = await createSvcResp;
    assert(createSvc.ok(), 'Create service API call failed');
    summary.service_saved = true;

    // Ensure service is mapped to active chat phase.
    const mapPhaseResp = await page.evaluate(async (sid) => {
      const res = await fetch(`/admin/api/config/services/${encodeURIComponent(sid)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phase_id: 'pre_booking', ticketing_enabled: true }),
      });
      return { ok: res.ok, status: res.status };
    }, SERVICE_ID);
    assert(mapPhaseResp.ok, `Failed to map service to pre_booking phase: ${JSON.stringify(mapPhaseResp)}`);

    await page.waitForSelector('#services-list .service-item', { timeout: 60000 });
    const svcVisible = await page.locator('#services-list .service-item', { hasText: SERVICE_ID }).first().isVisible();
    assert(svcVisible, 'Saved service not visible in services list');

    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#cfg-property-code', { timeout: 60000 });
    await page.fill('#cfg-property-code', PROPERTY_CODE);
    await page.evaluate((code) => {
      if (typeof syncPropertyScopeUi === 'function') {
        syncPropertyScopeUi(code);
      }
    }, PROPERTY_CODE);
    await openTab(page, 'services');
    await page.evaluate(async () => {
      if (typeof loadServices === 'function') {
        await loadServices();
      }
    });
    await page.waitForTimeout(1200);

    const serviceCard = page.locator('#services-list .service-item', { hasText: SERVICE_ID }).first();
    assert(await serviceCard.isVisible(), 'Service card not visible after reload');
    await serviceCard.getByRole('button', { name: 'Edit' }).click();
    await page.waitForSelector('#add-service-modal.active', { timeout: 30000 });

    const persistedKb = String(await page.inputValue('#new-svc-kb-knowledge')).trim();
    const persistedMode = await page.inputValue('#new-svc-ticketing-mode');
    const persistedFieldCount = await page.locator('#svc-form-fields-list .form-builder-field-row').count();
    summary.service_persisted_after_reload =
      persistedKb.length > 0 &&
      persistedMode === 'form' &&
      persistedFieldCount >= 4;
    assert(
      summary.service_persisted_after_reload,
      `Service persistence check failed. mode=${persistedMode} fields=${persistedFieldCount} kbChars=${persistedKb.length}`
    );
    await page.click('#add-service-modal .modal-footer .btn.btn-outline');

    const chatPage = await context.newPage();
    await chatPage.goto(`${BASE_URL}/chat`, { waitUntil: 'domcontentloaded' });
    await chatPage.waitForSelector('#hotel-code', { timeout: 60000 });
    await chatPage.selectOption('#hotel-code', PROPERTY_CODE);
    await chatPage.selectOption('#journey-phase', 'pre_booking');
    await chatPage.waitForTimeout(1000);

    const infoReply = await sendChatMessage(chatPage, 'What room booking options do you have? Please share details.');
    summary.chat_info_response_chars = infoReply.length;
    assert(summary.chat_info_response_chars > 20, `Chat info response too short: "${infoReply}"`);

    await sendChatMessage(chatPage, 'I want to book a room');
    const formContainer = chatPage.locator('.inline-form-container').last();
    summary.inline_form_visible = await formContainer.isVisible({ timeout: 60000 }).catch(() => false);
    assert(summary.inline_form_visible, 'Inline booking form did not appear in chat');

    await formContainer.locator('[name="guest_name"]').fill('Playwright Guest');
    await formContainer.locator('[name="phone"]').fill('9999999999');
    await formContainer.locator('[name="checkin"]').fill(nowPlusDays(2));
    await formContainer.locator('[name="checkout"]').fill(nowPlusDays(4));
    await formContainer.locator('[name="request"]').fill('High floor, non-smoking');
    await formContainer.locator('.inline-form-submit').click();

    const successLocator = formContainer.locator('.inline-form-success');
    const errorLocator = formContainer.locator('.inline-form-errors');
    const submitResolved = await Promise.race([
      successLocator
        .waitFor({ state: 'visible', timeout: 120000 })
        .then(() => 'success')
        .catch(() => null),
      errorLocator
        .waitFor({ state: 'visible', timeout: 120000 })
        .then(() => 'error')
        .catch(() => null),
    ]);
    summary.inline_form_submit_success = submitResolved === 'success';
    if (submitResolved === 'error') {
      summary.inline_form_submit_error = String(await errorLocator.innerText()).trim();
    }

    console.log('PLAYWRIGHT_E2E_SUMMARY', JSON.stringify(summary, null, 2));
    await context.close();
  } catch (err) {
    console.error('PLAYWRIGHT_E2E_FAILED', err && err.stack ? err.stack : String(err));
    process.exitCode = 1;
  } finally {
    if (browser) {
      await browser.close().catch(() => {});
    }
    await stopServer();
  }
}

run();
