// LecAI 공용 사이드바: 데스크톱 아이콘 막대 접기 (UnivDash 방식). 상태는 브라우저에 저장.
(function () {
  var KEY = 'lecai-sidebar-collapsed';
  function get() { try { return localStorage.getItem(KEY) === '1'; } catch (e) { return false; } }
  function set(v) { try { localStorage.setItem(KEY, v ? '1' : '0'); } catch (e) {} }
  if (get()) document.documentElement.classList.add('sidebar-collapsed');
  function sync() {
    var collapsed = document.documentElement.classList.contains('sidebar-collapsed');
    var btn = document.getElementById('sidebarCollapseBtn');
    if (btn) { btn.setAttribute('aria-label', collapsed ? '사이드바 펼치기' : '사이드바 아이콘만 남기기'); btn.title = collapsed ? '사이드바 펼치기' : '사이드바 아이콘만 남기기'; }
  }
  window.toggleSidebarRail = function (force) {
    var collapsed = typeof force === 'boolean' ? force : !document.documentElement.classList.contains('sidebar-collapsed');
    document.documentElement.classList.toggle('sidebar-collapsed', collapsed);
    set(collapsed); sync();
    window.dispatchEvent(new Event('resize'));
  };
  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('sidebarCollapseBtn');
    if (btn) btn.addEventListener('click', function () { window.toggleSidebarRail(); });
    sync();
  });
})();
