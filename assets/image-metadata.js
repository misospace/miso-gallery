// Lazily loads embedded PNG generation metadata into the per-card Details panel.
//
// This lives in an external file rather than an inline <script> because the
// page CSP allows script-src 'self' only — inline scripts carry no usable nonce
// here. Nothing below touches innerHTML: every value comes from a user-supplied
// file's PNG text chunks and is written with textContent.

(function () {
  'use strict';

  var LABEL_TO_FIELD = [
    ['Seed', 'seed'],
    ['Model', 'model'],
    ['Sampler', 'sampler'],
    ['Steps', 'steps'],
    ['CFG scale', 'cfg'],
    ['Size', 'size']
  ];

  function row(label, value) {
    var el = document.createElement('div');
    el.className = 'image-details-row';
    var l = document.createElement('span');
    l.className = 'image-details-label';
    l.textContent = label;
    var v = document.createElement('span');
    v.className = 'image-details-value';
    v.textContent = value;
    el.appendChild(l);
    el.appendChild(v);
    return el;
  }

  function promptBlock(summaryText, text) {
    var details = document.createElement('details');
    details.className = 'image-metadata-prompt';
    var summary = document.createElement('summary');
    summary.textContent = summaryText;
    var body = document.createElement('div');
    body.className = 'image-metadata-prompt-body';
    body.textContent = text;
    details.appendChild(summary);
    details.appendChild(body);
    return details;
  }

  function selectText(node) {
    if (window.getSelection && document.createRange) {
      var range = document.createRange();
      range.selectNodeContents(node);
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
  }

  function recipeRow(recipe) {
    var el = document.createElement('div');
    el.className = 'image-details-row image-metadata-recipe';
    var l = document.createElement('span');
    l.className = 'image-details-label';
    l.textContent = 'Recipe';
    var right = document.createElement('span');
    right.className = 'image-details-value';
    var text = document.createElement('span');
    text.className = 'image-metadata-recipe-text';
    text.textContent = recipe;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'image-metadata-copy';
    btn.textContent = 'Copy';
    btn.addEventListener('click', function () {
      var clipboard = navigator.clipboard;
      if (!clipboard || typeof clipboard.writeText !== 'function') {
        selectText(text);
        return;
      }
      clipboard.writeText(recipe).then(function () {
        btn.textContent = 'Copied';
        btn.classList.add('is-copied');
        window.setTimeout(function () {
          btn.textContent = 'Copy';
          btn.classList.remove('is-copied');
        }, 1500);
      }, function () {
        // Clipboard blocked (insecure context, permission): let the user copy
        // by hand instead of silently doing nothing.
        selectText(text);
      });
    });
    right.appendChild(text);
    right.appendChild(btn);
    el.appendChild(l);
    el.appendChild(right);
    return el;
  }

  function render(container, metadata) {
    while (container.firstChild) container.removeChild(container.firstChild);

    if (metadata.category) {
      container.appendChild(row('Category', metadata.category));
    }

    var rolls = metadata.rolls;
    if (Array.isArray(rolls) && rolls.length) {
      var list = document.createElement('div');
      list.className = 'image-metadata-rolls';
      var listLabel = document.createElement('span');
      listLabel.className = 'image-details-label';
      listLabel.textContent = 'Rolls';
      list.appendChild(listLabel);
      var values = document.createElement('div');
      values.className = 'image-metadata-roll-list';
      rolls.forEach(function (roll) {
        var item = document.createElement('div');
        item.className = 'image-metadata-roll';
        item.textContent = String(roll);
        values.appendChild(item);
      });
      list.appendChild(values);
      container.appendChild(list);
    }

    if (metadata.positive) {
      container.appendChild(promptBlock('Prompt', metadata.positive));
    }
    if (metadata.negative) {
      container.appendChild(promptBlock('Negative prompt', metadata.negative));
    }

    LABEL_TO_FIELD.forEach(function (pair) {
      var value = metadata[pair[1]];
      if (value !== null && value !== undefined && value !== '') {
        container.appendChild(row(pair[0], String(value)));
      }
    });

    if (metadata.recipe) {
      container.appendChild(recipeRow(String(metadata.recipe)));
    }

    container.hidden = false;
  }

  function load(container) {
    var url = container.getAttribute('data-metadata-url');
    if (!url) return;
    // Fetch on first open only: closing and reopening the panel must not
    // re-request, and a 500-item page should not fan out 500 requests.
    if (container.dataset.metadataRequested === '1') return;
    container.dataset.metadataRequested = '1';
    fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (res) {
        if (!res.ok) return null;
        return res.json();
      })
      .then(function (payload) {
        // Non-PNG media, or a PNG with no text chunks: leave the container
        // hidden so the Details panel looks exactly as it did before.
        if (!payload || payload.metadata === null || payload.metadata === undefined) return;
        render(container, payload.metadata);
      })
      .catch(function () {
        /* Network or parse failure: the panel simply stays without metadata. */
      });
  }

  function init() {
    var panels = document.querySelectorAll('.image-details');
    Array.prototype.forEach.call(panels, function (panel) {
      var container = panel.querySelector('.image-metadata');
      if (!container || container.dataset.metadataBound === '1') return;
      container.dataset.metadataBound = '1';
      panel.addEventListener('toggle', function () {
        if (panel.open) load(container);
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();