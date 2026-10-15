// A Project's page while it clones: show git's progress, then reload once the clone has ended
// (the page then shows the Project, or why the clone failed).
// While it waits for the Claude Account: reload once it no longer waits (it started).
(() => {
  "use strict";
  const waiting = document.querySelector("[data-waiting]");
  if (!waiting) return;

  const follow = async () => {
    try {
      const response = await fetch(waiting.dataset.waiting);
      if (response.ok && (await response.json()).status !== "waiting") {
        location.reload();
        return;
      }
    } catch {
      // Dockyard unreachable for a moment: keep trying
    }
    setTimeout(follow, 2000);
  };
  setTimeout(follow, 2000);
})();

(() => {
  "use strict";
  const output = document.querySelector("[data-clone]");
  if (!output) return;

  const follow = async () => {
    try {
      const response = await fetch(output.dataset.clone);
      if (response.status === 404) return; // nothing to follow any more: the page says the rest
      if (response.ok) {
        const clone = await response.json();
        output.textContent = clone.output;
        output.scrollTop = output.scrollHeight;
        if (clone.state !== "running") {
          location.reload();
          return;
        }
      }
    } catch {
      // Dockyard unreachable for a moment: keep trying
    }
    setTimeout(follow, 1000);
  };
  setTimeout(follow, 1000);
})();
