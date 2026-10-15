// A terminal in the browser, joined over a WebSocket to a terminal in the Runtime.
//
//   <div data-terminal="/ws/...">              the terminal, bridged to that socket
//   <form data-terminal-send><input>...</form>  sends the input's value, then Enter
//
// When the socket closes, the terminal element dispatches "terminal-closed" (detail: {code}).
(() => {
  "use strict";
  const element = document.querySelector("[data-terminal]");
  if (!element) return;

  const terminal = new Terminal({ cursorBlink: true, fontSize: 13, scrollback: 2000 });
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
  terminal.onData((data) => send({ type: "input", data }));
  new ResizeObserver(resize).observe(element);

  for (const form of document.querySelectorAll("[data-terminal-send]")) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const input = form.querySelector("input");
      send({ type: "input", data: `${input.value}\r` });
      input.value = "";
    });
  }
})();
