// Portfolio screenshots from FICTIONAL demo data (no real messages). Usage: node demo_shots.mjs <outdir>
import { chromium } from 'playwright';
const out = process.argv[2];
const now = new Date(); const at = (h, m) => { const d = new Date(now); d.setHours(h, m, 0, 0); return d.toISOString(); };
const BASE = new Date(at(8, 31)).getTime();
const ago = mins => new Date(BASE - mins * 60000).toISOString();
const local = mins => { const d = new Date(BASE - mins * 60000); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19); };
const idx = {
  'wa:1': { source: 'whatsapp', from: 'Happy Hogan', title: 'Happy Hogan', text: 'Happy: Car is ready at 9. Do you want the Audi or the R8?', time: ago(35), link: 'whatsapp://', flags: ['awaiting-reply'] },
  'wa:2': { source: 'whatsapp', from: 'Peter Parker', title: 'Peter Parker', text: 'Peter: Mr Stark, can I come by the lab after school?', time: ago(80), link: 'whatsapp://', flags: ['awaiting-reply'] },
  'gm:1': { source: 'gmail', from: 'Pepper Potts <pepper@stark.example>', title: 'Board deck — need your sign-off by 11', text: 'Need your sign-off on slides 4-9 before the 11am board call.', time: ago(50), link: 'https://mail.google.com', flags: ['unread', 'important'] },
  'gm:2': { source: 'gmail', from: 'Stark Expo Ops', title: 'Stage rig failed safety check', text: 'The main stage rig failed this morning’s safety inspection. Re-test booked for 2pm.', time: ago(140), link: 'https://mail.google.com', flags: ['unread'] },
  'sl:1': { source: 'slack', from: 'rhodey', title: 'DM', text: 'Need the suit telemetry from Friday before the review.', time: ago(25), link: 'https://slack.com', flags: ['dm', 'awaiting-reply'] },
  'im:1': { source: 'imessage', from: 'Natasha', title: 'Natasha', text: 'Dinner Thursday still on?', time: ago(200), link: 'messages://', flags: ['awaiting-reply'] },
};
const cal = [
  { id: 'c1', title: 'Board call — Q3 numbers', time: at(11, 0), end: at(11, 45), attendees: 8, meet: 'https://meet.google.com', rsvp: 'accepted' },
  { id: 'c2', title: 'Stark Expo stage re-test', time: at(14, 0), end: at(15, 0), location: 'Flushing Meadows' },
  { id: 'c3', title: 'Lab time — Mark 50 thrusters', time: at(16, 30), end: at(18, 30) },
  { id: 'c4', title: 'Dinner with Pepper', time: at(20, 0), end: at(21, 30), location: 'Malibu' },
];
const briefing = {
  generated_at: ago(3), since: ago(1440), engine: 'claude', took_s: 41, user_name: 'Tony',
  weather: { place: 'Malibu', now_c: 24, now: 'clear', high_c: 27, low_c: 18, rain_pct: 0, day: 'clear' },
  sources: { gmail: { status: 'ok', count: 14 }, slack: { status: 'ok', count: 3 }, whatsapp: { status: 'ok', count: 9 }, calendar: { status: 'ok', count: 4 }, imessage: { status: 'ok', count: 2 } },
  calendar: cal, index: idx,
  brief: {
    headline: 'Board sign-off before eleven, a stage re-test at two, and three people waiting on you.',
    spoken_briefing: "Good morning, sir. It's half past eight on Monday. Clear skies over Malibu, twenty-four degrees, rising to twenty-seven. Pepper needs your sign-off on slides four to nine before the eleven o'clock board call, so that comes first. The Expo's main stage rig failed its safety check; the re-test is at two and you may wish to be there. Rhodey wants Friday's suit telemetry before his review, and Happy is asking which car you'd like at nine. I'd suggest the Audi. That's everything worth your attention, sir.",
    priorities: [
      { title: 'Sign off the board deck', why: 'Pepper needs slides 4–9 approved before the 11am board call.', action: 'Review slides 4–9 and reply to Pepper', urgency: 'critical', deadline: 'by 11:00', source_ids: ['gm:1'] },
      { title: 'Stage rig failed safety check', why: 'Re-test booked for 2pm; the Expo opens Thursday.', action: 'Attend the 2pm re-test or delegate', urgency: 'high', deadline: '2:00 pm', source_ids: ['gm:2'] },
      { title: 'Send Rhodey the suit telemetry', why: 'He needs Friday’s flight data before his review.', action: 'Export Friday’s telemetry and DM it', urgency: 'high', source_ids: ['sl:1'] },
      { title: 'Confirm dinner with Natasha', why: 'She asked if Thursday is still on.', action: 'Reply yes or suggest another day', urgency: 'medium', source_ids: ['im:1'] },
    ],
    waiting_on_you: [
      { who: 'Happy Hogan', summary: 'Car ready at 9 — Audi or the R8?', source_ids: ['wa:1'] },
      { who: 'Rhodey', summary: 'Needs Friday’s suit telemetry before his review.', source_ids: ['sl:1'] },
      { who: 'Peter Parker', summary: 'Asked to come by the lab after school.', source_ids: ['wa:2'] },
    ],
    schedule_notes: [{ event_id: 'c1', note: 'Pepper needs your deck sign-off before this.' }],
    fyi: [{ title: 'Stark stock up 2.1% pre-market', source_ids: [] }, { title: 'Two press requests about the Expo, both routine', source_ids: [] }, { title: 'Promos and newsletters skipped: 23', source_ids: [] }],
    noise_summary: '23 promos, newsletters and OTPs skipped.',
  },
};
const T = s => BASE / 1000 - s;
const overview = (extra = {}) => ({
  ears: { mode: 'idle', awake_until: BASE / 1000 + 480, locked: false, paused: false },
  usage: { five_hour: { pct: 31, resets: BASE / 1000 + 3 * 3600 } },
  timers: [{ label: 'tea', due: BASE / 1000 + 252 }],
  memory: ['Pepper handles finance and approves anything over $1M.', 'Prefers calls before noon.', 'Happy drives on weekdays.'],
  did: [{ at: local(40), kind: 'reminder', title: 'Call Rhodey about the review' }, { at: local(15), kind: 'event_created', title: 'Dinner with Pepper' }],
  done: [{ title: 'Reply to the Expo press team', at: local(60), ids: [] }],
  conversation: [
    { at: T(160), you: "What's most urgent?", arthur: "Pepper's deck, sir. She needs slides four to nine signed off before the eleven o'clock board call.", action: 'answer' },
    { at: T(120), you: 'Book dinner with Pepper at eight tonight.', arthur: "Added to your calendar for eight o'clock, sir.", action: 'event_create' },
    { at: T(40), you: "Tell Happy I'll take the Audi.", arthur: "That's Happy Hogan. I'll text him: \"I'll take the Audi, thanks.\" Shall I send it?", action: 'whatsapp_send' },
  ],
  evening: null, confirm: null, asking: false, ...extra,
});
const fulfil = body => route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

const browser = await chromium.launch();
async function shot(name, size, ov, tab, setup) {
  const p = await browser.newPage({ viewport: size, deviceScaleFactor: 2 });
  await p.clock.setFixedTime(new Date(at(8, 31)));
  await p.route('**/api/briefing', fulfil(briefing));
  await p.route('**/api/overview', fulfil(ov));
  await p.route('**/api/done', fulfil({}));
  await p.route('**/api/status', fulfil({ phase: 'ready', generated_at: briefing.generated_at }));
  await p.route('**/api/speaking', fulfil({ speaking: false }));
  await p.goto('http://localhost:8730/?noboot=1'); await p.waitForTimeout(2500);
  if (tab) { await p.locator(`[data-tab=${tab}]`).click(); await p.waitForTimeout(400); }
  if (setup) await setup(p);
  await p.screenshot({ path: `${out}/${name}.png` });
  await p.close();
}
await shot('dashboard', { width: 1600, height: 980 }, overview(), 'brief');
await shot('conversation', { width: 1600, height: 980 }, overview({ confirm: { heard: "Tell Happy I'll take the Audi.", what: "I'll take the Audi, thanks.", question: "That's Happy Hogan. I'll text him. Shall I send it?", draft: false } }), 'convo');
await shot('mobile', { width: 430, height: 1500 }, overview(), 'brief');
await browser.close();
console.log('ok');
