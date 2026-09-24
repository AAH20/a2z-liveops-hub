const $ = id => document.getElementById(id);
const notice = message => { $('notice').textContent = message; };
const key = () => sessionStorage.getItem('liveops-role-key') || '';
async function api(path, body) {
  const options = {headers: {'Authorization': `Bearer ${key()}`}};
  if (body !== undefined) { options.method = 'POST'; options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function el(tag, className, value) { const node = document.createElement(tag); if (className) node.className = className; if (value !== undefined) node.textContent = String(value); return node; }
function action(text, handler) { const button = el('button', '', text); button.addEventListener('click', handler); return button; }
async function run(path, payload) {
  try { notice('Working…'); const result = await api(path, payload); notice(`${result.status || result.outcome || 'Done'} · ${result.draft_id || ''}`); await refresh(); }
  catch (error) { notice(error.message); await refresh().catch(() => {}); }
}
function renderDrafts(rows) {
  const target = $('drafts'); target.replaceChildren();
  if (!rows.length) { target.append(el('p', '', 'No drafts yet. Load the synthetic demo to inspect the flow.')); return; }
  for (const row of rows) {
    const card = el('article', 'card'); card.append(el('h3', '', `Ticket #${row.ticket_id}`));
    card.append(el('div', 'meta', `Draft ${row.draft_id} · ${row.locale} · ${row.article_id}`));
    card.append(el('div', 'status', row.status + (row.is_demo ? ' · SYNTHETIC' : '') + (row.outcome ? ` · ${row.outcome}` : '')));
    card.append(el('div', 'answer', row.answer));
    const source = el('div', 'meta', `Knowledge source: ${row.source_url} · stamp ${row.source_stamp}`); card.append(source);
    const actions = el('div', 'actions');
    if (row.status === 'PENDING_REVIEW') { actions.append(action('Approve', () => run('/api/review', {draft_id:row.draft_id,approve:true})), action('Reject', () => run('/api/review', {draft_id:row.draft_id,approve:false}))); }
    if (row.status === 'APPROVED_PENDING_SEND' && !row.is_demo) actions.append(action('Send approved reply', () => { if (window.confirm('Send this exact approved reply to Zendesk?')) run('/api/send', {draft_id:row.draft_id}); }));
    if (row.status === 'SENT_UNVERIFIED') for (const outcome of ['ACCEPTED','REWORK','UNRESOLVED']) actions.append(action(`Declare ${outcome}`, () => run('/api/outcome', {draft_id:row.draft_id,status:outcome})));
    if (row.status.includes('UNCERTAIN')) card.append(el('p', 'status', 'Stop. Reconcile the installed app and Zendesk state before any further action.'));
    card.append(actions); target.append(card);
  }
}
function renderMetrics(summary) {
  const target = $('metrics'); target.replaceChildren();
  for (const [label, value] of Object.entries({...summary.draft_states, ...summary.operator_declared_outcomes})) {
    const box=el('div','metric'); box.append(el('strong','',value),el('span','',label)); target.append(box);
  }
  const box=el('div','metric'); box.append(el('strong','','—'),el('span','','Verified customer resolutions')); target.append(box);
}
async function refresh() {
  const [drafts, summary, events] = await Promise.all([api('/api/drafts'), api('/api/summary'), api('/api/events')]);
  renderDrafts(drafts); renderMetrics(summary); $('events').replaceChildren(...events.map(event => el('div','event',`${event.at} · ${event.kind} · ${event.actor} · ${event.draft_id || 'no draft'}`)));
}
$('saveKey').addEventListener('click', async () => { sessionStorage.setItem('liveops-role-key', $('key').value); $('key').value=''; try { await refresh(); notice('Access key accepted for this session.'); } catch { notice('Key was not accepted.'); } });
$('clearKey').addEventListener('click', () => { sessionStorage.removeItem('liveops-role-key'); $('key').value=''; $('drafts').replaceChildren(); $('metrics').replaceChildren(); $('events').replaceChildren(); notice('Key cleared.'); });
$('refresh').addEventListener('click', () => refresh().catch(error => notice(error.message)));
$('import').addEventListener('click', () => run('/api/import', {ticket_id:Number($('ticket').value),locale:$('locale').value}));
$('demo').addEventListener('click', () => run('/api/demo', {locale:$('locale').value}));
if (key()) refresh().catch(() => notice('Enter a valid role key.'));
