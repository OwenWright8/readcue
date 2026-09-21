// Small progressive enhancements; every page still works without this file.
(function () {
  "use strict";

  // Toasts fade out on their own (errors stay until dismissed).
  document.querySelectorAll(".toast").forEach(function (toast) {
    var close = function () {
      toast.classList.add("leaving");
      setTimeout(function () { toast.remove(); }, 320);
    };
    var button = toast.querySelector("button");
    if (button) button.addEventListener("click", close);
    if (!toast.classList.contains("error")) setTimeout(close, 7000);
  });

  // Inline edit panel on each reading row.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-toggle-edit]");
    if (!button) return;
    var row = button.closest(".reading");
    var open = row.classList.toggle("is-editing");
    button.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) row.querySelector(".reading-edit input").focus();
  });

  // Ticking a device means "only the ones I pick".
  document.querySelectorAll(".device input").forEach(function (box) {
    box.addEventListener("change", function () {
      var radio = document.getElementById("mode-some");
      if (radio) radio.checked = true;
    });
  });

  // "Check pages" on the textbook review screen: look at the pages in and around a proposed chapter, and nudge
  // its first or last page. The server sends JSON; everything here is built with textContent (no HTML injection).
  (function () {
    var section = document.querySelector("[data-check-url]");
    if (!section) return;
    var url = section.dataset.checkUrl;
    var timers = new WeakMap();
    var ROLE = { before: "Just before the chapter", first: "First page", last: "Last page", after: "Just after the chapter" };

    function el(tag, cls, text) {
      var node = document.createElement(tag);
      if (cls) node.className = cls;
      if (text != null) node.textContent = text;
      return node;
    }
    function fields(panel) {
      var row = panel.previousElementSibling;
      return {
        start: row.querySelector('input[name^="start-"]'),
        end: row.querySelector('input[name^="end-"]'),
        button: row.querySelector("[data-check]"),
      };
    }
    function message(panel, text, isError) {
      panel.replaceChildren(el("div", isError ? "pc-error" : "muted", text));
    }

    function load(panel) {
      var f = fields(panel);
      var start = parseInt(f.start.value, 10), end = parseInt(f.end.value, 10);
      if (!start || !end) return message(panel, "Enter the first and last page to see them here.");
      var token = String(Math.random());
      panel.dataset.token = token; // ignore a slow reply if the numbers changed again meanwhile
      var query = new URLSearchParams({ start: start, end: end, number: panel.dataset.number });
      fetch(url + "?" + query, { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (r) { return r.json().then(function (body) { return { ok: r.ok, body: body }; }); })
        .then(function (res) {
          if (panel.dataset.token !== token) return;
          if (res.ok) render(panel, res.body);
          else message(panel, res.body.error || "Couldn't load those pages.", true);
        })
        .catch(function () { if (panel.dataset.token === token) message(panel, "Couldn't load those pages.", true); });
    }

    function setRange(panel, start, end, pageCount) {
      var f = fields(panel);
      start = Math.min(Math.max(start, 1), pageCount);
      end = Math.min(Math.max(end, 1), pageCount);
      if (start > end) { if (document.activeElement === f.end || end < parseInt(f.start.value, 10)) start = end; else end = start; }
      f.start.value = start;
      f.end.value = end;
      load(panel);
    }

    function button(label, onClick) {
      var b = el("button", "btn btn-sm btn-ghost", label);
      b.type = "button";
      b.addEventListener("click", onClick);
      return b;
    }

    function card(panel, c, data) {
      var wrap = el("div", "pc-card pc-" + c.role);
      var title = el("div", "pc-title");
      title.appendChild(el("span", "pc-role", ROLE[c.role]));
      if (c.page) title.appendChild(el("span", "pc-page", "page " + c.page));
      wrap.appendChild(title);
      if (c.note) wrap.appendChild(el("div", "pc-note" + (c.tone ? " " + c.tone : ""), c.note));
      if (!c.page) return wrap;

      var f = fields(panel);
      var tail = c.role === "before" || c.role === "last";
      var lines = tail ? c.tail : c.head;
      var body = el("pre", "pc-body" + (tail && lines.length ? " tail" : ""), lines.length ? lines.join("\n") : "(no readable text)");
      var full = el("pre", "pc-body", c.text + (c.truncated ? "\n…" : ""));
      full.hidden = true;
      wrap.appendChild(body);
      wrap.appendChild(full);

      var actions = el("div", "pc-actions");
      var toggle = button("Show whole page", function () {
        full.hidden = !full.hidden;
        body.hidden = !full.hidden;
        toggle.textContent = full.hidden ? "Show whole page" : "Show less";
      });
      actions.appendChild(toggle);
      var start = parseInt(f.start.value, 10), end = parseInt(f.end.value, 10), n = data.page_count;
      if (c.role === "first") {
        actions.appendChild(button("◀ Start a page earlier", function () { setRange(panel, start - 1, end, n); }));
        actions.appendChild(button("Start a page later ▶", function () { setRange(panel, start + 1, end, n); }));
      } else if (c.role === "last") {
        actions.appendChild(button("◀ End a page earlier", function () { setRange(panel, start, end - 1, n); }));
        actions.appendChild(button("End a page later ▶", function () { setRange(panel, start, end + 1, n); }));
      } else if (c.role === "before") {
        actions.appendChild(button("Start here instead", function () { setRange(panel, c.page, end, n); }));
      } else if (c.role === "after") {
        actions.appendChild(button("End here instead", function () { setRange(panel, start, c.page, n); }));
      }
      wrap.appendChild(actions);
      return wrap;
    }

    function render(panel, data) {
      var head = el("div", "pc-head");
      head.appendChild(el("strong", null, data.pages + (data.pages === 1 ? " page" : " pages") + " · about " + data.words.toLocaleString() + " words"));
      data.warnings.forEach(function (w) { head.appendChild(el("span", "pc-warn", w)); });
      var cards = el("div", "pc-cards");
      data.cards.forEach(function (c) { cards.appendChild(card(panel, c, data)); });
      panel.replaceChildren(head, cards);
    }

    document.addEventListener("click", function (event) {
      var toggle = event.target.closest("[data-check]");
      if (!toggle) return;
      var panel = toggle.closest(".split").nextElementSibling;
      panel.hidden = !panel.hidden;
      toggle.setAttribute("aria-expanded", panel.hidden ? "false" : "true");
      if (!panel.hidden) load(panel);
    });

    // Typing new page numbers refreshes an open panel.
    document.addEventListener("input", function (event) {
      if (!event.target.matches('input[name^="start-"], input[name^="end-"]')) return;
      var panel = event.target.closest(".split").nextElementSibling;
      if (!panel || !panel.classList.contains("pagecheck") || panel.hidden) return;
      clearTimeout(timers.get(panel));
      timers.set(panel, setTimeout(function () { load(panel); }, 350));
    });
  })();

  // Confirm dialogs: <form data-confirm="..."> or <button data-confirm="...">.
  document.addEventListener("submit", function (event) {
    var message = (event.submitter && event.submitter.dataset.confirm) || event.target.dataset.confirm;
    if (message && !window.confirm(message)) event.preventDefault();
  });

  // Show a spinner and message on long-running submits: <form data-busy="Reading the file...">.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (event.defaultPrevented || !form.dataset.busy) return;
    var button = event.submitter || form.querySelector("button[type=submit], button:not([type])");
    if (!button) return;
    setTimeout(function () {
      button.disabled = true;
      button.classList.add("is-busy");
      button.innerHTML = '<span class="spinner"></span> ' + form.dataset.busy;
    }, 0);
  });

  // File drop zones: list the chosen files.
  function formatSize(bytes) {
    if (bytes > 1048576) return (bytes / 1048576).toFixed(1) + " MB";
    return Math.max(1, Math.round(bytes / 1024)) + " KB";
  }
  document.querySelectorAll(".dropzone").forEach(function (zone) {
    var input = zone.querySelector("input[type=file]");
    var list = zone.parentElement.querySelector(".file-list");
    ["dragenter", "dragover"].forEach(function (name) {
      zone.addEventListener(name, function () { zone.classList.add("is-over"); });
    });
    ["dragleave", "drop"].forEach(function (name) {
      zone.addEventListener(name, function () { zone.classList.remove("is-over"); });
    });
    input.addEventListener("change", function () {
      if (!list) return;
      list.innerHTML = "";
      Array.prototype.forEach.call(input.files, function (file) {
        var item = document.createElement("li");
        var name = document.createElement("span");
        var size = document.createElement("span");
        name.textContent = file.name;
        size.className = "size";
        size.textContent = formatSize(file.size);
        item.appendChild(name);
        item.appendChild(size);
        list.appendChild(item);
      });
    });
  });

  // Live filter for the glossary.
  var filter = document.querySelector("[data-filter-terms]");
  if (filter) {
    filter.addEventListener("input", function () {
      var query = filter.value.trim().toLowerCase();
      document.querySelectorAll(".term-card").forEach(function (card) {
        card.hidden = query !== "" && card.textContent.toLowerCase().indexOf(query) === -1;
      });
    });
  }

  // Close an open <details class="menu"> when clicking elsewhere.
  document.addEventListener("click", function (event) {
    document.querySelectorAll("details.menu[open]").forEach(function (menu) {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });
})();
