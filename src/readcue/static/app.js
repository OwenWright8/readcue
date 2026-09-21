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
