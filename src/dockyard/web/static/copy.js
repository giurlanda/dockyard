// Copy buttons: data-copy names the field whose text the button copies; the element whose
// data-copied names the same field says it was copied.
(() => {
  "use strict";
  for (const button of document.querySelectorAll("[data-copy]")) {
    button.addEventListener("click", async () => {
      const field = document.getElementById(button.dataset.copy);
      field.select(); // without the clipboard, the person copies the selection themselves
      try {
        await navigator.clipboard.writeText(field.value);
      } catch {
        return;
      }
      for (const copied of document.querySelectorAll(`[data-copied="${button.dataset.copy}"]`)) {
        copied.hidden = false;
      }
    });
  }
})();
