/* Converter: 헤드리스 Chrome 안에서 원본을 HTML 로 그린다 (UnivDash 파일 뷰어의 마크다운 렌더러와 같은 방식).
   - 사용자 내용은 모두 DOMPurify 로 정리한 뒤 넣는다. 그림은 data: 만 보인다 (CSP 로 파일 · 네트워크 접근 차단). */
(function () {
  'use strict';
  var esc = function (s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
  var LANGS = {
    py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript', json: 'json', html: 'xml', htm: 'xml', xml: 'xml', svg: 'xml', vue: 'xml',
    css: 'css', scss: 'scss', less: 'less', md: 'markdown', sh: 'bash', bash: 'bash', zsh: 'bash', yml: 'yaml', yaml: 'yaml', toml: 'ini', ini: 'ini', cfg: 'ini', conf: 'ini', env: 'bash',
    go: 'go', rs: 'rust', java: 'java', kt: 'kotlin', c: 'c', h: 'c', cpp: 'cpp', hpp: 'cpp', cc: 'cpp', cs: 'csharp', rb: 'ruby', php: 'php', sql: 'sql', swift: 'swift', lua: 'lua', r: 'r', pl: 'perl', diff: 'diff', patch: 'diff', graphql: 'graphql', tex: 'latex',
  };

  // 수식: marked 가 TeX 기호를 망가뜨리지 않게 먼저 자리표시(KTXM0Z)로 빼 두었다가 정리 뒤 KaTeX 로 그린다.
  // 백슬래시가 이스케이프로 먹힌 TeX 복구 (파이썬 일반 문자열로 쓴 파일: "\boxed" → 백스페이스+"oxed", "\frac" → 폼피드+"rac" …)
  // \a \b \f \v 제어 문자는 글에 올 일이 없어 항상, 탭 · CR 은 TeX 명령 꼬리가 바로 이어질 때만 되돌린다.
  var CTRL = { '\x07': 'a', '\x08': 'b', '\x0b': 'v', '\x0c': 'f' };
  function repairTexEscapes(src) {
    return src
      .replace(/[\x07\x08\x0b\x0c](?=[A-Za-z])/g, function (c) { return '\\' + CTRL[c]; })
      .replace(/\t(?=(?:heta|imes|ext|extbf|extit|ilde|au|op|riangle|herefore|frac|o\b))/g, '\\t')
      .replace(/\r(?=(?:ho|ight|angle|ceil|floor|vert|Vert|m\b|m\{))/g, '\\r');
  }
  function extractMath(src) {
    src = repairTexEscapes(src);
    var math = [];
    if (!window.katex || !/\$|\\\(|\\\[/.test(src)) return { text: src, math: math };
    var put = function (tex, display) { math.push({ tex: tex.trim(), display: display }); return 'KTXM' + (math.length - 1) + 'Z'; };
    var inText = function (text) {
      return text
        .replace(/\$\$([\s\S]+?)\$\$/g, function (m, tex) { return put(tex, true); })
        .replace(/\\\[([\s\S]+?)\\\]/g, function (m, tex) { return put(tex, true); })
        .replace(/\\\(([\s\S]+?)\\\)/g, function (m, tex) { return put(tex, false); })
        .replace(/(^|[^\\$])\$(?![\s$])((?:\\.|[^$\n\\])+?)(?<!\s)\$(?!\d)/g, function (m, pre, tex) { return pre + put(tex, false); });
    };
    var text = src.split(/(^[ \t]*(?:```|~~~)[^\n]*\n[\s\S]*?^[ \t]*(?:```|~~~)[ \t]*$)/m)
      .map(function (part, i) { return i % 2 ? part : part.split(/(`+[^`\n]*?`+)/).map(function (piece, j) { return j % 2 ? piece : inText(piece); }).join(''); })
      .join('');
    return { text: text, math: math };
  }
  function renderMath(html, math) {
    if (!math.length) return html;
    return html.replace(/KTXM(\d+)Z/g, function (m, index) {
      var item = math[Number(index)];
      if (!item) return m;
      try {
        return window.katex.renderToString(item.tex, { displayMode: item.display, throwOnError: false, trust: false, strict: 'ignore', maxSize: 50, maxExpand: 1000, output: 'htmlAndMathml' });
      } catch (e) { return '<code>' + esc(item.tex) + '</code>'; }
    });
  }
  function highlight(code, lang) {
    var name = LANGS[lang] || lang;
    if (window.hljs && name && window.hljs.getLanguage(name)) {
      try { return window.hljs.highlight(code, { language: name, ignoreIllegals: true }).value; } catch (e) { /* 그대로 */ }
    }
    return esc(code);
  }
  // data: 가 아닌 그림(로컬 · 웹)은 가져올 수 없으니 대체 글로 바꾼다
  function dropExternalImages(root) {
    root.querySelectorAll('img').forEach(function (img) {
      var src = img.getAttribute('src') || '';
      if (/^data:image\//i.test(src)) return;
      var span = document.createElement('span');
      span.className = 'cv-missing';
      span.textContent = '[그림' + (img.getAttribute('alt') ? ': ' + img.getAttribute('alt') : '') + ']';
      img.replaceWith(span);
    });
    root.querySelectorAll('a[href]').forEach(function (a) { if (!/^(https?:|mailto:)/i.test(a.getAttribute('href'))) a.removeAttribute('href'); });
  }

  function markdown(content) {
    var parts = extractMath(content);
    var raw = window.marked.parse(parts.text, { gfm: true, breaks: false });
    var html = window.DOMPurify.sanitize(raw, { USE_PROFILES: { html: true }, FORBID_TAGS: ['style', 'form', 'input', 'button', 'iframe'], FORBID_ATTR: ['style'] });
    var box = document.createElement('div');
    box.className = 'exv-md';
    box.innerHTML = renderMath(html, parts.math);
    dropExternalImages(box);
    box.querySelectorAll('pre code').forEach(function (code) {
      var lang = (code.className.match(/language-([\w-]+)/) || [])[1];
      if (lang) { code.innerHTML = highlight(code.textContent, lang); code.classList.add('hljs'); }
    });
    box.querySelectorAll('table').forEach(function (table) { var w = document.createElement('div'); w.className = 'exv-md-table'; table.replaceWith(w); w.appendChild(table); });
    return box;
  }

  function sanitizedHtml(content, keepStyle) {
    var box = document.createElement('div');
    box.className = 'cv-html';
    box.innerHTML = window.DOMPurify.sanitize(content, {
      USE_PROFILES: { html: true, svg: true },
      FORBID_TAGS: ['form', 'input', 'button', 'iframe', 'object', 'embed', 'link', 'meta', 'base'].concat(keepStyle ? [] : ['style']),
      ADD_TAGS: keepStyle ? ['style'] : [],
      FORCE_BODY: true,
    });
    dropExternalImages(box);
    return box;
  }

  function text(content, lang) {
    var pre = document.createElement('pre');
    pre.className = 'cv-text';
    var code = document.createElement('code');
    code.className = 'hljs';
    code.innerHTML = content.length < 400000 ? highlight(content, lang) : esc(content);
    pre.appendChild(code);
    return pre;
  }

  var src = JSON.parse(document.getElementById('cv-src').textContent);
  var doc = document.getElementById('doc');
  var node;
  try {
    if (src.kind === 'md') node = markdown(src.content);
    else if (src.kind === 'html') node = sanitizedHtml(src.content, true);
    else if (src.kind === 'hwpx') { node = sanitizedHtml(src.content, false); node.className = 'hwp-doc'; }
    else node = text(src.content, src.lang);
  } catch (e) {
    node = text(src.content, '');
  }
  doc.appendChild(node);
})();
