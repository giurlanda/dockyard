// A terminal in the browser, joined over a WebSocket to a terminal in the Runtime.
//
//   <div data-terminal="/ws/...">              the terminal, bridged to that socket
//   <div data-terminal="/ws/..." data-terminal-readonly>  only shows what the socket sends
//   <form data-terminal-send><input>...</form>  sends the input's value, then Enter
//   <button data-terminal-key="escape">          sends a key a phone keyboard lacks: escape, tab,
//                                                ctrl-c, up, down
//
// When the socket closes, the terminal element dispatches "terminal-closed" (detail: {code}).
// Leaving the page closes the socket: Dockyard stops following what it showed.
(() => {
  "use strict";
  const element = document.querySelector("[data-terminal]");
  if (!element) return;

  const readonly = element.hasAttribute("data-terminal-readonly");
  const terminal = new Terminal({
    cursorBlink: !readonly,
    disableStdin: readonly,
    fontSize: 13,
    scrollback: 2000,
  });
  const fit = new FitAddon.FitAddon();
  terminal.loadAddon(fit);
  terminal.loadAddon(new WebLinksAddon.WebLinksAddon());
  terminal.open(element);
  fit.fit();

  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${scheme}//${location.host}${element.dataset.terminal}`);
  socket.binaryType = "arraybuffer";
  const send = (message) => {
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  };
  const resize = () => {
    fit.fit();
    send({ type: "resize", cols: terminal.cols, rows: terminal.rows });
  };

  socket.addEventListener("open", resize);
  socket.addEventListener("message", (event) => terminal.write(new Uint8Array(event.data)));
  socket.addEventListener("close", (event) => {
    element.dispatchEvent(
      new CustomEvent("terminal-closed", { bubbles: true, detail: { code: event.code } }),
    );
  });
  if (!readonly) terminal.onData((data) => send({ type: "input", data }));
  new ResizeObserver(resize).observe(element);
  addEventListener("pagehide", () => socket.close());

  // Arrows as the program in the terminal asked for them: full-screen programs want the
  // "application" ones.
  const arrow = (code) => (terminal.modes.applicationCursorKeysMode ? "\x1bO" : "\x1b[") + code;
  const keys = {
    escape: () => "\x1b",
    tab: () => "\t",
    "ctrl-c": () => "\x03",
    up: () => arrow("A"),
    down: () => arrow("B"),
  };
  for (const button of document.querySelectorAll("[data-terminal-key]")) {
    // Keeps the focus where it is: on a phone, the keyboard stays open.
    button.addEventListener("mousedown", (event) => event.preventDefault());
    button.addEventListener("click", () => {
      send({ type: "input", data: keys[button.dataset.terminalKey]() });
    });
  }

  for (const form of document.querySelectorAll("[data-terminal-send]")) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const input = form.querySelector("input");
      send({ type: "input", data: `${input.value}\r` });
      input.value = "";
    });
  }
})();
