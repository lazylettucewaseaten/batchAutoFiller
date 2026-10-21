// Runs inside each page frame (evaluated by autofill.py).
// extract() finds every form field, including inside open shadow roots, and tags it with
// data-af="<frame>-<n>" so Python can find it again. Radio/checkbox options get "<id>.<k>".
(() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const shown = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const byId = (el, id) => (el.getRootNode().getElementById?.(id)) || document.getElementById(id);

  // Label text the element itself declares.
  const own = el => {
    const t = [];
    if (el.labels) for (const l of el.labels) t.push(l.innerText);
    const by = el.getAttribute('aria-labelledby');
    if (by) for (const id of by.split(/\s+/)) { const n = byId(el, id); if (n) t.push(n.innerText); }
    t.push(el.getAttribute('aria-label') || '');
    return clean([...new Set(t.map(clean))].join(' '));
  };
  // Nearest text right before the element, a few levels up (question headings on unlabeled fields).
  // Stops at a sibling holding another field: text before that belongs to that field.
  const FIELD = 'input:not([type=hidden]),select,textarea,[role=combobox]';
  const before = el => {
    for (let n = el, i = 0; n && i < 5; n = n.parentElement, i++)
      for (let p = n.previousElementSibling; p; p = p.previousElementSibling) {
        if (p.matches(FIELD) || p.querySelector(FIELD)) break;
        const t = clean(p.innerText);
        if (t) return t.length > 400 ? t.slice(0, 150) + ' … ' + t.slice(-250) : t;  // keep heading + actual question
      }
    return '';
  };
  const common = els => { let a = els[0]; while (a && !els.every(e => a.contains(e))) a = a.parentElement; return a; };
  const checked = el => el.checked ?? (el.getAttribute('aria-checked') || el.getAttribute('aria-pressed')) === 'true';
  const required = el => !!(el.required || el.getAttribute('aria-required') === 'true');

  const allElements = () => {
    const all = [];
    (function walk(root) {
      for (const el of root.querySelectorAll('*')) { all.push(el); if (el.shadowRoot) walk(el.shadowRoot); }
    })(document);
    return all;
  };

  const TEXT_TYPES = /^(|text|email|tel|url|number|date|month|week|time|datetime-local)$/;

  function extract(prefix) {
    const all = allElements();
    for (const el of all) el.removeAttribute('data-af');
    const fields = [], groups = new Map();
    let n = 0, password = false;
    const add = (el, f) => {
      f.id = `${prefix}-${n++}`;
      el.setAttribute('data-af', f.id);
      f.question = clean(f.question).slice(0, 300);
      f.required = f.required || /\*/.test(f.question);
      f.placeholder = el.getAttribute('placeholder') || '';
      f.name = [el.getAttribute('name'), el.id].filter(Boolean).join(' ');  // ids like cover_letter help with unlabeled uploads
      f.autocomplete = el.getAttribute('autocomplete') || '';
      f.options = f.options || [];
      fields.push(f);
    };

    for (const el of all) {
      if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
      if (el.getAttribute('aria-hidden') === 'true' && el.type !== 'file') continue;  // validation mirrors behind custom widgets
      const tag = el.tagName, type = (el.getAttribute('type') || '').toLowerCase(), role = el.getAttribute('role');

      if (role === 'combobox' || (tag === 'BUTTON' && el.getAttribute('aria-haspopup') === 'listbox')) {
        if (!shown(el)) continue;
        let value = tag === 'INPUT' ? el.value : clean(el.innerText);
        if (/^(select|choose|--)/i.test(value)) value = '';
        // react-select keeps the chosen value in a sibling div, not in the input
        if (!value) value = clean(el.closest('[class*=control],[class*=container]')?.querySelector('[class*=singleValue],[class*=single-value]')?.innerText);
        const list = el.getAttribute('aria-controls') && byId(el, el.getAttribute('aria-controls'));
        const options = list ? [...list.querySelectorAll('[role=option]')].map(o => clean(o.innerText)).filter(Boolean) : [];
        add(el, { kind: 'combobox', typeable: tag === 'INPUT', question: own(el) || before(el), required: required(el), value, options });
      } else if (tag === 'SELECT') {
        if (!shown(el)) continue;
        const options = [...el.options].filter(o => o.value !== '').map(o => clean(o.text));
        add(el, { kind: 'select', question: own(el) || before(el), required: required(el), value: el.value ? clean(el.selectedOptions[0]?.text) : '', options });
      } else if (tag === 'BUTTON' && el.hasAttribute('aria-pressed') && el.parentElement.querySelectorAll(':scope > button[aria-pressed]').length > 1) {
        // Yes/No toggle buttons (Ashby and similar) act like a radio group
        if (!groups.has(el.parentElement)) groups.set(el.parentElement, []);
        groups.get(el.parentElement).push({ el, radio: true });
      } else if (type === 'checkbox' && el.parentElement?.querySelector(':scope > button[aria-pressed]')) {
        continue;  // hidden mirror of the toggle buttons above
      } else if (type === 'radio' || type === 'checkbox' || role === 'radio' || role === 'checkbox' || role === 'switch') {
        const radio = type === 'radio' || role === 'radio';
        const vis = shown(el) || (el.labels?.[0] && shown(el.labels[0])) || (el.parentElement && shown(el.parentElement));
        if (!vis) continue;
        const key = el.getAttribute('name') ? (radio ? 'r:' : 'c:') + el.getAttribute('name')
          : (el.closest('[role=radiogroup],[role=group],fieldset') || (radio ? el.parentElement?.parentElement : el));
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push({ el, radio });
      } else if (type === 'file') {
        add(el, { kind: 'file', question: own(el) || before(el) || el.getAttribute('accept') || '', required: required(el), value: el.files?.length ? 'set' : '' });
      } else if ((tag === 'INPUT' && TEXT_TYPES.test(type)) || tag === 'TEXTAREA') {
        if (!shown(el) || el.readOnly) continue;
        add(el, { kind: type === 'date' ? 'date' : 'text', input_type: tag === 'TEXTAREA' ? 'textarea' : (type || 'text'),
          question: own(el) || before(el), required: required(el), value: el.value });
      } else if (tag === 'INPUT' && type === 'password') {
        if (shown(el)) password = true;
      } else if (el.getAttribute('contenteditable') === 'true' && !el.parentElement?.isContentEditable) {
        if (!shown(el)) continue;
        add(el, { kind: 'editable', question: own(el) || before(el), required: required(el), value: clean(el.innerText) });
      }
    }

    for (const opts of groups.values()) {
      const els = opts.map(o => o.el), first = els[0];
      const kind = opts[0].radio ? 'radio' : els.length > 1 ? 'checkbox_group' : 'checkbox';
      const optionLabel = el => own(el) || clean(el.innerText) || el.getAttribute('value') || clean(el.parentElement?.innerText);
      let question;
      if (kind === 'checkbox') {
        question = own(first) || clean(first.parentElement?.innerText);
        if (question.length < 15) question = before(first) + ' ' + question;  // "Yes" box under a question
      } else {
        const g = first.closest('fieldset,[role=radiogroup],[role=group]');
        question = (g && (own(g) || clean(g.querySelector('legend')?.innerText)))
          || before(common(els) || first) || before(first.labels?.[0] || first);
      }
      const options = els.map(optionLabel);
      const value = kind === 'checkbox' ? checked(first)
        : kind === 'radio' ? (options[els.findIndex(checked)] || '') : options.filter((_, i) => checked(els[i]));
      const group = first.closest('[role=radiogroup],[role=group]');
      add(first, { kind, question, value, options: kind === 'checkbox' ? [] : options,
        required: els.some(required) || (group && required(group)) });
      if (kind !== 'checkbox') {
        first.removeAttribute('data-af');
        els.forEach((el, k) => el.setAttribute('data-af', `${fields.at(-1).id}.${k}`));
      }
    }
    return { fields, password };
  }

  // Tags and returns the text of the first visible Next-style (or Apply-style) button.
  function findButton(kind) {
    const want = kind === 'next'
      ? /^(next|continue|save (and|&) (continue|next)|proceed|next step)\b/i
      : /^(apply|apply now|apply manually|apply for this (job|position|role)|start (your )?application)$/i;
    const avoid = kind === 'next' ? /submit|apply|send|finish/i : /linkedin|indeed|seek/i;
    document.querySelectorAll('[data-af-btn]').forEach(b => b.removeAttribute('data-af-btn'));
    for (const b of document.querySelectorAll('button, [role=button], input[type=submit], input[type=button], a')) {
      const t = clean(b.innerText || b.value || b.getAttribute('aria-label'));
      if (t.length < 40 && want.test(t) && !avoid.test(t) && shown(b) && !b.disabled) {
        b.setAttribute('data-af-btn', '1');
        return t;
      }
    }
    return null;
  }

  return { extract, findButton };
})()
