// The login page: once the login terminal closes, say how the sign-in went.
document.addEventListener("terminal-closed", async (event) => {
  "use strict";
  let outcome = "lost"; // the connection dropped: the login environment may still be waiting
  if (event.detail.code === 1000 || event.detail.code === 4404) {
    try {
      const status = await (await fetch("/api/claude-account")).json();
      outcome = status.connected ? "connected" : "not-connected";
    } catch {
      // keep "lost": reloading the page tells the User where things stand
    }
  }
  for (const element of document.querySelectorAll("[data-outcome]")) {
    element.hidden = element.dataset.outcome !== outcome;
  }
  for (const element of document.querySelectorAll("[data-while-signing-in]")) {
    element.hidden = outcome !== "lost";
  }
});
