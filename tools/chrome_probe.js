// Paste into Chrome DevTools Console on the page you want to compare.
// It writes a downloadable JSON file with computed styles, geometry, fonts,
// images, loaded stylesheets, and resource timing.
(() => {
  const STYLE_PROPS = [
    'display', 'position', 'box-sizing', 'width', 'height', 'min-width', 'max-width',
    'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
    'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
    'border-top-width', 'border-right-width', 'border-bottom-width', 'border-left-width',
    'border-top-style', 'border-right-style', 'border-bottom-style', 'border-left-style',
    'background-color', 'background-image', 'background-size', 'background-position',
    'color', 'font-family', 'font-size', 'font-weight', 'font-style', 'line-height',
    'letter-spacing', 'word-spacing', 'white-space', 'overflow', 'overflow-x', 'overflow-y',
    'z-index', 'opacity', 'transform', 'gap', 'row-gap', 'column-gap',
    'grid-template-columns', 'grid-template-rows', 'flex-direction', 'justify-content',
    'align-items', 'align-content', 'flex-wrap',
  ];

  const rect = (r) => ({
    x: r.x, y: r.y, width: r.width, height: r.height,
    top: r.top, right: r.right, bottom: r.bottom, left: r.left,
  });

  const selectorFor = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== document.documentElement; node = node.parentElement) {
      let part = node.localName;
      if (node.classList.length) part += '.' + [...node.classList].map(CSS.escape).join('.');
      const siblings = node.parentElement ? [...node.parentElement.children].filter(s => s.localName === node.localName) : [];
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      parts.unshift(part);
    }
    return parts.join(' > ');
  };

  const elementInfo = (el) => {
    const cs = getComputedStyle(el);
    const style = {};
    for (const prop of STYLE_PROPS) style[prop] = cs.getPropertyValue(prop);
    const pseudoInfo = (pseudo) => {
      const pcs = getComputedStyle(el, pseudo);
      const pseudoStyle = {};
      for (const prop of STYLE_PROPS) pseudoStyle[prop] = pcs.getPropertyValue(prop);
      return {
        content: pcs.getPropertyValue('content'),
        style: pseudoStyle,
      };
    };
    return {
      selector: selectorFor(el),
      tag: el.localName,
      id: el.id || '',
      className: el.className || '',
      text: (el.innerText || el.textContent || '').trim().slice(0, 160),
      rect: rect(el.getBoundingClientRect()),
      clientRects: [...el.getClientRects()].map(rect),
      style,
      pseudo: {
        before: pseudoInfo('::before'),
        after: pseudoInfo('::after'),
      },
    };
  };

  const visible = [...document.querySelectorAll('body, body *')]
    .filter(el => {
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
    })
    .slice(0, 500)
    .map(elementInfo);

  const stylesheets = [...document.styleSheets].map(sheet => {
    let rules = null;
    let error = null;
    try {
      rules = [...sheet.cssRules].slice(0, 200).map(rule => rule.cssText);
    } catch (e) {
      error = String(e);
    }
    return {
      href: sheet.href,
      disabled: sheet.disabled,
      media: sheet.media ? [...sheet.media] : [],
      owner: sheet.ownerNode ? sheet.ownerNode.outerHTML.slice(0, 500) : null,
      ruleCount: rules ? rules.length : null,
      rules,
      error,
    };
  });

  const resources = performance.getEntriesByType('resource').map(r => ({
    name: r.name,
    initiatorType: r.initiatorType,
    transferSize: r.transferSize,
    encodedBodySize: r.encodedBodySize,
    decodedBodySize: r.decodedBodySize,
    duration: r.duration,
  }));

  const payload = {
    url: location.href,
    capturedAt: new Date().toISOString(),
    viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
    title: document.title,
    readyState: document.readyState,
    bodyClass: document.body ? document.body.className : '',
    htmlClass: document.documentElement.className,
    fonts: document.fonts ? [...document.fonts].map(f => ({
      family: f.family, style: f.style, weight: f.weight, stretch: f.stretch, status: f.status,
    })) : [],
    stylesheets,
    resources,
    images: [...document.images].map(img => ({
      src: img.currentSrc || img.src,
      alt: img.alt,
      complete: img.complete,
      naturalWidth: img.naturalWidth,
      naturalHeight: img.naturalHeight,
      rect: rect(img.getBoundingClientRect()),
    })),
    elements: visible,
  };

  const json = JSON.stringify(payload, null, 2);
  console.log(json);
  const blob = new Blob([json + '\n'], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `chrome-probe-${location.hostname}-${Date.now()}.json`;
  a.click();
})();
