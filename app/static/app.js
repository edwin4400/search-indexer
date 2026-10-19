"use strict";

// The token arrives in the launch URL and is kept for this tab only.
const TOKEN = new URLSearchParams(location.search).get("t") || "";
sessionStorage.setItem("cs-token", TOKEN);

const $ = (id) => document.getElementById(id);

let lastQuery = "";
let lastMatch = "";
let currentScope = "all";
let typingTimer = null;
let quitting = false;

const SCOPE_LABELS = {
  all: "Everything",
  files: "Files",
  email: "Email",
  attachments: "Attachments",
};

const KIND_LABELS = {
  file: "File",
  message: "Email",
  attachment: "Attachment",
  "archive-member": "Archive",
};

const KIND_ICONS = {
  file: "file-text",
  message: "mail",
  attachment: "paperclip",
  "archive-member": "archive",
};

const SCOPE_ICONS = {
  all: "search",
  files: "folder",
  email: "mail",
  attachments: "paperclip",
};

// ------------------------------------------------------------------- plumbing

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Token": sessionStorage.getItem("cs-token") || TOKEN,
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    // A tab opened by an earlier run still carries that run's token. Without
    // saying so, the interface simply looks broken.
    throw new Error("this tab's session token is no longer valid - open the URL "
      + "printed by the app (a tab left open from an earlier run keeps the old one)");
  }
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

function notice(message) {
  const box = $("notice");
  box.textContent = message || "";
  box.hidden = !message;
}

function placeholder(title, hint) {
  const box = document.createElement("div");
  box.className = "empty";
  const strong = document.createElement("strong");
  strong.textContent = title;
  box.append(strong);
  if (hint) {
    const span = document.createElement("span");
    span.textContent = hint;
    box.append(span);
  }
  return box;
}

function icon(name, extra = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", extra ? `icon ${extra}` : "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `/icons.svg#i-${name}`);
  svg.append(use);
  return svg;
}

function chip(text, extra = "", iconName = "") {
  const span = document.createElement("span");
  span.className = extra ? `chip ${extra}` : "chip";
  if (iconName) span.append(icon(iconName, "sm"));
  span.append(document.createTextNode(text));
  return span;
}

/** Drop everything the previous search produced. */
function clearResults() {
  lastQuery = "";
  lastMatch = "";
  $("results").replaceChildren();
  $("search-meta").textContent = "";
  $("preview").replaceChildren(
    placeholder("Nothing selected", "Search, then choose a result to read it.")
  );
}

// ------------------------------------------------------------------ searching

async function runSearch(query, record = false) {
  if (!query.trim()) {
    clearResults();
    return;
  }
  lastQuery = query;
  lastMatch = "";
  $("results").replaceChildren();
  // The previously previewed item may not be in the new result set, so it must
  // not stay on screen as if it were.
  $("preview").replaceChildren(placeholder("Searching…", ""));

  try {
    const data = await api(
      `/api/search?q=${encodeURIComponent(query)}&limit=50&scope=${currentScope}`);
    lastMatch = data.match || "";
    const summary = data.count
      ? `${data.count} result${data.count === 1 ? "" : "s"} for “${data.query}”`
      : `no results for “${data.query}”`;
    // The server says when it had to reinterpret or broaden the query. Without
    // that, "no results" covers several different situations silently.
    $("search-meta").textContent = data.note ? `${summary} — ${data.note}` : summary;
    renderResults(data.results);
    if (!data.results.length) {
      $("preview").replaceChildren(
        placeholder("No matches", "Try fewer words, or a different scope.")
      );
    } else {
      // The search is done; without this the pane still reads "Searching…".
      $("preview").replaceChildren(
        placeholder("Choose a result", "Select one above to read it here.")
      );
    }
    if (record) {
      // Only a submitted search joins the recent list; typing does not.
      const saved = await api("/api/recent", {
        method: "POST",
        body: JSON.stringify({ query }),
      });
      renderRecent(saved.recent);
    }
  } catch (error) {
    notice(`search failed: ${error.message}`);
  }
}

function renderResults(results) {
  const list = $("results");
  list.replaceChildren();
  for (const result of results) {
    const item = document.createElement("li");
    item.className = "result";
    item.tabIndex = 0;
    item.dataset.id = result.id;
    item.id = `r-${result.id}`;          // so a link can point at one result

    const head = document.createElement("div");
    head.className = "result-head";
    head.append(chip(KIND_LABELS[result.kind] || result.kind, "kind",
      KIND_ICONS[result.kind] || "file"));
    const title = document.createElement("span");
    title.className = "result-title";
    // Server-escaped, with only <mark> inserted; see service.highlight().
    title.innerHTML = result.name_html;
    head.append(title);
    item.append(head);

    if (result.parent_name) {
      const parent = document.createElement("div");
      parent.className = "result-sub";
      parent.textContent = `in: ${result.parent_name}`;
      item.append(parent);
    }

    const path = document.createElement("div");
    path.className = "result-path";
    path.textContent = result.uri;         // untrusted: textContent, never HTML
    item.append(path);

    if (result.snippet_html) {
      const snippet = document.createElement("div");
      snippet.className = "snippet";
      snippet.innerHTML = result.snippet_html;
      item.append(snippet);
    }

    const tags = document.createElement("div");
    tags.className = "result-tags";
    if (result.attachments) {
      tags.append(chip(`${result.attachments} attachment${result.attachments === 1 ? "" : "s"}`));
    }
    if (result.inline) tags.append(chip(`${result.inline} inline image(s)`));
    if (result.kind !== "file" && result.state === "partial") {
      tags.append(chip("name only", "warn"));
    }
    if (tags.children.length) item.append(tags);

    const open = () => activate(item);
    item.addEventListener("click", open);
    // Double-click opens the item itself: the file in its own application, or
    // the containing folder. A mail item explains why it cannot.
    item.addEventListener("dblclick", (event) => {
      event.preventDefault();
      launch(result.id, "open");
    });
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
    list.append(item);
  }
}

/** Mark one result as selected, keep it visible, and preview it. */
function activate(node) {
  if (!node) return;
  for (const sibling of node.parentElement.children) {
    sibling.classList.toggle("active", sibling === node);
  }
  node.scrollIntoView({ block: "nearest" });
  openItem(Number(node.dataset.id));
}

async function launch(id, action) {
  notice("");
  try {
    const result = await api("/api/open", {
      method: "POST",
      body: JSON.stringify({ id, action }),
    });
    if (action !== "check") notice(`Opened ${result.path}`);
  } catch (error) {
    notice(`cannot open: ${error.message}`);
  }
}

async function openItem(id) {
  try {
    // Send the expression that actually matched, so highlighting agrees with
    // the results even when the query had to be rebuilt or broadened.
    const expression = lastMatch || lastQuery;
    const item = await api(`/api/item?id=${id}&m=${encodeURIComponent(expression)}`);
    const preview = $("preview");
    preview.replaceChildren();

    const heading = document.createElement("h2");
    heading.className = "preview-title";
    heading.textContent = item.name || "(unnamed)";
    preview.append(heading);

    const sub = document.createElement("div");
    sub.className = "preview-sub";
    sub.textContent = `${KIND_LABELS[item.kind] || item.kind} · ${item.state}`
      + (item.reason ? ` · ${item.reason}` : "");
    preview.append(sub);

    const actions = document.createElement("div");
    actions.className = "row";
    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "btn tiny";
    openButton.append(icon("external-link", "sm"), document.createTextNode("Open"));
    openButton.addEventListener("click", () => launch(item.id, "open"));
    const revealButton = document.createElement("button");
    revealButton.type = "button";
    revealButton.className = "btn tiny";
    revealButton.append(icon("folder-open", "sm"), document.createTextNode("Show in folder"));
    revealButton.addEventListener("click", () => launch(item.id, "reveal"));
    actions.append(openButton, revealButton);
    preview.append(actions);

    if (item.parent) {
      const parent = document.createElement("button");
      parent.type = "button";
      parent.className = "linkish";
      parent.textContent = `in: ${item.parent.name}`;
      parent.addEventListener("click", () => openItem(item.parent.id));
      preview.append(parent);
    }

    if (item.children.length) {
      const IMAGE = /\.(png|jpe?g|gif|bmp|tiff?|webp|emf|wmf|ico)$/i;
      const documents = item.children.filter((child) => !IMAGE.test(child.name));
      const images = item.children.filter((child) => IMAGE.test(child.name));

      const summary = document.createElement("div");
      summary.className = "preview-sub";
      summary.textContent = images.length
        ? `${documents.length} attachment(s), plus ${images.length} inline image(s)`
        : `${documents.length} attachment(s)`;
      preview.append(summary);

      const list = document.createElement("ul");
      list.className = "children";
      for (const child of documents) {
        const entry = document.createElement("li");
        const link = document.createElement("button");
        link.type = "button";
        link.className = "linkish";
        link.textContent = child.name;
        link.addEventListener("click", () => openItem(child.id));
        const flag = document.createElement("span");
        flag.className = child.searchable ? "children-flag ok" : "children-flag";
        flag.textContent = child.searchable ? "· contents searchable" : "· name only";
        entry.append(link, flag);
        list.append(entry);
      }
      preview.append(list);

      if (images.length) {
        const note = document.createElement("div");
        note.className = "preview-sub";
        note.textContent = `${images.length} inline image(s) not listed — signature and `
          + "formatting graphics, findable by name only";
        preview.append(note);
      }
    }

    const textHost = document.createElement("div");
    preview.append(textHost);

    const text = document.createElement("pre");
    text.className = "preview-text";
    // Server-escaped, with only <mark> inserted around the actual matches.
    text.innerHTML = item.text_html || "(no stored text)";
    textHost.append(text);

    const marks = Array.from(text.querySelectorAll("mark"));
    if (marks.length) {
      const bar = document.createElement("div");
      bar.className = "matchbar";

      const previous = document.createElement("button");
      previous.type = "button";
      previous.append(icon("chevron-left", "sm"), document.createTextNode("prev"));

      const next = document.createElement("button");
      next.type = "button";
      next.append(document.createTextNode("next"), icon("chevron-right", "sm"));

      const label = document.createElement("span");
      label.className = "match-label";

      let current = 0;
      const show = (index, scroll = true) => {
        current = (index + marks.length) % marks.length;
        marks.forEach((mark, position) => {
          mark.classList.toggle("current", position === current);
        });
        // Only move the pane when the reader asked to step: scrolling on open
        // pushed the title, attachments and this toolbar out of view.
        if (scroll) marks[current].scrollIntoView({ block: "center", inline: "nearest" });
        label.textContent = `match ${current + 1} of ${marks.length}`
          + (item.matches > marks.length
            ? ` — ${item.matches} in the document, the rest outside this view`
            : "");
      };
      previous.addEventListener("click", () => show(current - 1));
      next.addEventListener("click", () => show(current + 1));
      bar.append(previous, label, next);
      textHost.insertBefore(bar, text);
      show(0, false);
    }
    // Replacing the children does not reliably reset the pane's own scroll
    // offset, so a document opened while the last one was scrolled down would
    // appear part-way through it.
    preview.scrollTop = 0;

    if (/\.pdf$/i.test(item.name || "")) {
      // Render the actual page when possible; the extracted text underneath
      // stays as the fallback if the file cannot be drawn.
      if (await renderPdf(preview, item, expression, textHost)) textHost.hidden = true;
    }

    if (item.fields.length) {
      const fields = document.createElement("div");
      fields.className = "fields";
      for (const field of item.fields) {
        const line = document.createElement("div");
        line.className = "field-line";
        const key = document.createElement("b");
        key.textContent = `${field.name}: `;
        line.append(key, document.createTextNode(field.value));
        fields.append(line);
      }
      preview.append(fields);
    }
  } catch (error) {
    notice(`could not open item: ${error.message}`);
  }
}

// --------------------------------------------------------------------- pdf

function escapeRegex(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Turn an FTS expression into patterns that match the text as it is written.
 *
 * `"6 450 00"` is how FTS stores the phrase `$6,450.00`; looking for those
 * three words with spaces between them would never find it in the document.
 * Join them with punctuation instead, so the price is highlighted whole.
 */
function queryPatterns(expression) {
  const found = [];
  const seen = new Set();
  const add = (source) => {
    const key = source.toLowerCase();
    if (!seen.has(key)) {
      seen.add(key);
      found.push(new RegExp(source, "gi"));
    }
  };

  let rest = expression || "";
  rest = rest.replace(/"([^"]+)"\*?/g, (whole, phrase) => {
    const words = phrase.trim().split(/\s+/).filter((word) => word
      && word.toUpperCase() !== "AND");
    if (words.length) add(words.map(escapeRegex).join("[^\\p{L}\\p{N}]*"));
    return " ";
  });
  for (const word of rest.match(/[0-9A-Za-z_\u00c0-\u024f]+/g) || []) {
    if (word.toUpperCase() !== "AND") add(escapeRegex(word));
  }
  return found;
}

/** Draw the PDF page that contains the search terms, marking the matching words. */
async function renderPdf(preview, item, expression, before) {
  const patterns = queryPatterns(expression);
  if (!patterns.length) return false;

  let pdfjs;
  let doc;
  try {
    const response = await fetch(`/api/raw?id=${item.id}`, {
      headers: { "X-Token": sessionStorage.getItem("cs-token") || TOKEN },
    });
    if (!response.ok) return false;
    const bytes = await response.arrayBuffer();
    pdfjs = await import("/vendor/pdf.min.mjs");
    pdfjs.GlobalWorkerOptions.workerSrc = "/vendor/pdf.worker.min.mjs";
    doc = await pdfjs.getDocument({ data: bytes }).promise;
  } catch (error) {
    return false;                    // fall back to the extracted text
  }

  const wrap = document.createElement("div");
  wrap.className = "pdf";
  const stage = document.createElement("div");
  stage.className = "pdf-stage";
  const canvas = document.createElement("canvas");
  stage.append(canvas);

  const bar = document.createElement("div");
  bar.className = "matchbar";
  const previous = document.createElement("button");
  previous.type = "button";
  previous.append(icon("chevron-left", "sm"));
  const next = document.createElement("button");
  next.type = "button";
  next.append(icon("chevron-right", "sm"));
  const label = document.createElement("span");
  label.className = "match-label";
  bar.append(previous, label, next);
  const note = document.createElement("div");
  note.className = "pdf-note";
  // Page controls stay above the sheet: a full page is taller than the pane,
  // so a toolbar underneath would be out of sight most of the time.
  wrap.append(bar, stage, note);
  preview.insertBefore(wrap, before);

  const context = canvas.getContext("2d");
  let pageNumber = 1;

  const draw = async (wanted) => {
    const page = await doc.getPage(Math.min(Math.max(wanted, 1), doc.numPages));
    pageNumber = page.pageNumber;
    const viewport = page.getViewport({ scale: 1.45 });
    canvas.width = Math.floor(viewport.width);
    canvas.height = Math.floor(viewport.height);
    await page.render({ canvasContext: context, viewport }).promise;

    const items = (await page.getTextContent()).items;
    let hits = 0;
    context.fillStyle = "rgba(255, 214, 102, .46)";
    for (const entry of items) {
      if (!entry.str) continue;
      const text = entry.str.toLowerCase();
      const matrix = pdfjs.Util.transform(viewport.transform, entry.transform);
      const height = Math.hypot(matrix[2], matrix[3]) || 10;
      const top = matrix[5] - height;
      // pdf.js reports a run's width in PDF units (font size included), so a
      // character's slice scales with the viewport. Summing the whole run was
      // what made a single hit look like a highlighted paragraph.
      const runWidth = Number.isFinite(entry.width) && entry.width > 0
        ? entry.width * viewport.scale
        : height * 0.55 * entry.str.length;
      const perChar = runWidth / Math.max(entry.str.length, 1);
      for (const pattern of patterns) {
        pattern.lastIndex = 0;
        let hit;
        while ((hit = pattern.exec(text)) !== null) {
          const left = matrix[4] + hit.index * perChar;
          context.fillRect(
            left, top,
            Math.max(hit[0].length * perChar, 4), height * 1.15,
          );
          hits += 1;
          if (!hit[0].length) pattern.lastIndex += 1;
        }
      }
    }

    label.textContent = `page ${pageNumber} of ${doc.numPages}`;
    note.textContent = hits
      ? `${hits} match${hits === 1 ? "" : "es"} highlighted on this page`
      : "no match on this page — it is elsewhere in the document";
    previous.disabled = pageNumber <= 1;
    next.disabled = pageNumber >= doc.numPages;
  };

  previous.addEventListener("click", () => draw(pageNumber - 1));
  next.addEventListener("click", () => draw(pageNumber + 1));

  try {
    let first = 1;
    const limit = Math.min(doc.numPages, 200);
    for (let candidate = 1; candidate <= limit; candidate += 1) {
      const page = await doc.getPage(candidate);
      const content = await page.getTextContent();
      const text = content.items.map((entry) => entry.str || "").join(" ").toLowerCase();
      if (patterns.some((pattern) => {
        pattern.lastIndex = 0;
        return pattern.test(text);
      })) {
        first = candidate;
        break;
      }
    }
    await draw(first);
  } catch (error) {
    wrap.remove();
    return false;
  }
  return true;
}

// ------------------------------------------------------------------- status

async function refreshStatus() {
  if (quitting) return;
  try {
    const status = await api("/api/status");
    if (quitting) return;
    const pill = $("mode");
    pill.textContent = status.mode + (status.busy ? "…" : "");
    pill.className = "pill"
      + (status.mode === "watching" ? " live" : status.busy ? " busy" : "");

    const totals = status.totals;
    $("counts").textContent =
      `${totals.items_total.toLocaleString()} items · `
      + `${totals.items_attachments.toLocaleString()} attachments · `
      + `${totals.with_text.toLocaleString()} with text · `
      + `${(totals.db_bytes / 1048576).toFixed(1)} MB`;

    notice(status.error
      ? `worker error: ${status.error}`
      : status.stopping
        ? "Stopping - finishing the current item..."
        : "");
    $("progress").textContent = Object.keys(status.progress).length
      ? JSON.stringify(status.progress, null, 2)
      : "idle";
    renderRoots(status.roots);
    renderScopes(status.scopes);
    renderRecent(status.recent);
  } catch (error) {
    if (quitting) return;
    $("mode").textContent = "disconnected";
    notice("Content Search is not responding. Close this tab and start the app again.");
  }
}

function renderScopes(scopes) {
  const nav = $("scopes");
  nav.replaceChildren();
  for (const key of ["all", "files", "email", "attachments"]) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "rail-item" + (key === currentScope ? " active" : "");

    const label = document.createElement("span");
    label.className = "rail-label";
    label.append(
      icon(SCOPE_ICONS[key] || "search", "sm"),
      document.createTextNode(SCOPE_LABELS[key]),
    );

    const count = document.createElement("span");
    count.className = "rail-count";
    count.textContent = (scopes?.[key] ?? 0).toLocaleString();

    button.append(label, count);
    button.addEventListener("click", () => {
      currentScope = key;
      // Re-run whatever is in the box so the scope change is visible at once.
      if (lastQuery) runSearch(lastQuery);
      else refreshStatus();
    });
    nav.append(button);
  }
}

function renderRecent(recent) {
  const list = $("recent-list");
  const input = $("q");
  list.replaceChildren();
  for (const query of recent || []) {
    const option = document.createElement("option");
    option.value = query;
    list.append(option);
  }
  // An empty datalist still draws the browser's dropdown indicator, so it is
  // attached only once there is something in it.
  if (recent && recent.length) input.setAttribute("list", "recent-list");
  else input.removeAttribute("list");
}

function renderRoots(roots) {
  const list = $("roots");
  list.replaceChildren();
  if (!roots.length) {
    const empty = document.createElement("li");
    empty.className = "muted small";
    empty.textContent = "No folders yet — add one above, then press Index now.";
    list.append(empty);
    return;
  }
  for (const root of roots) {
    const li = document.createElement("li");

    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = root.enabled !== false;
    toggle.addEventListener("change", async () => {
      await api("/api/roots/toggle", {
        method: "POST",
        body: JSON.stringify({ path: root.path, enabled: toggle.checked }),
      });
      refreshStatus();
    });

    const path = document.createElement("span");
    path.className = "path";
    path.textContent = root.path;

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn tiny";
    remove.textContent = "Remove";
    remove.addEventListener("click", async () => {
      const result = await api("/api/roots/remove", {
        method: "POST",
        body: JSON.stringify({ path: root.path }),
      });
      notice(`Removed ${root.path} and ${result.removed_items} indexed item(s).`);
      refreshStatus();
    });

    li.append(toggle, path, remove);
    list.append(li);
  }
}

// ------------------------------------------------------------------- wiring

$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch($("q").value, true);
});

$("q").addEventListener("input", (event) => {
  clearTimeout(typingTimer);
  const value = event.target.value;
  // Clearing the box must clear the panes, not leave the last search on screen.
  if (!value.trim()) {
    clearResults();
    return;
  }
  typingTimer = setTimeout(() => {
    if (value.trim().length >= 2) runSearch(value);
  }, 300);
});

// Escape empties the box; / or Ctrl+K focuses it from anywhere.
$("q").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.target.value = "";
    clearResults();
    return;
  }
  // Arrow keys walk the results, previewing each one, so the list can be read
  // without reaching for the mouse.
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    const items = Array.from($("results").children);
    if (!items.length) return;
    event.preventDefault();
    const at = items.findIndex((node) => node.classList.contains("active"));
    const next = event.key === "ArrowDown"
      ? Math.min(at + 1, items.length - 1)
      : Math.max(at - 1, 0);
    activate(items[next < 0 ? 0 : next]);
  }
});
document.addEventListener("keydown", (event) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
  if ((event.key === "/" && !typing) || (event.key.toLowerCase() === "k" && event.ctrlKey)) {
    event.preventDefault();
    $("q").focus();
    $("q").select();
  }
});

$("folders-btn").addEventListener("click", () => { $("drawer").hidden = false; });
$("close-drawer").addEventListener("click", () => { $("drawer").hidden = true; });

$("add-root").addEventListener("click", async () => {
  try {
    const result = await api("/api/roots", {
      method: "POST",
      body: JSON.stringify({ path: $("new-root").value }),
    });
    $("new-root").value = "";
    notice(result.already_present
      ? "That folder is already in the list."
      : `Added. Indexing ${result.indexing}.`);
    refreshStatus();
  } catch (error) {
    notice(`could not add folder: ${error.message}`);
  }
});

$("clear-index").addEventListener("click", async () => {
  if (!confirm("Remove every indexed item? Folders stay configured.")) return;
  try {
    const result = await api("/api/index/clear", { method: "POST", body: "{}" });
    notice(`Cleared ${result.removed_items} item(s). Press Index now to rebuild.`);
    clearResults();
    refreshStatus();
  } catch (error) {
    notice(`clear failed: ${error.message}`);
  }
});

function showStopped() {
  quitting = true;
  const pill = $("mode");
  pill.textContent = "stopped";
  pill.className = "pill";
  $("quit-app").disabled = true;
  notice("Content Search has stopped. You can close this tab.");
}

$("quit-app").addEventListener("click", async () => {
  if (!confirm("Stop Content Search? This tab will stop working.")) return;
  const quitButton = $("quit-app");
  quitting = true;
  quitButton.disabled = true;
  notice("Stopping Content Search...");
  try {
    await api("/api/quit", { method: "POST", body: "{}" });
    showStopped();
  } catch (error) {
    if (error instanceof TypeError) {
      // The server can close the socket before the browser reads its reply.
      showStopped();
    } else {
      quitting = false;
      quitButton.disabled = false;
      notice(`could not stop: ${error.message}`);
    }
  }
});

$("sync-mail").addEventListener("click", async () => {
  await syncMail("file");
});

$("sync-mail-com").addEventListener("click", async () => {
  await syncMail("com");
});

async function syncMail(via) {
  const limit = Number($("mail-limit").value) || 0;
  try {
    await api("/api/mail/outlook", {
      method: "POST",
      body: JSON.stringify({ limit: limit || null, via }),
    });
    const how = via === "com" ? "through Outlook" : "from the store file";
    notice(limit
      ? `Syncing up to ${limit} messages ${how} (watching pauses briefly)…`
      : `Syncing all mail ${how} — this can take a while, and watching pauses until it finishes.`);
    refreshStatus();
  } catch (error) {
    notice(`mail sync failed: ${error.message}`);
  }
}

$("index-now").addEventListener("click", async () => {
  try { await api("/api/index", { method: "POST", body: "{}" }); refreshStatus(); }
  catch (error) { notice(`index failed: ${error.message}`); }
});

$("watch").addEventListener("click", async () => {
  try {
    await api("/api/watch", {
      method: "POST",
      body: JSON.stringify({ interval: Number($("interval").value) || 2 }),
    });
    refreshStatus();
  } catch (error) {
    notice(`watch failed: ${error.message}`);
  }
});

$("stop").addEventListener("click", async () => {
  try { await api("/api/stop", { method: "POST", body: "{}" }); refreshStatus(); }
  catch (error) { notice(`stop failed: ${error.message}`); }
});

$("verify-btn").addEventListener("click", async () => {
  const button = $("verify-btn");
  const out = $("verify-out");
  button.disabled = true;
  out.textContent = "Checking index...";
  try {
    const report = await api("/api/verify");
    const table = document.createElement("table");
    table.className = "checks";
    for (const check of report.checks) {
      const row = document.createElement("tr");
      const flag = document.createElement("td");
      flag.className = check.count ? "bad" : "ok";
      flag.textContent = check.count ? "BAD" : "ok";
      const label = document.createElement("td");
      label.textContent = check.code;
      const count = document.createElement("td");
      count.className = "n";
      count.textContent = check.count;
      row.append(flag, label, count);
      table.append(row);
    }
    out.replaceChildren(table);
    const verdict = document.createElement("p");
    verdict.className = report.ok ? "ok small" : "bad small";
    verdict.textContent = report.ok
      ? "PASS — catalog, stored text, index and filesystem agree"
      : `FAIL — ${report.problems} problem(s)`;
    out.append(verdict);
  } catch (error) {
    notice(`verify failed: ${error.message}`);
    out.textContent = "Check failed - see the message at the top.";
  } finally {
    button.disabled = false;
  }
});

clearResults();
refreshStatus();
setInterval(refreshStatus, 2000);

// Deep link: ?q=... runs that search on load, so a URL can point at a query.
// ?open=<id> additionally previews one item.
const deep = new URLSearchParams(location.search);
const deepQuery = deep.get("q");
if (deepQuery) {
  $("q").value = deepQuery;
  runSearch(deepQuery).then(() => {
    const openId = Number(deep.get("open") || 0);
    if (openId) openItem(openId);
  });
}
