// HUD test in a private headless browser. Opens reply boxes and cancels — never sends.
import { chromium } from 'playwright';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 980 } });
const errors = [];
page.on('pageerror', e => errors.push('pageerror: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
await page.goto('http://localhost:8730/?noboot=1'); await page.waitForTimeout(3000);
const r = {};
const cleanup = [];  // anything this test ticks is un-ticked at the end no matter what
r.title = await page.locator('.mark b').innerText();
r.tasks = await page.locator('.task').count();
r.waiting = await page.locator('.who').count();
r.events = await page.locator('.ev').count();
r.nodes = await page.locator('.node').evaluateAll(n => n.map(x => x.innerText.replace(/\n/g, ' ')));
r.date = await page.locator('#date').innerText();
r.pill = await page.locator('#awake').innerText();
r.usage = await page.locator('#usage').innerText();
r.tabs = await page.locator('#tabs button').allInnerTexts();
r.emptyPanelsHidden = await page.evaluate(() => ['#memPanel', '#donePanel'].every(s => { const el = document.querySelector(s);
  const empty = el.querySelectorAll('li').length === 0; return !empty || getComputedStyle(el).display === 'none'; }));
// tabs switch
for (const t of r.tabs) { await page.locator('#tabs button', { hasText: t }).click(); await page.waitForTimeout(150); }
r.tabSwitchVisible = await page.evaluate(() => ['#transcript', '#convo', '#evening'].filter(s => !document.querySelector(s).hidden).length === 1);
// reply boxes (open, check, cancel — never send)
const kinds = {};
for (const b of await page.locator('[data-reply]').all()) {
  const label = await b.innerText(); if (kinds[label]) continue;
  await b.click(); await page.waitForTimeout(150);
  const box = page.locator('.reply').first();
  kinds[label] = await box.locator('[data-send]').evaluateAll(x => x.map(y => y.innerText));
  await box.locator('[data-cancel]').click();
}
r.replyBoxes = kinds;
r.boxesLeftOpen = await page.locator('.reply').count();
if (await page.locator('[data-reply]').count()) {
  await page.locator('[data-reply]').first().click();
  await page.locator('.reply [data-send]').first().click();
  r.emptyReplyNotSent = (await page.locator('.reply .msg').innerText()) === '';
  await page.locator('.reply [data-cancel]').click();
}
// tick -> appears in Done today -> undo from the Done list
if (await page.locator('.task').count()) {
  const card = page.locator('.task').first();
  const title = await card.locator('.ttl').innerText();
  await card.locator('.chk').click(); await page.waitForTimeout(2500);
  r.tick = await card.evaluate(c => c.classList.contains('done'));
  r.tickShowsInDone = (await page.locator('#done').innerText()).includes(title);
  const undo = page.locator(`#done [data-undo="${title.replace(/"/g, '\\"')}"]`);
  if (await undo.count()) { await undo.click(); await page.waitForTimeout(2500); }
  r.undoRemovesFromDone = !(await page.locator('#done').innerText()).includes(title);
  r.undoUnticksCard = !(await card.evaluate(c => c.classList.contains('done')));
}
try {
// waiting row: Done hides it, shows in Done today, undo brings it back
if (await page.locator('[data-wdone]').count()) {
  const before = await page.locator('.who').count();
  const t = await page.locator('[data-wdone]').first().getAttribute('data-wdone');
  cleanup.push(t);
  await page.locator('[data-wdone]').first().click(); await page.waitForTimeout(2500);
  r.waitDoneHides = (await page.locator('.who').count()) === before - 1;
  r.waitDoneInDoneList = (await page.locator('#done').innerText()).includes(t.slice(0, 20));
  const undo = page.locator(`#done [data-undo="${t.replace(/"/g, '\\"')}"]`);
  if (await undo.count()) await undo.click();
  await page.waitForTimeout(2500);
  r.waitUndoRestores = (await page.locator('.who').count()) === before;
}
} catch (e) { r.waitError = String(e).slice(0, 200); }
// shortcuts overlay
await page.keyboard.press('?'); r.helpOpens = await page.locator('#keys').isVisible();
await page.keyboard.press('Escape'); r.helpCloses = !(await page.locator('#keys').isVisible());
await page.locator('[data-reply]').first().click().catch(() => {});
await page.locator('.reply textarea').type('r l ').catch(() => {});
r.typingShortcutsIgnored = !(await page.locator('#scanBtn').isDisabled());
await page.locator('.reply [data-cancel]').click().catch(() => {});
for (const w of [1280, 900, 400]) {
  await page.setViewportSize({ width: w, height: 900 }); await page.waitForTimeout(300);
  r['overflow_' + w] = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 2);
}
await page.setViewportSize({ width: 1600, height: 980 }); await page.waitForTimeout(300);
for (const t of cleanup) await page.request.post('http://localhost:8730/api/undone', { data: { title: t } }).catch(() => {});
await page.screenshot({ path: process.argv[2] });
r.errors = errors;
console.log(JSON.stringify(r, null, 2));
await browser.close();
