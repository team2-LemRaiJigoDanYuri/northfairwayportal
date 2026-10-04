document.addEventListener('DOMContentLoaded', () => {
    const sidebar = document.getElementById('sidebar');
    const sidebarToggle = document.getElementById('sidebarToggle');
    const desktopSidebarToggle = document.getElementById('desktopSidebarToggle');
    const sidebarBackdrop = document.getElementById('sidebarBackdrop');
    const navItems = document.querySelectorAll('.sidebar-nav .nav-item[data-tab]');
    const tabContents = document.querySelectorAll('.tab-content');
    const pageTitle = document.getElementById('pageTitle');

    const setSidebarOpen = (open) => {
        if (!sidebar) return;
        sidebar.classList.toggle('open', open);
        document.body.classList.toggle('sidebar-open', open);
        if (sidebarToggle) sidebarToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    };

    const syncDesktopToggle = () => {
        const collapsed = document.body.classList.contains('sidebar-collapsed');
        if (!desktopSidebarToggle) return;
        desktopSidebarToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        desktopSidebarToggle.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
        desktopSidebarToggle.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
        const icon = desktopSidebarToggle.querySelector('i');
        if (icon) icon.className = collapsed ? 'fa-solid fa-angles-right' : 'fa-solid fa-angles-left';
    };
    if (desktopSidebarToggle) desktopSidebarToggle.addEventListener('click', () => {
        if (window.innerWidth < 1024) return;
        const collapsed = document.body.classList.toggle('sidebar-collapsed');
        sessionStorage.setItem('nfhSidebarCollapsed', collapsed ? '1' : '0');
        syncDesktopToggle();
    });
    if (sidebarToggle) sidebarToggle.addEventListener('click', () => setSidebarOpen(!sidebar.classList.contains('open')));
    if (window.innerWidth >= 1024 && sessionStorage.getItem('nfhSidebarCollapsed') === '1') document.body.classList.add('sidebar-collapsed');
    syncDesktopToggle();
    if (sidebarBackdrop) sidebarBackdrop.addEventListener('click', () => setSidebarOpen(false));

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && window.innerWidth <= 992 && sidebar?.classList.contains('open')) {
            setSidebarOpen(false);
            sidebarToggle?.focus();
        }
    });

    navItems.forEach((item, index) => {
        item.style.setProperty('--nav-order', index);
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const target = item.getAttribute('data-tab');
            navItems.forEach(i => i.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));
            item.classList.add('active');
            const tc = document.getElementById(target);
            if (tc) { tc.classList.add('active'); tc.scrollTop = 0; }
            if (pageTitle) pageTitle.textContent = item.querySelector('span').textContent;
            if (window.innerWidth <= 992) setSidebarOpen(false);
        });
    });

    window.addEventListener('resize', () => {
        if (window.innerWidth >= 1024) setSidebarOpen(false);
    }, { passive: true });

    document.querySelectorAll('[data-activity-time]').forEach(el => {
        const raw = (el.dataset.activityTime || '').trim();
        const parsed = new Date(raw.replace(' ', 'T'));
        if (!Number.isNaN(parsed.getTime())) el.textContent = parsed.toLocaleString('en-US', {month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit'}).replace(',', ' •');
    });

    requestAnimationFrame(() => document.body.classList.add('navigation-ready'));

    const logoutBtn = document.getElementById('logoutBtn');
    if (logoutBtn) logoutBtn.addEventListener('click', () => openModal('logoutModal'));
});

function openModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.add('show');
}

function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.remove('show');
}

// Lightweight role-aware live metrics. Keeps the existing Flask/MySQL architecture.
async function refreshLiveMetrics(){
  try{
    const res=await fetch('/api/live-metrics',{headers:{'Accept':'application/json'},cache:'no-store'});
    if(!res.ok)return; const data=await res.json(); if(data.status!=='success')return;
    document.querySelectorAll('[data-metric]').forEach(el=>{
      const key=el.dataset.metric; if(!(key in data.metrics))return;
      const raw=data.metrics[key] ?? 0;
      const next=el.dataset.currency==='true' ? `₱${Number(raw||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}` : String(raw);
      if(el.classList.contains('nav-count-badge')) el.hidden=Number(raw||0) <= 0;
      if(el.textContent.trim()!==next){el.textContent=next;el.classList.remove('metric-updated');void el.offsetWidth;el.classList.add('metric-updated');}
    });
  }catch(e){console.debug('Live metrics unavailable',e);}
}
refreshLiveMetrics(); setInterval(refreshLiveMetrics,10000);


// Professional in-system confirmations and toast feedback for Admin actions.
let adminConfirmResolve=null, adminConfirmTrigger=null;
function showToast(message,type='info',timeout=4200){const c=document.getElementById('toastContainer');if(!c||!message)return;const t=document.createElement('div');t.className=`system-toast toast-${type}`;const icon={success:'fa-circle-check',error:'fa-circle-xmark',warning:'fa-triangle-exclamation',info:'fa-circle-info'}[type]||'fa-circle-info';t.innerHTML=`<span class="toast-icon" aria-hidden="true"><i class="fa-solid ${icon}"></i></span><div class="toast-message"></div><button class="toast-close" type="button" aria-label="Dismiss">×</button>`;t.querySelector('.toast-message').textContent=message;t.querySelector('.toast-close').onclick=()=>dismissToast(t);c.appendChild(t);requestAnimationFrame(()=>t.classList.add('show'));setTimeout(()=>dismissToast(t),timeout)}
function dismissToast(t){if(!t||t.dataset.closing)return;t.dataset.closing='1';t.classList.remove('show');setTimeout(()=>t.remove(),220)}
function showAdminConfirm({title,message,confirmText='Confirm',type='primary',context=[]}){const m=document.getElementById('systemConfirmModal');if(!m)return Promise.resolve(true);adminConfirmTrigger=document.activeElement;document.getElementById('systemConfirmTitle').textContent=title;document.getElementById('systemConfirmMessage').textContent=message;const x=document.getElementById('systemConfirmContext');x.innerHTML='';context.forEach(i=>{if(!i?.value)return;const r=document.createElement('div');r.className='confirm-context-row';r.innerHTML='<span></span><strong></strong>';r.children[0].textContent=i.label;r.children[1].textContent=i.value;x.appendChild(r)});x.hidden=!x.children.length;const b=document.getElementById('systemConfirmButton');b.textContent=confirmText;b.className=`btn confirm-primary confirm-${type}`;m.classList.add('show');setTimeout(()=>b.focus(),0);return new Promise(res=>adminConfirmResolve=res)}
function closeAdminConfirm(result=false){document.getElementById('systemConfirmModal')?.classList.remove('show');const r=adminConfirmResolve;adminConfirmResolve=null;if(r)r(result);setTimeout(()=>adminConfirmTrigger?.focus?.(),0)}
function initAdminFeedback(){(window.SERVER_FLASH_MESSAGES||[]).forEach(i=>showToast(i.message,i.category==='success'?'success':i.category==='error'?'error':i.category==='warning'?'warning':'info'));document.getElementById('systemConfirmCancel')?.addEventListener('click',()=>closeAdminConfirm(false));document.getElementById('systemConfirmButton')?.addEventListener('click',()=>closeAdminConfirm(true));document.getElementById('systemConfirmModal')?.addEventListener('click',e=>{if(e.target===e.currentTarget)closeAdminConfirm(false)});document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.getElementById('systemConfirmModal')?.classList.contains('show'))closeAdminConfirm(false)});document.querySelectorAll('form[data-system-confirm]').forEach(form=>form.addEventListener('submit',async e=>{if(form.dataset.confirmed==='true')return;e.preventDefault();const submitter=e.submitter;const ok=await showAdminConfirm({title:form.dataset.confirmTitle||'Confirm Action',message:form.dataset.systemConfirm,confirmText:form.dataset.confirmText||submitter?.textContent?.trim()||'Confirm',type:form.dataset.confirmType||'primary',context:[{label:form.dataset.contextLabel||'',value:form.dataset.contextValue||''}]});if(!ok)return;form.dataset.confirmed='true';form.submit()}));}
document.addEventListener('DOMContentLoaded',initAdminFeedback);


document.addEventListener('click', (event) => {
    const jump = event.target.closest('[data-tab-jump]');
    if (!jump) return;
    event.preventDefault();
    const target = jump.getAttribute('data-tab-jump');
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.getElementById(target)?.classList.add('active');
    const jumpPageTitle = document.getElementById('pageTitle');
    document.querySelectorAll('.sidebar .nav-item[data-tab]').forEach(el => {
        const active = el.dataset.tab === target;
        el.classList.toggle('active', active);
        if (active && jumpPageTitle) jumpPageTitle.textContent = el.querySelector('span')?.textContent || 'Administrator';
    });
    window.NFHRecordFilters?.init?.();
    window.scrollTo({top:0, behavior:'smooth'});
});

function closeAccountMenus(){
  document.querySelectorAll('.account-dropdown').forEach(menu=>menu.setAttribute('hidden',''));
  document.querySelectorAll('.account-menu-button').forEach(btn=>btn.setAttribute('aria-expanded','false'));
}
function toggleAccountMenu(btn){
  const menu=btn.parentElement.querySelector('.account-dropdown');
  if(!menu)return;
  const opening=menu.hasAttribute('hidden');
  closeAccountMenus();
  if(opening){menu.removeAttribute('hidden');btn.setAttribute('aria-expanded','true');}
}
function openAccountProfile(){closeAccountMenus();openModal('accountProfileModal')}
document.addEventListener('click',e=>{if(!e.target.closest('.account-menu-wrap'))closeAccountMenus();});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){const open=document.querySelector('.account-menu-button[aria-expanded=\"true\"]');closeAccountMenus();open?.focus();}});


// Password controls: icon-only eye toggle and icon-only Caps Lock indicator.
(function initPasswordUsability(){
  const EYE_OPEN = '<svg class="eye-icon eye-open" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.7"/></svg>';
  const EYE_CLOSED = '<svg class="eye-icon eye-closed" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18"/><path d="M10.6 6.2A9.7 9.7 0 0 1 12 6c6 0 9.5 6 9.5 6a16 16 0 0 1-3.1 3.8M6.2 6.2C3.8 8 2.5 12 2.5 12s3.5 6 9.5 6a9.8 9.8 0 0 0 3.2-.5"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';
  function setup(){
    document.querySelectorAll('[data-toggle-password]').forEach(function(btn){
      if(btn.dataset.passwordReady) return;
      btn.dataset.passwordReady='1';
      btn.innerHTML=EYE_OPEN;
      btn.addEventListener('click', function(){
        var input=document.getElementById(btn.dataset.togglePassword);
        if(!input) return;
        var reveal=input.type==='password';
        input.type=reveal?'text':'password';
        btn.innerHTML=reveal?EYE_CLOSED:EYE_OPEN;
        btn.setAttribute('aria-label', reveal?'Hide password':'Show password');
        btn.title=reveal?'Hide password':'Show password';
        input.focus({preventScroll:true});
      });
    });
    document.querySelectorAll('.password-field input').forEach(function(input){
      if(input.dataset.capsReady) return;
      input.dataset.capsReady='1';
      var field=input.closest('.password-field');
      if(!field) return;
      var icon=document.createElement('span');
      icon.className='caps-lock-icon';
      icon.setAttribute('aria-label','Caps Lock is on');
      icon.title='Caps Lock is on';
      icon.textContent='⇪';
      icon.hidden=true;
      field.appendChild(icon);
      function caps(e){ if(e.getModifierState) icon.hidden=!e.getModifierState('CapsLock'); }
      input.addEventListener('keydown',caps);
      input.addEventListener('keyup',caps);
      input.addEventListener('focus',function(e){ if(e.getModifierState) caps(e); });
      input.addEventListener('blur',function(){ icon.hidden=true; });
    });
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',setup); else setup();
})();


// Service Type editor: uses the existing Admin save route but makes Edit explicit and reliable.
function openServiceEditor(button){
  const modal=document.getElementById('serviceTypeModal');
  const form=document.getElementById('serviceTypeForm');
  if(!modal||!form||!button)return;
  const title=document.getElementById('serviceTypeModalTitle');
  const submit=document.getElementById('serviceTypeSubmit');
  form.querySelector('[name="service_id"]').value=button.dataset.serviceId||'';
  form.querySelector('[name="service_name"]').value=button.dataset.serviceName||'';
  form.querySelector('[name="category"]').value=button.dataset.category||'Document Request';
  form.querySelector('[name="fee"]').value=button.dataset.fee||'0.00';
  form.querySelector('[name="initial_reviewer"]').value=button.dataset.reviewer||'Secretary';
  form.querySelector('[name="requires_executive"]').checked=button.dataset.executive==='1';
  form.querySelector('[name="requires_payment"]').checked=button.dataset.payment==='1';
  form.querySelector('[name="requires_dues_clearance"]').checked=button.dataset.dues==='1';
  form.querySelector('[name="purpose_required"]').checked=button.dataset.purpose==='1';
  form.querySelector('[name="release_role"]').value=button.dataset.releaseRole||'Secretary';
  form.querySelector('[name="form_template"]').value=button.dataset.formTemplate||'generic';
  form.querySelector('[name="description"]').value=button.dataset.description||'';
  if(title) title.textContent='Edit Service Type';
  if(submit) submit.textContent='Save Changes';
  openModal('serviceTypeModal');
}
function openNewService(){
  const form=document.getElementById('serviceTypeForm');
  if(!form)return;
  form.reset();
  form.querySelector('[name="service_id"]').value='';
  const exec=form.querySelector('[name="requires_executive"]');
  const pay=form.querySelector('[name="requires_payment"]');
  const dues=form.querySelector('[name="requires_dues_clearance"]');
  const purpose=form.querySelector('[name="purpose_required"]');
  if(exec)exec.checked=true; if(pay)pay.checked=true; if(dues)dues.checked=true; if(purpose)purpose.checked=true;
  const release=form.querySelector('[name="release_role"]'); if(release)release.value='Secretary';
  const template=form.querySelector('[name="form_template"]'); if(template)template.value='generic';
  const title=document.getElementById('serviceTypeModalTitle');
  const submit=document.getElementById('serviceTypeSubmit');
  if(title)title.textContent='Add Service Type';
  if(submit)submit.textContent='Add Service';
  openModal('serviceTypeModal');
}


async function nfhUploadAccountImage(input, endpoint, kind){
  const file=input?.files?.[0]; if(!file)return;
  const data=new FormData(); data.append('file',file);
  try{
    const res=await fetch(endpoint,{method:'POST',body:data}); const payload=await res.json();
    if(!res.ok||payload.status!=='success'){showToast(payload.message||`Unable to update ${kind}.`,'error');return;}
    showToast(payload.message||`${kind} updated.`,'success');
    if(kind==='profile picture'){
      const url=payload.image_url;
      const preview=document.getElementById('accountPhotoPreview'); if(preview)preview.innerHTML=`<img src="${url}?v=${Date.now()}" alt="Current profile picture">`;
      ['headerProfileImage','dropdownProfileImage'].forEach(id=>{let img=document.getElementById(id);if(img){img.src=`${url}?v=${Date.now()}`;}else{const parent=id.startsWith('header')?document.querySelector('.account-avatar'):document.querySelector('.account-dropdown-avatar');if(parent)parent.innerHTML=`<img src="${url}?v=${Date.now()}" alt="" class="account-avatar-img" id="${id}">`;}});
    }else{
      const preview=document.getElementById('accountESignPreview'); if(preview)preview.innerHTML=`<img src="${payload.preview_url}?v=${Date.now()}" alt="Current e-signature">`;
      const remove=document.getElementById('removeESignBtn'); if(remove)remove.hidden=false;
    }
  }catch(err){showToast(`Connection error while updating ${kind}.`,'error');}
  finally{input.value='';}
}
async function nfhRemoveESignature(){
  const ok=typeof showConfirmModal==='function'?await showConfirmModal({title:'Remove E-Signature',message:'Remove your current e-signature? Future documents that require your role will not show a signature until you upload a new one.',confirmText:'Remove',type:'danger'}):window.confirm('Remove current e-signature?');
  if(!ok)return;
  try{const res=await fetch('/api/account/esign',{method:'DELETE'});const data=await res.json();if(!res.ok||data.status!=='success'){showToast(data.message||'Unable to remove e-signature.','error');return;}showToast(data.message,'success');const preview=document.getElementById('accountESignPreview');if(preview)preview.innerHTML='<span class="esign-empty"><i class="fa-solid fa-signature"></i> No e-signature uploaded</span>';document.getElementById('removeESignBtn').hidden=true;}catch(e){showToast('Connection error while removing e-signature.','error');}
}
function initAccountMediaControls(){
  const profile=document.getElementById('officerProfilePhotoInput')||document.getElementById('adminProfilePhotoInput');
  const esign=document.getElementById('officerESignInput')||document.getElementById('adminESignInput');
  if(profile&&!profile.dataset.ready){profile.dataset.ready='1';profile.addEventListener('change',()=>nfhUploadAccountImage(profile,'/api/profile/photo','profile picture'));}
  if(esign&&!esign.dataset.ready){esign.dataset.ready='1';esign.addEventListener('change',()=>nfhUploadAccountImage(esign,'/api/account/esign','e-signature'));}
  const remove=document.getElementById('removeESignBtn');if(remove&&!remove.dataset.ready){remove.dataset.ready='1';remove.addEventListener('click',nfhRemoveESignature);}
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',initAccountMediaControls);else initAccountMediaControls();


(function initTemplateTokenEditor(){
  let activeTarget=null;
  function bind(){
    document.querySelectorAll('.template-token-target').forEach(el=>{if(el.dataset.tokenReady)return;el.dataset.tokenReady='1';['focus','click','keyup'].forEach(ev=>el.addEventListener(ev,()=>activeTarget=el));});
    document.querySelectorAll('[data-template-token]').forEach(btn=>{if(btn.dataset.tokenReady)return;btn.dataset.tokenReady='1';btn.addEventListener('click',()=>{const modal=btn.closest('.document-template-modal');let target=activeTarget&&modal?.contains(activeTarget)?activeTarget:modal?.querySelector('.template-body-editor');if(!target)return;const token=btn.dataset.templateToken||'';const start=target.selectionStart??target.value.length,end=target.selectionEnd??start;target.value=target.value.slice(0,start)+token+target.value.slice(end);target.focus();target.selectionStart=target.selectionEnd=start+token.length;});});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',bind);else bind();
})();

async function saveOfficerAdminPassword(event){
    event?.preventDefault();
    const current=document.getElementById('accountCurrentPassword')?.value||'';
    const next=document.getElementById('accountNewPassword')?.value||'';
    const confirm=document.getElementById('accountConfirmPassword')?.value||'';
    if(!current||!next||!confirm){showToast('Complete all password fields.','warning');return false;}
    if(next.length<8){showToast('New password must be at least 8 characters.','warning');return false;}
    if(next!==confirm){showToast('New password and confirmation do not match.','warning');return false;}
    try{
        const response=await fetch('/api/settings/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_password:current,new_password:next,confirm_password:confirm})});
        const result=await response.json();
        if(!response.ok||result.status!=='success'){showToast(result.message||'Unable to update password.','error');return false;}
        document.getElementById('accountSecurityForm')?.reset(); closeModal('accountSecurityModal'); showToast('Password changed successfully.','success');
    }catch(err){console.error(err);showToast('Unable to update password right now.','error');}
    return false;
}


// ---------------------------------------------------------------------------
// Final-defense Existing Data Import UI
// ---------------------------------------------------------------------------
(function initExistingDataImport(){
  const excelForm=document.getElementById('excelImportPreviewForm');
  const excelOut=document.getElementById('excelImportPreview');
  const sqlForm=document.getElementById('sqlImportPreviewForm');
  const sqlOut=document.getElementById('sqlImportPreview');
  if(!excelForm && !sqlForm) return;

  const escHtml=value=>String(value??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  const csrf=()=>document.querySelector('meta[name="csrf-token"]')?.content || '';

  function mappingMarkup(data){
    const options=data.headers.map(h=>`<option value="${escHtml(h)}">${escHtml(h)}</option>`).join('');
    return `<div class="import-mapping"><h3>Column Mapping</h3><p>Required fields are marked with *. Confirm the suggested mapping before importing.</p><div class="import-mapping-grid">${data.fields.map(f=>{
      const selected=data.mapping[f.name]||'';
      return `<label><span>${escHtml(f.name.replaceAll('_',' '))}${f.required?' *':''}</span><select class="form-input import-map-select" data-field="${escHtml(f.name)}"><option value="">Not mapped</option>${data.headers.map(h=>`<option value="${escHtml(h)}" ${h===selected?'selected':''}>${escHtml(h)}</option>`).join('')}</select></label>`;
    }).join('')}</div></div>`;
  }

  function previewTable(data){
    if(!data.preview?.length) return '<p class="import-empty">No data rows found.</p>';
    const heads=data.headers.map(h=>`<th>${escHtml(h)}</th>`).join('');
    const rows=data.preview.map(r=>`<tr>${data.headers.map(h=>`<td>${escHtml(r.values[h] ?? '')}</td>`).join('')}</tr>`).join('');
    return `<div class="table-responsive import-preview-table"><table class="data-table"><thead><tr>${heads}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  excelForm?.addEventListener('submit',async e=>{
    e.preventDefault();
    const btn=excelForm.querySelector('button[type="submit"]'); btn.disabled=true; btn.textContent='Reading workbook…';
    try{
      const fd=new FormData(excelForm);
      const res=await fetch('/api/admin/import/excel/preview',{method:'POST',body:fd,headers:{'Accept':'application/json','X-CSRF-Token':csrf()}});
      const data=await res.json();
      if(!res.ok||data.status!=='success'){showToast(data.message||'Unable to preview Excel import.','error');return;}
      excelOut.hidden=false;
      excelOut.dataset.token=data.token; excelOut.dataset.filename=data.filename; excelOut.dataset.dataset=data.dataset;
      excelOut.innerHTML=`<div class="import-preview-head"><div><strong>${escHtml(data.filename)}</strong><span>${data.row_count} data row${data.row_count===1?'':'s'} detected</span></div><span class="status-pill status-active">Validated workbook</span></div>${mappingMarkup(data)}${previewTable(data)}<div class="import-commit-bar"><label>Duplicates<select id="excelConflictAction" class="form-input"><option value="skip">Skip Existing (Recommended)</option><option value="update">Update Existing</option></select></label><button type="button" class="btn btn-primary" id="commitExcelImport"><i class="fa-solid fa-database"></i> Import Valid Rows</button></div><div id="excelImportResult"></div>`;
      document.getElementById('commitExcelImport')?.addEventListener('click',commitExcel);
    }catch(err){showToast('Connection error while reading the workbook.','error');}
    finally{btn.disabled=false;btn.innerHTML='<i class="fa-regular fa-eye"></i> Preview &amp; Validate';}
  });

  async function commitExcel(){
    const button=document.getElementById('commitExcelImport'); if(!button)return;
    const mapping={}; excelOut.querySelectorAll('.import-map-select').forEach(sel=>{if(sel.value)mapping[sel.dataset.field]=sel.value;});
    const ok=await showAdminConfirm({title:'Import Existing Records',message:'Import the validated Excel rows into the NFH-HOA MySQL database?',confirmText:'Import Records',context:[{label:'Source',value:excelOut.dataset.filename},{label:'Data Type',value:excelOut.dataset.dataset}]});
    if(!ok)return;
    button.disabled=true; button.textContent='Importing…';
    try{
      const res=await fetch('/api/admin/import/excel/commit',{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify({token:excelOut.dataset.token,filename:excelOut.dataset.filename,dataset:excelOut.dataset.dataset,mapping,conflict_action:document.getElementById('excelConflictAction')?.value||'skip'})});
      const data=await res.json(); if(!res.ok||data.status!=='success'){showToast(data.message||'Import failed.','error');return;}
      const x=data.summary||{}; document.getElementById('excelImportResult').innerHTML=`<div class="import-result success"><strong>Import completed</strong><span>Total ${x.total||0} • Successful ${x.successful||0} • Duplicates ${x.duplicates||0} • Rejected ${x.rejected||0}</span>${x.errors?.length?`<details><summary>View rejected rows</summary><ul>${x.errors.map(e=>`<li>Row ${e.row}: ${escHtml(e.message)}</li>`).join('')}</ul></details>`:''}</div>`;
      showToast('Existing records import completed.','success');
      setTimeout(()=>window.location.reload(),1800);
    }catch(err){showToast('Connection error during import.','error');}
    finally{button.disabled=false;button.innerHTML='<i class="fa-solid fa-database"></i> Import Valid Rows';}
  }

  sqlForm?.addEventListener('submit',async e=>{
    e.preventDefault(); const btn=sqlForm.querySelector('button[type="submit"]'); btn.disabled=true; btn.textContent='Validating SQL…';
    try{
      const fd=new FormData(sqlForm); const res=await fetch('/api/admin/import/sql/preview',{method:'POST',body:fd,headers:{'Accept':'application/json','X-CSRF-Token':csrf()}}); const data=await res.json();
      if(!res.ok||data.status!=='success'){
        const extra=data.problems?.length?` ${data.problems.join(' ')}`:''; showToast((data.message||'SQL validation failed.')+extra,'error',8000);return;
      }
      sqlOut.hidden=false; sqlOut.dataset.token=data.token; sqlOut.dataset.filename=data.filename;
      sqlOut.innerHTML=`<div class="import-preview-head"><div><strong>${escHtml(data.filename)}</strong><span>${data.statement_count} approved statement${data.statement_count===1?'':'s'} • Tables: ${data.tables.map(escHtml).join(', ')}</span></div><span class="status-pill status-active">Safe subset validated</span></div><div class="sql-statement-preview">${data.preview.map(x=>`<code>${escHtml(x)}</code>`).join('')}</div><div class="import-commit-bar"><span>Review the summary before execution. The import is transactional.</span><button type="button" class="btn btn-primary" id="commitSqlImport">Execute Validated SQL</button></div>`;
      document.getElementById('commitSqlImport')?.addEventListener('click',commitSql);
    }catch(err){showToast('Connection error while validating SQL.','error');}
    finally{btn.disabled=false;btn.innerHTML='<i class="fa-solid fa-shield-halved"></i> Validate SQL';}
  });

  async function commitSql(){
    const button=document.getElementById('commitSqlImport'); if(!button)return;
    const ok=await showAdminConfirm({title:'Execute Validated SQL',message:'Execute this validated data-only SQL import? The system will roll back the transaction if an execution error occurs.',confirmText:'Execute Import',type:'warning',context:[{label:'Source',value:sqlOut.dataset.filename}]});
    if(!ok)return; button.disabled=true; button.textContent='Executing…';
    try{
      const res=await fetch('/api/admin/import/sql/commit',{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify({token:sqlOut.dataset.token,filename:sqlOut.dataset.filename})}); const data=await res.json();
      if(!res.ok||data.status!=='success'){showToast(data.message||'SQL import failed.','error',8000);return;}
      showToast(`SQL import completed: ${data.summary?.affected_rows||0} affected row(s).`,'success'); setTimeout(()=>window.location.reload(),1600);
    }catch(err){showToast('Connection error during SQL import.','error');}
    finally{button.disabled=false;button.textContent='Execute Validated SQL';}
  }
})();
