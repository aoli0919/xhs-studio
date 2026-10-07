'use strict';
const $ = id => document.getElementById(id);
const STORE = 'xhs-workbench-drafts-v1';
const state = { mode: 'connecting', ready: false, healthy: false, drafts: new Map(), active: null, loggedIn: false, jobs: new Map(), timers: new Map(), deleted: new Set(), versions: new Map(), saved: new Map(), uploads: new Map(), prepared: null, resetDraft: null, resetting: false, publishing: false, preparing: false, accountRequest: 0 };
const clone = value => JSON.parse(JSON.stringify(value));
function message(text = '', kind = '') { $('editor-message').textContent = text; $('editor-message').className = 'message ' + kind; }
function notify(text) { $('notice').hidden = !text; $('notice').textContent = text; }
function current() { return state.drafts.get(state.active); }
function countTitle(text) { let n = 0; for (let i = 0; i < text.length; i++) n += text.charCodeAt(i) < 128 ? 1 : 2; return Math.ceil(n / 2); }
function normalize(d) { return { id: d.id || crypto.randomUUID(), title: String(d.title || ''), content: String(d.content || ''), tags: Array.isArray(d.tags) ? d.tags.map(String) : [], images: Array.isArray(d.images) ? d.images.map(i => ({ id: i.id, name: String(i.name || '图片'), url: String(i.url || '') })) : [], schedule_at: d.schedule_at || '', visibility: d.visibility === 'private' ? 'private' : 'public', is_original: !!d.is_original }; }
async function api(path, body, method) {
  const controller = new AbortController();
  const timeout = path === '/api/publish' ? 360000 : path === '/api/login/qrcode' || path === '/api/login/status' ? 120000 : 45000;
  const timer = setTimeout(() => controller.abort(), timeout);
  let response;
  try { response = await fetch(path, { method: method || (body === undefined ? 'GET' : 'POST'), credentials: 'same-origin', headers: body === undefined ? {} : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal }); }
  catch (e) { const err = new Error(e.name === 'AbortError' ? '请求超时' : '无法连接本地服务，请检查服务是否运行'); err.unknown = true; throw err; }
  finally { clearTimeout(timer); }
  let result;
  try { result = await response.json(); } catch { const err = new Error('服务返回了无法识别的响应'); err.unknown = true; throw err; }
  if (!response.ok || result.success !== true) { const error = new Error(typeof result.error === 'string' ? result.error : '请求失败（' + response.status + '）'); error.code = result.code; throw error; }
  return result.data;
}
function controls() {
  const exists = !!current();
  for (const id of ['title', 'content', 'tags', 'images', 'schedule', 'visibility', 'original', 'save-draft', 'delete-draft']) $(id).disabled = !exists || !state.ready;
  $('new-draft').disabled = !state.ready;
  $('prepare').disabled = !state.ready || !exists || state.mode !== 'local' || !state.healthy || !state.loggedIn || state.preparing || state.publishing || state.resetting || (state.uploads.get(state.active) || 0) > 0;
  $('reset-publish').disabled = !state.ready || state.resetting || state.publishing || state.preparing || state.mode !== 'local' || !state.healthy;
  $('login').disabled = $('refresh-login').disabled = state.mode !== 'local' || !state.healthy;
}
function renderList() {
  $('draft-list').replaceChildren();
  for (const d of state.drafts.values()) {
    const b = document.createElement('button'); b.type = 'button'; b.className = 'draft-item' + (d.id === state.active ? ' active' : ''); b.setAttribute('aria-current', d.id === state.active ? 'true' : 'false');
    const t = document.createElement('span'); t.className = 'draft-title'; t.textContent = d.title || '未命名草稿';
    const s = document.createElement('span'); s.className = 'draft-summary'; s.textContent = d.images.length + ' 张图片 · ' + (d.content.trim().slice(0, 28) || '开始记录你的想法');
    b.append(t, s); b.addEventListener('click', () => selectDraft(d.id)); $('draft-list').append(b);
  }
}
function localDate(iso) { if (!iso) return ''; const date = new Date(iso); if (Number.isNaN(date.getTime())) return ''; const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000); return local.toISOString().slice(0, 16); }
function renderImages() {
  $('image-list').replaceChildren(); const d = current(); $('image-count').textContent = (d?.images.length || 0) + ' 张';
  for (const img of d?.images || []) {
    const card = document.createElement('div'); card.className = 'image-card'; const preview = document.createElement('img'); preview.alt = img.name; preview.src = img.url;
    const label = document.createElement('p'); label.textContent = img.name; label.title = img.name;
    const remove = document.createElement('button'); remove.type = 'button'; remove.disabled = !state.ready; remove.textContent = '移除'; remove.setAttribute('aria-label', '移除图片 ' + img.name);
    remove.addEventListener('click', () => { if (!state.ready) return; d.images = d.images.filter(i => i.id !== img.id); changed(d.id); renderImages(); }); card.append(preview, label, remove); $('image-list').append(card);
  }
}
function saveStatus(id, text) { if (state.active === id) $('save-status').textContent = text; }
function renderEditor() {
  const d = current(); $('title').value = d?.title || ''; $('content').value = d?.content || ''; $('tags').value = d?.tags.join(' ') || ''; $('schedule').value = localDate(d?.schedule_at); $('visibility').value = d?.visibility || 'public'; $('original').checked = !!d?.is_original;
  $('title-count').textContent = countTitle(d?.title || '') + ' / 20 字'; $('images').value = ''; renderImages(); controls();
  $('save-status').textContent = !d ? '请选择或新建草稿' : (state.saved.get(d.id) === state.versions.get(d.id) ? (state.mode === 'demo' ? '已保存到此浏览器' : '已保存') : '有待保存的修改');
}
function selectDraft(id) { if (id === state.active) return; if (current()) saveDraft(state.active).catch(() => {}); state.active = id; message(); renderList(); renderEditor(); }
function changed(id) { state.versions.set(id, (state.versions.get(id) || 0) + 1); saveStatus(id, '等待自动保存'); renderList(); clearTimeout(state.timers.get(id)); state.timers.set(id, setTimeout(() => saveDraft(id).catch(() => {}), 700)); }
function storeDemo() { localStorage.setItem(STORE, JSON.stringify([...state.drafts.values()])); }
async function saveDraft(id) {
  clearTimeout(state.timers.get(id));
  if (!state.ready || !state.drafts.has(id) || state.deleted.has(id)) return;
  if (state.jobs.has(id)) { await state.jobs.get(id); if (!state.deleted.has(id) && state.saved.get(id) !== state.versions.get(id)) return saveDraft(id); return; }
  if (state.saved.get(id) === state.versions.get(id)) return;
  const version = state.versions.get(id); const snapshot = clone(state.drafts.get(id)); saveStatus(id, '正在保存…');
  const job = (async () => {
    try { if (state.mode === 'demo') storeDemo(); else await api('/api/drafts', snapshot); state.saved.set(id, version); saveStatus(id, state.versions.get(id) === version ? (state.mode === 'demo' ? '已保存到此浏览器' : '已保存') : '有待保存的修改'); }
    catch (e) { saveStatus(id, '保存失败 · 内容保留在编辑器'); if (state.active === id) message('保存失败：' + e.message + '。当前编辑内容仍保留，可点击“立即保存”重试。', 'error'); throw e; }
  })(); state.jobs.set(id, job);
  try { await job; } finally { state.jobs.delete(id); }
  if (!state.deleted.has(id) && state.drafts.has(id) && state.saved.get(id) !== state.versions.get(id)) return saveDraft(id);
}
function newDraft() { if (!state.ready) return; const d = normalize({}); state.drafts.set(d.id, d); state.versions.set(d.id, 1); selectDraft(d.id); changed(d.id); }
async function deleteDraft() {
  const d = current(); if (!state.ready || !d || !window.confirm('删除“' + (d.title || '未命名草稿') + '”？此操作不能撤销。')) return;
  const id = d.id; state.deleted.add(id); clearTimeout(state.timers.get(id)); $('delete-draft').disabled = true;
  try { if (state.jobs.has(id)) await state.jobs.get(id).catch(() => {}); if (state.mode === 'local') await api('/api/drafts/' + encodeURIComponent(id), undefined, 'DELETE');
    if (state.mode === 'demo') localStorage.setItem(STORE, JSON.stringify([...state.drafts.values()].filter(item => item.id !== id))); state.drafts.delete(id); state.versions.delete(id); state.saved.delete(id); if (state.active === id) { state.active = state.drafts.keys().next().value || null; renderEditor(); message('草稿已删除'); } renderList();
  } catch (e) { state.deleted.delete(id); if (state.active === id) message('删除失败：' + e.message, 'error'); } finally { controls(); }
}
function fileData(file) { return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(new Error('无法读取图片')); reader.readAsDataURL(file); }); }
async function uploadImages(event) {
  const id = state.active; const files = [...event.target.files]; event.target.value = ''; if (!state.ready || !id) return;
  state.uploads.set(id, (state.uploads.get(id) || 0) + files.length); controls();
  for (const file of files) {
    try { if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error('仅支持 JPEG、PNG 和 WebP 图片'); if (file.size > 10 * 1024 * 1024) throw new Error('图片不能超过 10 MiB');
      const data = await fileData(file); const asset = state.mode === 'demo' ? { id: crypto.randomUUID(), name: file.name, url: data } : (await api('/api/assets', { filename: file.name, base64: data.split(',')[1] })).asset;
      if (!asset || !asset.url) throw new Error('服务未返回有效图片'); if (state.deleted.has(id) || !state.drafts.has(id)) continue;
      state.drafts.get(id).images.push({ id: asset.id, name: asset.name || file.name, url: asset.url }); changed(id); if (state.active === id) { renderImages(); message('图片已添加'); }
    } catch (e) { if (state.active === id) message(file.name + '：' + e.message, 'error'); }
    finally { state.uploads.set(id, Math.max(0, (state.uploads.get(id) || 1) - 1)); controls(); }
  }
}
async function refreshLogin() {
  const seq = ++state.accountRequest; $('account-status').textContent = '正在获取账号状态…';
  try { const data = await api('/api/login/status'); if (seq !== state.accountRequest) return; state.loggedIn = !!data.is_logged_in; $('account-status').textContent = state.loggedIn ? '已登录 · ' + (data.username || '小红书账号') : '未登录，请扫码登录'; if (state.loggedIn) $('qr-panel').hidden = true; }
  catch (e) { if (seq !== state.accountRequest) return; state.loggedIn = false; $('account-status').textContent = '获取失败：' + e.message; } finally { controls(); }
}
async function login() {
  $('reset-publish').disabled = !state.ready || state.resetting || state.publishing || state.preparing || state.mode !== 'local' || !state.healthy;
  $('login').disabled = true;
  try { const data = await api('/api/login/qrcode'); if (data.is_logged_in) { await refreshLogin(); return; } if (typeof data.img !== 'string' || !data.img.startsWith('data:image/')) throw new Error('服务未返回有效二维码'); $('qr-image').src = data.img; $('qr-panel').hidden = false; $('qr-info').textContent = data.timeout ? '二维码有效期：' + String(data.timeout) : '二维码过期后请重新获取'; }
  catch (e) { $('account-status').textContent = '获取二维码失败：' + e.message; } finally { controls(); }
}
function detail(label, value) { const dt = document.createElement('dt'); dt.textContent = label; const dd = document.createElement('dd'); dd.textContent = value; $('confirm-details').append(dt, dd); }
function rememberUnknown(draft) { state.resetDraft = clone(draft); $('recovery-panel').hidden = false; $('recovery-info').textContent = '“' + (draft.title || '未命名草稿') + '”的发布结果未知。请先到小红书核对；只有确认这份内容没有发布，才解除锁定。'; }
function clearRecovery() { state.resetDraft = null; $('recovery-panel').hidden = true; }
async function resetPublish() {
  if (!state.ready || !state.resetDraft || state.resetting || state.publishing || state.preparing) return;
  const snapshot = clone(state.resetDraft);
  if (!window.confirm('请先到小红书确认这篇笔记没有发布。只有确认未发布才解除锁定，避免重复笔记。\n\n笔记：' + (snapshot.title || '未命名草稿'))) return;
  state.resetting = true; controls(); $('reset-publish').textContent = '正在解除锁定…';
  try { await api('/api/publish/reset', { ...snapshot, confirmed_not_published: true }); clearRecovery(); message('发布锁定已解除，请重新检查并预览后再发布。', 'success'); }
  catch (e) { if (e.code === 'ALREADY_SUBMITTED') { clearRecovery(); message('这份内容已提交，请到小红书核对审核与可见状态。', 'error'); } else message('解除锁定失败：' + e.message + '。锁定仍保留，请核对后手动重试。', 'error'); }
  finally { state.resetting = false; $('reset-publish').textContent = '已核对，未发布，解除锁定'; controls(); }
}
async function prepare() {
  if (!state.ready || state.preparing || state.publishing || state.resetting || !current()) return;
  const id = state.active; const snapshot = clone(current()); const version = state.versions.get(id); state.preparing = true; controls(); message('正在检查发布内容…');
  try { await saveDraft(id); const data = await api('/api/prepare', snapshot); if (state.active !== id || state.versions.get(id) !== version) { message('草稿已变化，请重新检查后发布。'); return; } if (!data.confirmation_id) throw new Error('服务未返回发布确认凭证');
    state.prepared = { draft: snapshot, confirmation_id: data.confirmation_id }; $('confirm-title').textContent = snapshot.title || '未命名草稿'; $('confirm-details').replaceChildren(); detail('账号', $('account-status').textContent); detail('正文', snapshot.content); detail('图片', snapshot.images.length + ' 张'); detail('标签', snapshot.tags.join(' · ') || '未填写'); detail('可见范围', snapshot.visibility === 'private' ? '仅自己可见' : '公开'); detail('发布时间', snapshot.schedule_at ? new Date(snapshot.schedule_at).toLocaleString() : '立即发布'); detail('声明原创', snapshot.is_original ? '是' : '否');
    const warnings = Array.isArray(data.warnings) ? data.warnings : []; $('confirm-warnings').replaceChildren(); $('confirm-warnings').hidden = !warnings.length; for (const warning of warnings) { const p = document.createElement('p'); p.textContent = String(warning); $('confirm-warnings').append(p); }
    $('publish-message').textContent = ''; $('confirm-publish').disabled = false; $('confirm-publish').textContent = '确认发布'; $('confirm-dialog').showModal(); message();
  } catch (e) { if (e.code === 'PUBLISH_RESULT_UNKNOWN') { rememberUnknown(snapshot); message('这份内容的发布结果未知，请先到小红书核对，勿重复提交。', 'error'); } else if (e.code === 'ALREADY_SUBMITTED') { clearRecovery(); message('这份内容已提交，请到小红书核对审核与可见状态。', 'error'); } else message('发布检查失败：' + e.message, 'error'); } finally { state.preparing = false; controls(); }
}
async function publish() {
  if (!state.prepared || state.publishing) return;
  const prepared = state.prepared; state.prepared = null; state.publishing = true; controls(); $('confirm-publish').disabled = true; $('confirm-publish').textContent = '正在提交…'; $('publish-message').textContent = '正在提交，请勿重复操作。'; $('publish-message').className = 'message';
  try { await api('/api/publish', { ...prepared.draft, confirmed: true, confirmation_id: prepared.confirmation_id }); clearRecovery(); $('publish-message').textContent = '已提交，请到小红书核对审核与可见状态'; $('publish-message').className = 'message success'; $('confirm-publish').textContent = '已提交'; }
  catch (e) { if (e.code === 'PUBLISH_RESULT_UNKNOWN') rememberUnknown(prepared.draft); else if (e.code === 'ALREADY_SUBMITTED') clearRecovery(); $('publish-message').textContent = e.code === 'ALREADY_SUBMITTED' ? '这份内容已提交，请到小红书核对审核与可见状态。' : e.code === 'PUBLISH_RESULT_UNKNOWN' ? '结果未知，请到小红书核对，勿重复提交。确认未发布后，可返回编辑解除锁定。' : e.unknown ? '结果未知，请到小红书核对，勿重复提交。' : '发布失败：' + e.message + '。需要再次发布时，请返回编辑并重新检查。'; $('publish-message').className = 'message error'; $('confirm-publish').textContent = e.unknown ? '结果待核对' : '提交失败'; }
  finally { state.publishing = false; controls(); }
}
function closeConfirm() { if (state.publishing) return; $('confirm-dialog').close(); state.prepared = null; }
async function init() {
  controls();
  let initialized = false;
  try { const response = await fetch('/api/health', { credentials: 'same-origin', signal: AbortSignal.timeout(10000) }); const text = await response.text(); let health; try { health = JSON.parse(text); } catch { health = null; }
    if (response.status === 404 || !health) { state.mode = 'demo'; $('mode').textContent = '演示模式 · 仅此浏览器'; notify('演示模式：草稿与图片保存在此浏览器，不会连接账号或发布。清理浏览器数据会移除草稿。'); $('account-status').textContent = '演示模式不提供登录与发布'; const data = JSON.parse(localStorage.getItem(STORE) || '[]'); if (!Array.isArray(data)) throw new Error('本地草稿格式无效'); for (const item of data) { const d = normalize(item); state.drafts.set(d.id, d); state.versions.set(d.id, 0); state.saved.set(d.id, 0); } }
    else { if (!response.ok || health.success !== true) throw new Error(typeof health.error === 'string' ? health.error : '本地服务状态异常'); state.mode = 'local'; state.healthy = !!health.data?.backend_healthy; $('mode').textContent = state.healthy ? '本地服务已连接' : '本地模式 · 发布后端离线'; notify(state.healthy ? '' : '草稿工作台可用，但小红书发布后端未连接。启动后端后刷新此页面，再登录发布。'); const data = await api('/api/drafts'); for (const item of data.drafts || []) { const d = normalize(item); state.drafts.set(d.id, d); state.versions.set(d.id, 0); state.saved.set(d.id, 0); } if (state.healthy) refreshLogin(); else $('account-status').textContent = '发布后端离线，账号状态未知'; }
    initialized = true; state.ready = true;
  } catch (e) { if (state.mode === 'connecting') state.mode = 'local'; $('mode').textContent = state.mode === 'demo' ? '演示模式 · 草稿读取异常' : '本地服务连接异常'; state.ready = false; notify('草稿读取失败：' + e.message + '。编辑和保存已锁定，请先备份并修复草稿数据或检查服务，再刷新页面。'); }
  state.active = state.drafts.keys().next().value || null; renderList(); renderEditor(); if (!state.active && initialized) newDraft();
}
$('reset-publish').addEventListener('click', resetPublish); $('new-draft').addEventListener('click', newDraft); $('delete-draft').addEventListener('click', deleteDraft); $('save-draft').addEventListener('click', () => saveDraft(state.active).then(() => message('保存完成', 'success')).catch(() => {})); $('images').addEventListener('change', uploadImages);
for (const id of ['title', 'content', 'tags', 'schedule', 'visibility', 'original']) $(id).addEventListener(id === 'visibility' || id === 'original' || id === 'schedule' ? 'change' : 'input', () => {
  const d = current(); if (!state.ready || !d) return; d.title = $('title').value; d.content = $('content').value; d.tags = [...new Set($('tags').value.split(/[\s,，]+/).map(t => t.replace(/^#+/, '')).filter(Boolean))]; const date = $('schedule').value ? new Date($('schedule').value) : null; d.schedule_at = date && !Number.isNaN(date.getTime()) ? date.toISOString() : ''; d.visibility = $('visibility').value; d.is_original = $('original').checked; $('title-count').textContent = countTitle(d.title) + ' / 20 字'; changed(d.id);
});
$('editor-form').addEventListener('submit', e => e.preventDefault()); $('refresh-login').addEventListener('click', refreshLogin); $('login').addEventListener('click', login); $('prepare').addEventListener('click', prepare); $('confirm-publish').addEventListener('click', publish); $('cancel-confirm').addEventListener('click', closeConfirm); $('close-confirm').addEventListener('click', closeConfirm); $('confirm-dialog').addEventListener('cancel', e => { if (state.publishing) e.preventDefault(); else state.prepared = null; });
window.addEventListener('beforeunload', e => { if ([...state.drafts.keys()].some(id => !state.deleted.has(id) && state.saved.get(id) !== state.versions.get(id)) || state.publishing || [...state.uploads.values()].some(n => n > 0)) { e.preventDefault(); e.returnValue = ''; } });
init();
