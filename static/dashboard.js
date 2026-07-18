var _novelsData = [];

function api() { return fetch('/api/status'); }

function loadNovels() {
  api().then(function(r) { return r.json(); }).then(function(d) {
    document.getElementById('statNovels').textContent = d.total_novels;
    document.getElementById('statChapters').textContent = d.total_chapters;
    document.getElementById('statContent').textContent = d.chapters_with_content;
    var s = d.scraping || {};
    var statusEl = document.getElementById('statStatus');
    if(s.active) {
      statusEl.innerHTML = '<span style="color:#3fb950">Scraping...</span>';  
    } else {
      var latest = null;
      var latestTime = 0;
      for(var i=0;i<d.novels.length;i++){
        if(d.novels[i].last_scraped && d.novels[i].last_scraped!=='Never'){
          var t = d.novels[i].last_scraped;
          if(t>latestTime){latestTime=t;latest=d.novels[i];}
        }
      }
      if(latest){
        statusEl.innerHTML='<span style="color:#8b949e;font-size:12px">Idle (updated '+latest.last_scraped+')</span>';
      } else {
        statusEl.innerHTML='<span style="color:#8b949e;font-size:12px">Idle</span>';
      }
    }
    _novelsData = d.novels || [];
    _renderNovels(_novelsData);
  }).catch(function(e){
    var statusEl = document.getElementById('statStatus');
    if(statusEl){
      statusEl.innerHTML = '<span style="color:#f85149">Error loading data</span>';
    }
    var body = document.getElementById('novelBody');
    if(body){
      body.innerHTML = '<tr><td colspan="7" style="color:#f85149">Failed to load novels. Check console.</td></tr>';
    }
  });
}

function _renderNovels(novels) {
  var body = document.getElementById('novelBody');
  if (!novels || novels.length === 0) {
    body.innerHTML = '<tr><td colspan="7" class="empty-msg">No novels yet. Add URLs using the form below.</td></tr>';
    return;
  }
  var html = '';
  for (var i = 0; i < novels.length; i++) {
    var n = novels[i];
    var title = htmlEscape(n.title);
    var author = htmlEscape(n.author);
    var slug = n.url.replace(/.*\/novel\//,'').replace(/\/$/,'');
    html += '<tr>'
      + '<td><a href="/rss/' + slug + '" target="_blank">' + title + '</a></td>'
      + '<td style="color:#aaa">' + author + '</td>'
      + '<td>' + (n.chapter_count || 0) + '</td>'
      + '<td><div class="progress-cell"><div class="progress-track"><div class="progress-fill" style="width:' + (n.status || 0) + '%"></div></div><span class="progress-pct ' + (n.status >= 100 ? 'done' : 'low') + '" title="' + n.chapters_scraped + ' of ' + (n.chapter_count || 0) + ' chapters">' + (n.status || 0) + '%</span></div></td>'
      + '<td style="font-size:12px;color:#888">' + (n.latest || '') + '</td>'
      + '<td style="font-size:12px;color:#888">' + (n.last_scraped || 'Never') + '</td>'
      + '<td><div class="actions">'
        + '<button class="chevron-btn" onclick="toggleMenu(this)" title="Actions">&#8942;</button>'
        + '<div class="dropdown-menu">'
          + '<button class="menu-scrape" data-idx="' + i + '" onclick="handleMenuClick(this,\'scrape\')">&#9654; Scrape</button>'
          + '<button class="menu-edit" data-idx="' + i + '" onclick="handleMenuClick(this,\'edit\')">&#10098; Edit</button>'
          + '<button class="menu-delete" data-idx="' + i + '" onclick="handleMenuClick(this,\'delete\')">&#10005; Delete</button>'
        + '</div>'
    + '</div></td>'
    + '</tr>';
  }
  body.innerHTML = html;
}

function htmlEscape(s) {
  if (!s) return '';
  var div = document.createElement('div');
  div.appendChild(document.createTextNode(s));
  return div.innerHTML;
}

function deleteNovel(slug, title) {
  if (!confirm('Remove "' + title + '"?')) return;
  fetch('/api/sources/' + slug, {method:'DELETE'}).then(function(r){
    return r.json();
  }).then(function(d){
    loadNovels();
  }).catch(function(e){alert('Error: '+e)});
}

function scrapeSingle(url) {
  fetch('/api/scrape/' + encodeURIComponent(url), {method:'POST'}).then(function(r){
    return r.json();
  }).then(function(d){
    loadNovels();
  }).catch(function(e){alert('Error: '+e)});
}

function addNovel() {
  var url = document.getElementById('addUrl').value.trim();
  if (!url) return;
  var btn = document.getElementById('addBtn');
  var st = document.getElementById('addStatus');
  btn.disabled = true;
  btn.textContent = 'Adding...';
  st.textContent = '';
  fetch('/api/sources/add?url=' + encodeURIComponent(url), {method:'POST'}).then(function(r){
    return r.json();
  }).then(function(d){
    st.textContent = (d.ok ? 'Added and scraping: ' : 'Added (scraping failed): ') + d.url;
    st.style.color = d.ok ? '#3fb950' : '#f85149';
    document.getElementById('addUrl').value = '';
    btn.textContent = 'Add &amp; Scrape';
    btn.disabled = false;
    loadNovels();
  }).catch(function(e){
    st.textContent = 'Error: '+e;
    st.style.color = '#f85149';
    btn.textContent = 'Add &amp; Scrape';
    btn.disabled = false;
  });
}

var _currentEditSlug = '';
function openEdit(slug, title, author, url) {
  _currentEditSlug = slug;
  document.getElementById('modalTitle').textContent = 'Edit: ' + title;
  document.getElementById('editTitle').value = title;
  document.getElementById('editAuthor').value = author;
  document.getElementById('editUrl').value = url;
  document.getElementById('modal').className = 'show';
  document.getElementById('modalSave').onclick = function(){
    var t = document.getElementById('editTitle').value.trim();
    var a = document.getElementById('editAuthor').value.trim();
    var u = document.getElementById('editUrl').value.trim();
    if(!t || !u) return;
    fetch('/api/novels/update', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url:u, slug:_currentEditSlug, title:t, author:a})
    }).then(function(r){return r.json();}).then(function(d){
      closeModal();
      loadNovels();
    }).catch(function(e){alert('Error: '+e)});
  };
}

function closeModal() {
  document.getElementById('modal').className = '';
}

function handleMenuClick(btn, action) {
  var idx = parseInt(btn.dataset.idx);
  var n = _novelsData[idx];
  if(!n) return;
  var title = htmlEscape(n.title);
  var slug = n.url.replace(/.*\/novel\//,'').replace(/\/$/,'');
  closeAllMenus();
  if(action === 'scrape') scrapeSingle(n.url);
  else if(action === 'edit') openEdit(slug, title, htmlEscape(n.author), n.url);
  else if(action === 'delete') deleteNovel(slug, title);
}

function toggleMenu(btn) {
  var menu = btn.nextElementSibling;
  var wasOpen = menu.classList.contains('show');
  closeAllMenus();
  if(!wasOpen) {
    menu.classList.add('show');
    btn.classList.add('open');
    // Flip to open upward if menu would be cut off at bottom of viewport
    var rect = menu.getBoundingClientRect();
    if(rect.bottom > window.innerHeight) {
      menu.style.top = 'auto';
      menu.style.bottom = '100%';
    } else {
      menu.style.top = '100%';
      menu.style.bottom = 'auto';
    }
  }
}

function closeAllMenus() {
  document.querySelectorAll('.dropdown-menu.show').forEach(function(m){ m.classList.remove('show'); });
  document.querySelectorAll('.chevron-btn.open').forEach(function(b){ b.classList.remove('open'); });
}

document.addEventListener('keydown', function(e){
  if(e.key==='Escape') { closeModal(); closeAllMenus(); }
});

document.addEventListener('click', function(e){
  if(!e.target.closest('.actions')) closeAllMenus();
});

document.addEventListener('DOMContentLoaded', function() {
  loadNovels();
});