var _novelsData = [];

function api() { return fetch('/api/status'); }

function loadNovels() {
  api().then(function(r) { return r.json(); }).then(function(d) {
    document.getElementById('statNovels').textContent = d.total_novels;
    document.getElementById('statChapters').textContent = d.total_chapters;
    document.getElementById('statContent').textContent = d.chapters_with_content;
    _renderStatus(d);
    _novelsData = d.novels || [];
    _renderNovels(_novelsData);
  }).catch(function(){});
}

function _renderStatus(d) {
  var el = document.getElementById('scrapeStatus');
  var textEl = document.getElementById('scrapeText');
  var fillEl = document.getElementById('scrapeFill');
  var statusEl = document.getElementById('statStatus');
  var s = d.scraping || {};

  if (s.active) {
    el.style.display = 'block';
    fillEl.style.width = (s.percent || 0) + '%';
    var msg = (s.message || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    var novel = (s.novel || '').replace(/</g,'&lt;');
    var total = s.total || 0, scraped = s.scraped || 0;
    textEl.innerHTML = '<span>Scraping:</span> ' + novel + ' &mdash; ' + msg + ' (' + scraped + '/' + total + ')';
    statusEl.innerHTML = '<span style="color:#3fb950">ACTIVE</span>';
  } else {
    el.style.display = 'none';
    statusEl.textContent = 'Idle';
  }
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
      + '<td><div class="progress-cell"><div class="progress-track"><div class="progress-fill" style="width:' + (n.status || 0) + '%"></div></div><span class="progress-pct ' + (n.status >= 100 ? 'done' : 'low') + '">' + (n.status || 0) + '%</span></div></td>'
      + '<td style="font-size:12px;color:#888">' + (n.latest || '') + '</td>'
      + '<td style="font-size:12px;color:#888">' + (n.last_scraped || 'Never') + '</td>'
      + '<td><div class="actions">'
        + '<button class="chevron-btn" onclick="toggleMenu(this)" title="Actions">⋮</button>'
        + '<div class="dropdown-menu">'
          + '<button class="menu-scrape" onclick="scrapeSingle(\'' + n.url.replace(/'/g,"\\'") + '\');closeAllMenus()">▶ Scrape</button>'
          + '<button class="menu-edit" onclick="openEdit(\'' + slug + '\',\'' + title.replace(/'/g,"\\'") + '\',\'' + author.replace(/'/g,"\\'") + '\',\'' + n.url.replace(/'/g,"\\'") + '\');closeAllMenus()">✎ Edit</button>'
          + '<button class="menu-delete" onclick="deleteNovel(\'' + slug + '\',\'' + title.replace(/'/g,"\\'") + '\');closeAllMenus()">✕ Delete</button>'
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

function updateScrapeStatus() {
  api().then(function(r){return r.json();}).then(function(d){
    _renderStatus(d);
    if (!d.scraping || !d.scraping.active) {
      _novelsData = d.novels || [];
      _renderNovels(_novelsData);
    }
  }).catch(function(){});
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

document.addEventListener('keydown', function(e){
  if(e.key==='Escape') { closeModal(); closeAllMenus(); }
});

document.addEventListener('click', function(e){
  if(!e.target.closest('.actions')) closeAllMenus();
});

function toggleMenu(btn) {
  var menu = btn.nextElementSibling;
  var wasOpen = menu.classList.contains('show');
  closeAllMenus();
  if(!wasOpen) { menu.classList.add('show'); btn.classList.add('open'); }
}

function closeAllMenus() {
  document.querySelectorAll('.dropdown-menu.show').forEach(function(m){ m.classList.remove('show'); });
  document.querySelectorAll('.chevron-btn.open').forEach(function(b){ b.classList.remove('open'); });
}

document.addEventListener('DOMContentLoaded', function() {
  loadNovels();
  setInterval(updateScrapeStatus, 3000);
});