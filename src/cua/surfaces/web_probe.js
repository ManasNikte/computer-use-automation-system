/*
 * Accessibility-tree extraction for the web surface.
 *
 * We compute role + accessible name ourselves rather than using Playwright's
 * `page.accessibility.snapshot()` for two reasons:
 *
 *  1. That API is deprecated and gives us no addressing material -- we need
 *     the `name=` attribute, the containing row's text, and a CSS path in
 *     order to synthesize *durable* locator strategies at record time.
 *  2. It doesn't know about the legacy labelling convention that dominates
 *     these apps: a field is "labelled" purely by the table cell sitting to
 *     its left, with no `for`/`id` pairing anywhere. Walking it ourselves
 *     lets us recover a human-meaningful name in that case and, critically,
 *     record *where the name came from* so the recorder knows whether a
 *     role+name locator will actually work on replay.
 *
 * Returns a flat list of interactive controls. One call per frame.
 */
() => {
  const INTERACTIVE_ROLES = [
    "button", "link", "textbox", "combobox", "checkbox", "radio", "menuitem", "tab",
  ];

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();

  const roleOf = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit && INTERACTIVE_ROLES.indexOf(explicit) !== -1) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "button") return "button";
    if (tag === "a") return el.hasAttribute("href") ? "link" : null;
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      if (type === "hidden") return null;
      if (["submit", "button", "reset", "image"].indexOf(type) !== -1) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      return "textbox";
    }
    return null;
  };

  /* Returns [name, source]. `source` is what makes this useful downstream:
     a name derived from aria-label is one the platform's own accessible-name
     computation will agree with, so a role+name locator will resolve. A name
     we inferred from an adjacent table cell is meaningful to a human and to
     the LLM, but the browser does NOT consider it the accessible name -- so
     the recorder must fall back to attribute/CSS targeting for that control. */
  const nameOf = (el) => {
    const aria = el.getAttribute("aria-label");
    if (norm(aria)) return [norm(aria), "aria-label"];

    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const t = labelledBy.split(/\s+/)
        .map((id) => { const n = document.getElementById(id); return n ? n.textContent : ""; })
        .join(" ");
      if (norm(t)) return [norm(t), "aria-labelledby"];
    }

    if (el.id) {
      let lab = null;
      try { lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); } catch (e) { lab = null; }
      if (lab && norm(lab.textContent)) return [norm(lab.textContent), "label-for"];
    }

    const ancestorLabel = el.closest ? el.closest("label") : null;
    if (ancestorLabel && norm(ancestorLabel.textContent)) {
      return [norm(ancestorLabel.textContent), "label-ancestor"];
    }

    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "input" && ["submit", "button", "reset"].indexOf(type) !== -1) {
      if (norm(el.value)) return [norm(el.value), "value"];
    }
    if (tag === "button" || tag === "a") {
      if (norm(el.textContent)) return [norm(el.textContent), "text"];
    }
    if (norm(el.getAttribute("placeholder"))) return [norm(el.getAttribute("placeholder")), "placeholder"];
    if (norm(el.getAttribute("title"))) return [norm(el.getAttribute("title")), "title"];

    /* Legacy convention: <td>Member Number</td><td><input name=member_no></td> */
    const cell = el.closest ? el.closest("td") : null;
    if (cell && cell.previousElementSibling && norm(cell.previousElementSibling.textContent)) {
      return [norm(cell.previousElementSibling.textContent), "adjacent-cell"];
    }

    if (norm(el.getAttribute("name"))) return [norm(el.getAttribute("name")), "name-attr"];
    return ["", "none"];
  };

  const cssOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const unique = (sel) => {
      try { return document.querySelectorAll(sel).length === 1 ? sel : null; } catch (e) { return null; }
    };
    const nm = el.getAttribute("name");
    if (nm) {
      const s = unique(tag + '[name="' + nm + '"]');
      if (s) return s;
    }
    const type = el.getAttribute("type");
    if (tag === "input" && type && el.value) {
      const s = unique('input[type="' + type + '"][value="' + String(el.value).replace(/"/g, '\\"') + '"]');
      if (s) return s;
    }
    if (tag === "a" && el.getAttribute("href")) {
      const s = unique('a[href="' + el.getAttribute("href") + '"]');
      if (s) return s;
    }
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 8) {
      let p = cur.tagName.toLowerCase();
      if (p === "body" || p === "html") break;
      const parent = cur.parentElement;
      if (parent) {
        const sibs = Array.prototype.filter.call(parent.children, (c) => c.tagName === cur.tagName);
        if (sibs.length > 1) p += ":nth-of-type(" + (sibs.indexOf(cur) + 1) + ")";
      }
      parts.unshift(p);
      cur = parent;
    }
    return parts.join(" > ");
  };

  /* Text of the first cell of the containing row. In a results grid every row
     holds an identically-named "Open" link; the row's key column is the only
     thing that distinguishes them. This becomes a parameterized locator
     strategy at record time: "the link named Open in the row containing
     {member_id}". */
  const rowAnchorOf = (el) => {
    const tr = el.closest ? el.closest("tr") : null;
    if (!tr) return "";
    const first = tr.querySelector("td, th");
    if (!first) return "";
    return norm(first.textContent);
  };

  const visible = (el) => {
    if (!el.getClientRects || el.getClientRects().length === 0) return false;
    const st = window.getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none";
  };

  const out = [];
  const all = document.querySelectorAll("a, button, input, select, textarea, [role]");
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    const role = roleOf(el);
    if (!role) continue;
    if (!visible(el)) continue;
    const nm = nameOf(el);
    out.push({
      role: role,
      name: nm[0],
      name_source: nm[1],
      value: (el.value !== undefined && el.type !== "password") ? String(el.value || "") : "",
      enabled: !el.disabled,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute("type") || ""),
      name_attr: el.getAttribute("name") || "",
      href: el.getAttribute("href") || "",
      css: cssOf(el),
      row_anchor: rowAnchorOf(el),
    });
  }
  return {
    title: document.title || "",
    text: norm(document.body ? document.body.innerText : ""),
    controls: out,
  };
}
