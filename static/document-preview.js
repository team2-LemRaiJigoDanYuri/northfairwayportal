(function(){
  function ensurePreview(){
    let overlay=document.getElementById('nfhDocumentPreviewOverlay');
    if(overlay) return overlay;
    overlay=document.createElement('div');
    overlay.id='nfhDocumentPreviewOverlay';
    overlay.className='nfh-document-preview-overlay';
    overlay.setAttribute('role','dialog');overlay.setAttribute('aria-modal','true');overlay.setAttribute('aria-hidden','true');
    overlay.innerHTML=`<div class="nfh-document-preview-card" role="document">
      <div class="nfh-document-preview-head">
        <div class="nfh-document-preview-title"><i class="fa-regular fa-file-pdf" aria-hidden="true"></i><div><strong id="nfhDocumentPreviewTitle">Document Preview</strong><small>Official document layout — previewed inside the NFH-HOA portal.</small></div></div>
        <button type="button" class="nfh-document-preview-close" aria-label="Close preview"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="nfh-document-preview-body"><div class="nfh-document-preview-loading" id="nfhDocumentPreviewLoading">Loading preview…</div><iframe id="nfhDocumentPreviewFrame" class="nfh-document-preview-frame" title="Document preview"></iframe></div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('.nfh-document-preview-close').addEventListener('click',closePreview);
    overlay.addEventListener('click',e=>{if(e.target===overlay)closePreview();});
    const frame=overlay.querySelector('#nfhDocumentPreviewFrame');
    frame.addEventListener('load',()=>{const l=document.getElementById('nfhDocumentPreviewLoading');if(l)l.hidden=true;});
    return overlay;
  }
  function previewUrl(raw){
    const u=new URL(raw,window.location.origin);
    u.searchParams.set('preview','1');
    return `${u.pathname}${u.search}#toolbar=0&navpanes=0&scrollbar=1&view=FitH`;
  }
  function labelFor(anchor,url){
    const text=(anchor?.textContent||'').trim().toLowerCase();
    if(url.includes('/document.pdf')||text.includes('document')) return 'Official Document Preview';
    return 'Request Record Preview';
  }
  function openPreview(url,title){
    const overlay=ensurePreview();
    const frame=overlay.querySelector('#nfhDocumentPreviewFrame');
    const loading=overlay.querySelector('#nfhDocumentPreviewLoading');
    const heading=overlay.querySelector('#nfhDocumentPreviewTitle');
    if(heading) heading.textContent=title||'Document Preview';
    if(loading){loading.hidden=false;loading.textContent='Loading preview…';}
    frame.src=previewUrl(url);
    overlay.classList.add('is-open');overlay.setAttribute('aria-hidden','false');
    document.body.classList.add('nfh-preview-open');
    overlay.querySelector('.nfh-document-preview-close')?.focus();
  }
  function closePreview(){
    const overlay=document.getElementById('nfhDocumentPreviewOverlay');if(!overlay)return;
    overlay.classList.remove('is-open');overlay.setAttribute('aria-hidden','true');
    const frame=overlay.querySelector('#nfhDocumentPreviewFrame');if(frame)frame.src='about:blank';
    document.body.classList.remove('nfh-preview-open');
  }
  window.openInSystemDocumentPreview=openPreview;window.closeInSystemDocumentPreview=closePreview;
  document.addEventListener('click',e=>{
    const a=e.target.closest('a[href]');if(!a)return;
    if(a.hasAttribute('data-document-download')) return;
    const href=a.getAttribute('href')||'';
    const isRequestPdf=/\/requests\/[^/]+\/(?:pdf|document\.pdf)(?:[?#].*)?$/.test(href);
    if(!isRequestPdf)return;
    e.preventDefault();e.stopPropagation();
    openPreview(href,labelFor(a,href));
  },true);
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.getElementById('nfhDocumentPreviewOverlay')?.classList.contains('is-open'))closePreview();});
})();
