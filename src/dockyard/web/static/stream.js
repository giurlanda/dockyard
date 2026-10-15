// A Project's log, console or Terminal: once its terminal closes, say why.
document.addEventListener("terminal-closed", (event) => {
  "use strict";
  const outcome =
    {
      1000: "ended", // the Remote Control Server stopped or restarted, or the shell exited
      4404: "not-running", // the Project is gone
      4409: "not-running",
      4503: "unavailable",
    }[event.detail.code] ?? "lost";
  for (const element of document.querySelectorAll("[data-outcome]")) {
    element.hidden = element.dataset.outcome !== outcome;
  }
});
