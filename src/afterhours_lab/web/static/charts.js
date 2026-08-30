// Draw every server-supplied figure. The browser never computes a research value:
// each element carries a complete Plotly spec that the Python layer produced.
(function () {
  function draw(root) {
    (root || document).querySelectorAll("[data-figure]").forEach(function (node) {
      if (node.dataset.drawn === "1") return;
      var spec;
      try {
        spec = JSON.parse(node.dataset.figure);
      } catch (err) {
        node.textContent = "chart specification could not be parsed";
        return;
      }
      if (!spec || !spec.data || !spec.data.length) {
        node.innerHTML = '<p class="muted">no data for this chart</p>';
        node.dataset.drawn = "1";
        return;
      }
      Plotly.newPlot(node, spec.data, spec.layout, spec.config);
      node.dataset.drawn = "1";
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    draw(document);
  });
  document.body.addEventListener("htmx:afterSwap", function (event) {
    draw(event.target);
  });
})();
