// Dashboard under bad conditions — answers are faked inside a private browser; real data untouched.
import { chromium } from 'playwright';
const B = 'http://localhost:8730/?noboot=1';
const browser = await chromium.launch();
const out = {};
async function scenario(name, routes, check) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = []; let alerted = false;
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', d => { alerted = true; d.dismiss(); });
  for (const [pattern, handler] of routes) await page.route(pattern, handler);
  await page.goto(B); await page.waitForTimeout(3500);
  out[name] = { ...(await check(page)), jsErrors: errors, scriptRan: alerted || await page.evaluate(() => !!window.__pwned) };
  await page.close();
}
const json = body => route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

await scenario('fresh install (no briefing yet)', [['**/api/briefing', json({ empty: true })]], async p => ({
  emptyMessage: (await p.locator('#pri').innerText()).includes('Rescan'), headline: await p.locator('#headline').innerText() }));

await scenario('server down', [['**/api/**', r => r.abort()]], async p => ({
  offlineMessage: (await p.locator('#phase').innerText()).toLowerCase().includes('offline') }));

const evil = '<img src=x onerror="window.__pwned=1;alert(1)"><script>window.__pwned=1</script>';
await scenario('malicious text in messages', [['**/api/briefing', async route => {
  const real = await (await route.fetch()).json();
  real.brief.headline = 'Hi ' + evil;
  real.brief.priorities = [{ title: 'Evil ' + evil, why: evil, action: evil, urgency: 'critical', source_ids: ['x'] }];
  real.brief.waiting_on_you = [{ who: evil, summary: evil, source_ids: ['x'] }];
  real.index = { ...real.index, x: { source: 'whatsapp', from: evil, title: evil, text: evil, time: new Date().toISOString(), link: 'javascript:window.__pwned=1', flags: [] } };
  real.calendar = [{ id: 'c', title: evil, time: new Date().toISOString(), end: new Date().toISOString(), location: evil, meet: 'javascript:alert(1)' }];
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(real) });
}]], async p => {
  await p.locator('a.src').first().click({ timeout: 1000 }).catch(() => {});
  return { shownAsText: (await p.locator('.task .ttl').first().innerText()).includes('<img'),
           javascriptLinksBlocked: await p.evaluate(() => ![...document.querySelectorAll('a')].some(a => a.href.startsWith('javascript:'))) };
});

await scenario('huge day (60 priorities, 40 events)', [['**/api/briefing', async route => {
  const real = await (await route.fetch()).json();
  real.brief.priorities = Array.from({ length: 60 }, (_, i) => ({ title: `Task ${i} ` + 'very long title '.repeat(8), why: 'why '.repeat(40), action: 'do it', urgency: ['critical', 'high', 'medium'][i % 3], source_ids: [] }));
  const t = Date.now();
  real.calendar = Array.from({ length: 40 }, (_, i) => ({ id: 'e' + i, title: `Meeting ${i}`, time: new Date(t + i * 18e5).toISOString(), end: new Date(t + i * 18e5 + 18e5).toISOString() }));
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(real) });
}]], async p => ({ tasks: await p.locator('.task').count(), events: await p.locator('.ev').count(),
  pageDoesNotScroll: await p.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 2) }));

console.log(JSON.stringify(out, null, 2));
await browser.close();
