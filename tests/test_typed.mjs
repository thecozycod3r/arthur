// Typed commands end-to-end in a real browser against live Arthur. The send case is CANCELLED on screen.
import { chromium } from 'playwright';
const browser = await chromium.launch();
const p = await browser.newPage({ viewport: { width: 1600, height: 980 } });
const errors = []; p.on('pageerror', e => errors.push(e.message));
await p.goto('http://localhost:8730/?noboot=1'); await p.waitForTimeout(2500);
const r = {};
const turns = () => p.locator('.turn').count();
const answered = async q => p.evaluate(q => [...document.querySelectorAll('.turn')].some(t =>
  t.querySelector('.you')?.innerText.toLowerCase().includes(q.toLowerCase()) && t.querySelector('.him')?.innerText.trim().length > 3), q);
const lastFor = async q => p.evaluate(q => { const t = [...document.querySelectorAll('.turn')].reverse().find(t =>
  t.querySelector('.you')?.innerText.toLowerCase().includes(q.toLowerCase())); return t?.querySelector('.him')?.innerText || ''; }, q);
async function ask(text) {
  const before = await turns();
  await p.keyboard.press('/');                       // shortcut focuses the box
  r.slashFocuses = await p.evaluate(() => document.activeElement?.id === 'askInput');
  await p.keyboard.type(text); await p.keyboard.press('Enter');
  return before;
}
// 1. a question
let before = await ask("what's the weather like tonight?");
let t0 = Date.now();
while (Date.now() - t0 < 60000 && !(await answered('weather like tonight'))) await p.waitForTimeout(500);
r.questionAnswered = await answered('weather like tonight');
r.questionSecs = Math.round((Date.now() - t0) / 1000);
r.answer = (await lastFor('weather like tonight')).slice(0, 120);
r.switchedToConversation = await p.locator('#tabs [aria-selected=true]').innerText();
// 2. a reply that needs approval -> card appears -> Cancel
before = await ask('reply to the Supabase email saying thanks, got it');
t0 = Date.now();
while (Date.now() - t0 < 45000 && !(await p.locator('#confirmCard').isVisible())) await p.waitForTimeout(500);
r.confirmCardShown = await p.locator('#confirmCard').isVisible();
r.confirmQuestion = (await p.locator('#confirmQ').innerText()).slice(0, 120);
r.confirmWhat = (await p.locator('#confirmWhat').innerText()).slice(0, 120);
r.draftButtonForEmail = await p.locator('#confirmDraft').isVisible();
await p.locator('[data-answer=no]').click();
t0 = Date.now();
while (Date.now() - t0 < 30000 && !(await answered('saying thanks, got it'))) await p.waitForTimeout(500);
await p.waitForTimeout(2500);
r.cancelledCardGone = !(await p.locator('#confirmCard').isVisible());
r.afterCancel = (await lastFor('saying thanks, got it')).slice(0, 160);
r.errors = errors;
await p.screenshot({ path: process.argv[2] });
console.log(JSON.stringify(r, null, 2));
await browser.close();
