// ============================================================================
// LecAI 공통 테마 시스템 (모든 페이지 공용)
// - #themeSettingsBtn 이 있는 페이지에 테마 설정 모달을 붙인다.
// - 테마 스타일: <html data-ui-theme="mesh|apple|mono"> (기본 '오로라'는 속성 없음)
// - 다크/라이트: <html data-ui-theme-mode="light"> + html.dark 클래스
//   (viewer.html 의 기존 CSS 변수 체계가 html.dark 를 보므로 둘 다 관리한다.
//    저장 키도 viewer 가 쓰던 localStorage 'theme' 를 그대로 공유한다.)
// - 실제 배색은 /static/shared/theme.css 가 담당한다.
// ============================================================================
(function () {
  'use strict';

  var STYLE_KEY = 'lecai_theme_style';
  var MODE_KEY = 'theme'; // viewer.html 이 쓰던 키를 그대로 공유
  var THEMES = [
    { id: 'default', label: '오로라' },
    { id: 'mesh', label: '그라디언트 메시' },
    { id: 'apple', label: '애플 글래스' },
    { id: 'mono', label: '미니멀 플랫' },
  ];

  var html = document.documentElement;

  function get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function set(key, v) { try { localStorage.setItem(key, v); } catch (e) { /* noop */ } }

  function currentStyle() { return get(STYLE_KEY) || 'default'; }
  function currentMode() { return get(MODE_KEY) === 'light' ? 'light' : 'dark'; }

  function applyStyle(id) {
    if (id === 'default') html.removeAttribute('data-ui-theme');
    else html.setAttribute('data-ui-theme', id);
    set(STYLE_KEY, id);
  }

  function applyMode(mode) {
    if (mode === 'light') {
      html.setAttribute('data-ui-theme-mode', 'light');
      html.classList.remove('dark');
    } else {
      html.removeAttribute('data-ui-theme-mode');
      html.classList.add('dark');
    }
    set(MODE_KEY, mode);
    try {
      window.dispatchEvent(new CustomEvent('lecai-theme-change', { detail: { mode: mode, style: currentStyle() } }));
    } catch (e) { /* noop */ }
  }

  // 초기 적용 (각 페이지 <head> 인라인 스니펫이 이미 했더라도 한 번 더 보정)
  applyStyle(currentStyle());
  applyMode(currentMode());

  // viewer.html 의 자체 해/달 토글은 html.dark 클래스만 바꾼다 —
  // 클래스 변화를 감지해 data-ui-theme-mode 속성을 따라 맞춘다.
  try {
    new MutationObserver(function () {
      var isDark = html.classList.contains('dark');
      var attrLight = html.getAttribute('data-ui-theme-mode') === 'light';
      if (isDark && attrLight) html.removeAttribute('data-ui-theme-mode');
      if (!isDark && !attrLight) html.setAttribute('data-ui-theme-mode', 'light');
    }).observe(html, { attributes: true, attributeFilter: ['class'] });
  } catch (e) { /* noop */ }

  function buildModal() {
    var overlay = document.createElement('div');
    overlay.id = 'lecaiThemeOverlay';
    overlay.className = 'lecai-theme-overlay';
    overlay.hidden = true;

    var optionsHtml = THEMES.map(function (t) {
      return (
        '<button type="button" class="lecai-theme-option" data-theme="' + t.id + '">'
        + '<span class="lecai-theme-swatch lecai-swatch-' + t.id + '"></span>'
        + '<span class="lecai-theme-label"><span class="lecai-theme-check">✓</span>' + t.label + '</span>'
        + '</button>'
      );
    }).join('');

    overlay.innerHTML =
      '<div class="lecai-theme-modal" role="dialog" aria-modal="true" aria-label="테마 설정">'
      + '<div class="lecai-theme-head">'
      + '<div><h3>테마 설정</h3><p>원하는 테마를 골라보세요. 모든 페이지에 동일하게 적용됩니다.</p></div>'
      + '<button type="button" class="lecai-theme-close" aria-label="닫기">&times;</button>'
      + '</div>'
      + '<div class="lecai-theme-body">' + optionsHtml + '</div>'
      + '<div class="lecai-theme-mode-row"><span>다크 모드</span>'
      + '<button type="button" class="lecai-mode-switch" id="lecaiModeSwitch" aria-label="다크 모드 전환"></button></div>'
      + '</div>';

    document.body.appendChild(overlay);
    return overlay;
  }

  function markSelected(overlay) {
    var active = currentStyle();
    overlay.querySelectorAll('.lecai-theme-option').forEach(function (btn) {
      btn.classList.toggle('selected', btn.getAttribute('data-theme') === active);
    });
    overlay.querySelector('#lecaiModeSwitch').classList.toggle('on', currentMode() === 'dark');
  }

  function init() {
    var btn = document.getElementById('themeSettingsBtn');
    if (!btn) return;

    var overlay = buildModal();

    function open() { markSelected(overlay); overlay.hidden = false; }
    function close() { overlay.hidden = true; }

    btn.addEventListener('click', open);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    overlay.querySelector('.lecai-theme-close').addEventListener('click', close);
    overlay.querySelectorAll('.lecai-theme-option').forEach(function (opt) {
      opt.addEventListener('click', function () {
        applyStyle(opt.getAttribute('data-theme'));
        markSelected(overlay);
        close();
      });
    });
    overlay.querySelector('#lecaiModeSwitch').addEventListener('click', function () {
      applyMode(currentMode() === 'dark' ? 'light' : 'dark');
      markSelected(overlay);
    });
    window.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !overlay.hidden) close();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
