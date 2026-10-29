// Local browser smoke measurement, not a statistically controlled benchmark.
import { mkdir, readFile, writeFile } from 'node:fs/promises';

const label = process.argv[2];
if (!['before', 'after', 'disabled'].includes(label)) throw new Error('Expected before, after or disabled');
const { PB_PLAYWRIGHT_MODULE, PB_REVIEW_PROJECT_ID, PB_REVIEW_QUESTIONS_FILE } = process.env;
if (!PB_PLAYWRIGHT_MODULE || !/^[a-f0-9-]{36}$/i.test(PB_REVIEW_PROJECT_ID || '') || !PB_REVIEW_QUESTIONS_FILE) {
  throw new Error('Set PB_PLAYWRIGHT_MODULE, PB_REVIEW_PROJECT_ID (UUID), and PB_REVIEW_QUESTIONS_FILE (local JSON string array).');
}
const questions = JSON.parse(await readFile(PB_REVIEW_QUESTIONS_FILE, 'utf8'));
if (!Array.isArray(questions) || !questions.length || questions.some(q => typeof q !== 'string' || !q.trim())) throw new Error('Invalid questions');
const { chromium } = await import(PB_PLAYWRIGHT_MODULE);
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
const measurements = [];
try {
  for (const question of questions) {
    const page = await browser.newPage();
    await page.goto('http://localhost:8000/');
    await page.locator(`#project-select option[value="${PB_REVIEW_PROJECT_ID}"]`).waitFor({ state: 'attached' });
    await page.selectOption('#project-select', PB_REVIEW_PROJECT_ID);
    await page.fill('#question', question);
    const responsePromise = page.waitForResponse(r => r.url().endsWith('/api/ask') && r.request().method() === 'POST', { timeout: 90000 });
    const started = performance.now();
    await page.locator('#ask-form button').click();
    const response = await responsePromise;
    if (!response.ok()) {
      const error = await response.json();
      const detail = String(error.detail || '').replace(/sk-[A-Za-z0-9_-]+/g, '[redacted]');
      throw new Error(`Ask failed: ${response.status()} ${detail}`);
    }
    const payload = await response.json();
    await page.waitForFunction(() => !document.querySelector('#ask-form button').disabled && document.querySelector('.message.assistant .bubble')?.textContent.length > 0, { timeout: 90000 });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const elapsedMs = Math.round(performance.now() - started);
    const answer = await page.locator('.message.assistant .bubble').innerText();
    if (answer.startsWith('오류:')) throw new Error(answer);
    const displayedTime = await page.locator('.response-time').count() ? await page.locator('.response-time').innerText() : null;
    measurements.push({ question, elapsedMs, displayedTime, answer, payload });
    console.log(JSON.stringify({ label, question, elapsedMs, displayedTime, traceId: payload.trace_id, citations: payload.citations?.length }));
    await page.close();
  }
} finally {
  await browser.close();
  await mkdir('data/performance-review/20261030', { recursive: true });
  await writeFile(`data/performance-review/20261030/${label}.json`, JSON.stringify(measurements, null, 2));
}
